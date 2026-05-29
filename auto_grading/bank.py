"""Banque de questions partageable (Phase 1 MVP).

Une banque locale stocke des questions exportées depuis un projet AMCx, pour
être réimportées dans un autre projet. Stockage = 1 fichier JSON par question
sous `~/Documents/AMCx-banque/questions/` (override env `AMCX_BANK_DIR`).

Format d'une question :
```
{
  "bank_id":  "a3f2k7e9",                # uuid hex[:8], stable
  "kind":     "question_qcm",            # text|question_qcm|question_open|answerbox
  "data":     { ... },                   # data du Block AMCx (sans `bid` ni `_bank_id`)
  "title":    "Loi binomiale",
  "tags":     ["proba", "L2"],
  "author":   "epilliat@gmail.com",
  "created_at":  "2026-05-27T15:30:00",
  "modified_at": "2026-05-27T15:30:00",
  "version":  1,
  "source_project": "test0"
}
```

`index.json` = cache pour browse rapide. Reconstruit au démarrage si
désynchronisé d'avec les fichiers (mtime).

API publique :
- `bank_root()`, `ensure_root()`, `question_dir()`, `index_path()`
- `load(bank_id)`, `save(question)`, `delete(bank_id)`
- `list_questions(filters)`, `rebuild_index()`
- `from_block(block, ...)`, `to_block(question)`
"""

from __future__ import annotations

import copy as _copy
import json
import os
import re
import secrets
import unicodedata
from datetime import datetime
from pathlib import Path
from threading import Lock

from sujet_store import Block, _gen_bid

DEFAULT_BANK_ROOT = Path.home() / "Documents" / "AMCx-banque"

_lock = Lock()


def bank_root() -> Path:
    """Racine de la banque (env `AMCX_BANK_DIR` > défaut `~/Documents/AMCx-banque`)."""
    env = os.environ.get("AMCX_BANK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_BANK_ROOT


def question_dir() -> Path:
    return bank_root() / "questions"


def index_path() -> Path:
    return bank_root() / "index.json"


def ensure_root() -> Path:
    """Crée le dossier racine + sous-dossier `questions/` si absents."""
    root = bank_root()
    (root / "questions").mkdir(parents=True, exist_ok=True)
    return root


def _new_bank_id() -> str:
    """8 hex aléatoires — suffisant pour 1e9 questions sans collision (P~1e-4)."""
    return secrets.token_hex(4)


def _slug(s: str) -> str:
    """ascii + lower + dashes. Pour le nom de fichier (lecture humaine seule)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:50] or "untitled"


def _path_of(bank_id: str, slug: str) -> Path:
    return question_dir() / f"{bank_id}-{slug}.json"


def _find_path(bank_id: str) -> Path | None:
    """Cherche le fichier d'une question par bank_id (slug variable)."""
    for p in question_dir().glob(f"{bank_id}-*.json"):
        return p
    return None


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# I/O fichiers
# --------------------------------------------------------------------------

def load(bank_id: str) -> dict:
    """Charge 1 question depuis disque. Lève KeyError si absent."""
    p = _find_path(bank_id)
    if not p:
        raise KeyError(bank_id)
    return json.loads(p.read_text(encoding="utf-8"))


def save(question: dict) -> Path:
    """Écrit la question + reindex. Retourne le chemin écrit.

    Si un fichier existe déjà pour le même `bank_id` mais avec un slug différent
    (titre modifié), l'ancien est supprimé.
    """
    with _lock:
        ensure_root()
        bid = question["bank_id"]
        slug = _slug(question.get("title") or question.get("kind"))
        new_path = _path_of(bid, slug)
        # Supprimer un éventuel ancien fichier (slug obsolète) pour ce même bank_id.
        old = _find_path(bid)
        if old and old != new_path:
            old.unlink(missing_ok=True)
        new_path.write_text(json.dumps(question, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        rebuild_index()
        return new_path


def delete(bank_id: str) -> None:
    """Supprime une question. Idempotent (no-op si absente)."""
    with _lock:
        p = _find_path(bank_id)
        if p:
            p.unlink(missing_ok=True)
            rebuild_index()


# --------------------------------------------------------------------------
# Index (cache pour list_questions)
# --------------------------------------------------------------------------

def _build_index_entries() -> list[dict]:
    out: list[dict] = []
    for p in sorted(question_dir().glob("*.json")):
        try:
            q = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(q, dict) or "bank_id" not in q:
            continue
        out.append({
            "bank_id":     q.get("bank_id", ""),
            "kind":        q.get("kind", ""),
            "title":       q.get("title", ""),
            "tags":        q.get("tags", []) or [],
            "author":      q.get("author", ""),
            "modified_at": q.get("modified_at", ""),
            "source_project": q.get("source_project", ""),
            "stats":       stats_summary(q),
        })
    return out


def rebuild_index() -> dict:
    """Scan `questions/`, écrit `index.json`. Retourne l'index."""
    ensure_root()
    entries = _build_index_entries()
    idx = {"mtime": _now(), "questions": entries}
    index_path().write_text(json.dumps(idx, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return idx


def _read_or_rebuild_index() -> dict:
    """Lit `index.json` ; le reconstruit si absent ou désynchronisé."""
    ensure_root()
    qdir = question_dir()
    ipath = index_path()
    if not ipath.exists():
        return rebuild_index()
    try:
        idx_mtime = ipath.stat().st_mtime
        # Désynchronisation si un fichier de question est plus récent que l'index,
        # ou si le compte ne colle pas.
        files = list(qdir.glob("*.json"))
        if files and max(f.stat().st_mtime for f in files) > idx_mtime:
            return rebuild_index()
        idx = json.loads(ipath.read_text(encoding="utf-8"))
        if not isinstance(idx, dict) or "questions" not in idx:
            return rebuild_index()
        if len(idx["questions"]) != len(files):
            return rebuild_index()
        return idx
    except Exception:
        return rebuild_index()


def list_questions(filters: dict | None = None) -> list[dict]:
    """Liste les questions. Filtres optionnels :
    - `kind`   : str (exact match)
    - `tags`   : list[str] (any-match : au moins 1 tag commun)
    - `q`      : str (substring sur title + tags, casse-insensible)
    - `author` : str (substring sur author)
    """
    filters = filters or {}
    idx = _read_or_rebuild_index()
    items = list(idx.get("questions", []))

    kind = (filters.get("kind") or "").strip()
    if kind:
        items = [q for q in items if q.get("kind") == kind]

    tags = filters.get("tags") or []
    if tags:
        tagset = {t.lower() for t in tags if t}
        items = [q for q in items
                 if tagset.intersection({(t or "").lower() for t in (q.get("tags") or [])})]

    qs = (filters.get("q") or "").strip().lower()
    if qs:
        def hit(item: dict) -> bool:
            hay = (item.get("title", "") + " " +
                   " ".join(item.get("tags") or [])).lower()
            return qs in hay
        items = [q for q in items if hit(q)]

    author = (filters.get("author") or "").strip().lower()
    if author:
        items = [q for q in items if author in (q.get("author") or "").lower()]

    # Tri : récents en premier (modified_at descendant).
    items.sort(key=lambda q: q.get("modified_at", ""), reverse=True)
    return items


# --------------------------------------------------------------------------
# Conversions Block ↔ question banque
# --------------------------------------------------------------------------

# Champs internes du `data` d'un Block AMCx qu'il ne faut PAS exporter
# (ils dépendent du projet d'origine). En particulier `_bank_id` reste sur le
# Block local (trace d'origine), mais ne se propage pas dans la banque.
_BLOCK_DATA_PRIVATE_KEYS = {"_bank_id"}


def _strip_private(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items()
            if k not in _BLOCK_DATA_PRIVATE_KEYS}


def from_block(block, project_name: str = "", title: str = "",
               tags: list[str] | None = None, author: str = "") -> dict:
    """Convertit un Block (ou dict bloc) en question de banque.

    `block` accepte un dataclass `Block` ou un dict `{bid, kind, data}` (cas
    venu de l'API). Le `bid` du projet d'origine n'est pas conservé.
    """
    if isinstance(block, Block):
        kind = block.kind
        data = _strip_private(block.data)
    else:
        kind = block.get("kind", "")
        data = _strip_private(block.get("data", {}))
    if kind not in ("text", "question_qcm", "question_open", "answerbox"):
        raise ValueError(f"kind invalide: {kind}")
    now = _now()
    return {
        "bank_id":        _new_bank_id(),
        "kind":           kind,
        "data":           _copy.deepcopy(data),
        "title":          (title or _auto_title_from_data(kind, data) or "").strip(),
        "tags":           [t.strip() for t in (tags or []) if t and t.strip()],
        "author":         (author or "").strip(),
        "created_at":     now,
        "modified_at":    now,
        "version":        1,
        "source_project": (project_name or "").strip(),
    }


def to_block(question: dict) -> Block:
    """Convertit une question de banque en Block AMCx prêt à être inséré.

    Génère un bid frais et conserve la trace d'origine via `data._bank_id`.
    """
    kind = question["kind"]
    data = _copy.deepcopy(question.get("data", {}) or {})
    data["_bank_id"] = question["bank_id"]
    return Block(bid=_gen_bid(kind), kind=kind, data=data)


def update_project_stats(bank_id: str, project_name: str, n_eval: int,
                          sum_normalized: float, n_perfect: int,
                          max_score_at_sync: float) -> None:
    """Idempotent : remplace l'entrée `stats.by_project[<project_name>]`
    par les compteurs fournis. Le sync de stats côté serveur recalcule
    depuis raw_responses/, donc cette fonction écrase plutôt que d'incrémenter.
    """
    with _lock:
        q = load(bank_id)
        stats = q.setdefault("stats", {})
        by_p = stats.setdefault("by_project", {})
        by_p[project_name] = {
            "n_eval":            int(n_eval),
            "sum_normalized":    round(float(sum_normalized), 4),
            "n_perfect":         int(n_perfect),
            "max_score_at_sync": round(float(max_score_at_sync), 4),
            "last_sync":         _now(),
        }
    save(q)   # hors lock — save reprend le lock


def stats_summary(question: dict) -> dict:
    """Résumé des stats d'une question (utilisable dans l'index/liste).

    Retourne `{n_eval, n_perfect, sum_normalized, mean_normalized, projects:N}`.
    Tous les agrégats sont des sommes (n_eval, n_perfect, sum_normalized)
    sur l'ensemble des projets. `mean_normalized = sum_normalized / n_eval`
    si n_eval > 0, sinon None. `projects` = nombre de projets qui ont
    contribué (≥1 copie).
    """
    by_p = ((question.get("stats") or {}).get("by_project") or {})
    n_eval = 0
    n_perfect = 0
    sum_n = 0.0
    projs = 0
    for _, v in by_p.items():
        ne = int(v.get("n_eval", 0))
        if ne <= 0:
            continue
        projs += 1
        n_eval += ne
        n_perfect += int(v.get("n_perfect", 0))
        sum_n += float(v.get("sum_normalized", 0.0))
    mean = (sum_n / n_eval) if n_eval > 0 else None
    return {
        "n_eval":          n_eval,
        "n_perfect":       n_perfect,
        "sum_normalized":  round(sum_n, 4),
        "mean_normalized": (round(mean, 4) if mean is not None else None),
        "projects":        projs,
    }


def _auto_title_from_data(kind: str, data: dict) -> str:
    """Titre par défaut quand l'utilisateur n'en fournit pas."""
    if kind == "question_qcm":
        st = (data.get("statement") or "").strip()
        return _short(st) or (data.get("tag") or "QCM")
    if kind == "question_open":
        st = (data.get("statement") or "").strip()
        return _short(st) or (data.get("tag") or "Question ouverte")
    if kind == "answerbox":
        return data.get("title") or "Zone de réponse"
    if kind == "text":
        return _short((data.get("tex") or "")) or "Bloc texte"
    return kind


def _short(s: str, n: int = 60) -> str:
    s = (s or "").strip().replace("\n", " ").replace("\\", "")
    s = re.sub(r"\s+", " ", s)
    return s[:n] + ("…" if len(s) > n else "")
