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
from threading import RLock

import bank_taxonomy as tx
import config
from sujet_store import Block, _gen_bid

DEFAULT_BANK_ROOT = Path.home() / "Documents" / "AMCx-banque"

# Version du cache `index.json`. Incrémentée quand la forme d'une entrée
# change (v2 = ajout de `categories`) : `_read_or_rebuild_index` reconstruit
# alors tout seul, sans que l'utilisateur ait rien à supprimer à la main.
INDEX_VERSION = 2

# Version du fichier `categories.json`.
CATEGORIES_VERSION = 1

# Sentinelle : distingue « paramètre absent » de `None` (= remettre à la
# racine) dans `update_category(parent_id=…)`.
UNSET = object()

_lock = RLock()   # réentrant : update_project_stats appelle save() sous le lock


def bank_root() -> Path:
    """Racine de la banque locale active.

    Précédence : banque **explicitement configurée** (`config.banks[active]`,
    type local) > env `AMCX_BANK_DIR` > chemin de repli > défaut
    `~/Documents/AMCx-banque`.

    ⚠ Le test « explicitement configurée » compte : sans banque dans la config,
    `active_bank_cfg()` synthétise un repli qui porte DÉJÀ un `path`, si bien
    que la branche `AMCX_BANK_DIR` n'était jamais atteinte — la variable
    d'environnement documentée était sans effet dans tous les cas.
    """
    entry = config.active_bank_cfg()
    configured = bool((config.load_config().get("banks") or {}))
    if configured and entry.get("type") == "local" and entry.get("path"):
        return Path(str(entry["path"])).expanduser().resolve()
    env = os.environ.get("AMCX_BANK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if entry.get("type") == "local" and entry.get("path"):
        return Path(str(entry["path"])).expanduser().resolve()
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


# 8 hex (banque locale) ou UUID v4 (banque en ligne). Validé avant tout glob :
# `bank_id="*"` matchait la première question venue — `DELETE /api/bank/*`
# supprimait une question au hasard.
_BANK_ID_RE = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-fA-F-]{36})$")


def is_valid_bank_id(bank_id) -> bool:
    return bool(_BANK_ID_RE.match(str(bank_id or "")))


def _find_path(bank_id: str) -> Path | None:
    """Cherche le fichier d'une question par bank_id (slug variable)."""
    if not is_valid_bank_id(bank_id):
        return None
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


def save(question: dict, *, reindex: bool = True) -> Path:
    """Écrit la question + reindex. Retourne le chemin écrit.

    Si un fichier existe déjà pour le même `bank_id` mais avec un slug différent
    (titre modifié), l'ancien est supprimé.

    `reindex=False` saute la reconstruction de l'index — `rebuild_index()` scanne
    TOUT le dossier, donc affecter 40 questions à une catégorie ferait 40 scans
    complets. Les opérations en lot passent donc à False et reconstruisent une
    seule fois à la fin. L'appelant en devient responsable.
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
        config.write_json_atomic(new_path, question)
        if reindex:
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
            "categories":  list(q.get("categories") or []),
            "stats":       stats_summary(q),
        })
    return out


def rebuild_index() -> dict:
    """Scan `questions/`, écrit `index.json`. Retourne l'index."""
    ensure_root()
    entries = _build_index_entries()
    idx = {"index_version": INDEX_VERSION, "mtime": _now(),
           "questions": entries}
    config.write_json_atomic(index_path(), idx)
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
        # Index écrit par une version antérieure (sans `categories`) → rebuild.
        if int(idx.get("index_version") or 1) != INDEX_VERSION:
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
    - `category`      : str (uuid) — questions de ce nœud
    - `descendants`   : bool (défaut True) — inclure les sous-catégories
    - `uncategorized` : bool — questions sans aucune catégorie vivante
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

    # Catégories. Un id syntaxiquement invalide est une erreur (il finirait
    # interpolé dans une URL PostgREST côté online) ; un id valide mais absent
    # de l'arbre ne l'est pas : le nœud a pu être supprimé entre-temps.
    cat = (filters.get("category") or "").strip()
    want_uncat = bool(filters.get("uncategorized"))
    if cat or want_uncat:
        nodes = load_categories()
        if cat:
            if not tx.is_valid_cat_id(cat):
                raise tx.TaxonomyError(f"identifiant de catégorie invalide : {cat!r}")
            wanted = (tx.descendants(nodes, cat)
                      if filters.get("descendants", True) else {cat})
            items = [q for q in items
                     if wanted & set(q.get("categories") or [])]
        if want_uncat:
            # « Sans catégorie » ignore les ids morts (nœud supprimé ailleurs) :
            # sinon une question resterait introuvable des deux côtés du filtre.
            known = tx.index_by_id(nodes)
            items = [q for q in items
                     if not any(c in known for c in (q.get("categories") or []))]

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
               tags: list[str] | None = None, author: str = "",
               categories: list[str] | None = None) -> dict:
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
    if kind not in ("text", "question_qcm", "question_open",
                    "question_freeform", "answerbox"):
        raise ValueError(f"kind invalide: {kind}")
    now = _now()
    return {
        "bank_id":        _new_bank_id(),
        "kind":           kind,
        "data":           _copy.deepcopy(data),
        "title":          (title or _auto_title_from_data(kind, data) or "").strip(),
        "tags":           [t.strip() for t in (tags or []) if t and t.strip()],
        "categories":     _dedup_cat_ids(categories),
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
        save(q)


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
    if kind == "question_freeform":
        st = (data.get("statement") or "").strip()
        return _short(st) or (data.get("tag") or "Question libre")
    if kind == "answerbox":
        return data.get("title") or "Zone de réponse"
    if kind == "text":
        return _short((data.get("tex") or "")) or "Bloc texte"
    return kind


def _short(s: str, n: int = 60) -> str:
    s = (s or "").strip().replace("\n", " ").replace("\\", "")
    s = re.sub(r"\s+", " ", s)
    return s[:n] + ("…" if len(s) > n else "")


# --------------------------------------------------------------------------
# Catégories (arbre partagé de la banque) — voir BANK_CATEGORIES_PLAN.md
#
# L'arbre vit dans `<bank>/categories.json`, à la RACINE de la banque et surtout
# pas dans `questions/` : `_read_or_rebuild_index()` compare le nombre de
# fichiers `questions/*.json` au nombre d'entrées de l'index, donc un intrus
# dans ce dossier forcerait un rebuild complet à chaque lecture.
#
# Les affectations vivent sur la question (`categories: [uuid]`), comme `tags` :
# un fichier de question reste autoportant (copie, git, envoi par mail).
# --------------------------------------------------------------------------

def categories_path() -> Path:
    return bank_root() / "categories.json"


def _dedup_cat_ids(cat_ids) -> list[str]:
    """Filtre de forme seulement (pas d'accès à l'arbre) : ids valides, sans
    doublon, ordre conservé. Utilisable depuis `from_block`, qui est partagé
    avec le backend en ligne et ne doit donc faire aucune I/O locale."""
    out: list[str] = []
    for c in (cat_ids or []):
        c = str(c or "")
        if tx.is_valid_cat_id(c) and c not in out:
            out.append(c)
    return out


def load_categories() -> list[dict]:
    """Nœuds validés de l'arbre. `[]` si le fichier est absent.

    ⚠ Ne crée jamais le fichier : une banque locale posée sur un partage en
    lecture seule doit rester consultable.

    Fichier illisible ou arbre invalide → il est mis de côté en
    `categories.json.corrupt-<horodatage>` (même convention que `subject.json`)
    et l'arbre repart vide, plutôt que de rendre la banque entière inutilisable.
    Si la mise de côté échoue (dossier non inscriptible), on l'ignore.
    """
    p = categories_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        nodes = raw.get("nodes") if isinstance(raw, dict) else raw
        return tx.validate_nodes(nodes or [])
    except Exception:
        try:
            p.rename(p.with_name(p.name + ".corrupt-"
                                 + datetime.now().strftime("%Y%m%d-%H%M%S")))
        except OSError:
            pass
        return []


def _write_categories(nodes) -> list[dict]:
    """Valide puis écrit l'arbre. Retourne les nœuds normalisés."""
    with _lock:
        clean = tx.validate_nodes(nodes)
        ensure_root()
        config.write_json_atomic(categories_path(), {
            "version":     CATEGORIES_VERSION,
            "modified_at": _now(),
            "nodes":       clean,
        })
        return clean


def _members_by_category() -> dict[str, set]:
    """`{cat_id: {bank_id, …}}` — affectations DIRECTES, lues depuis l'index."""
    out: dict[str, set] = {}
    for e in _read_or_rebuild_index().get("questions", []):
        for c in e.get("categories") or []:
            out.setdefault(c, set()).add(e.get("bank_id"))
    return out


def list_categories() -> list[dict]:
    """Arbre aplati en ordre préfixe, avec `depth`, `path`, `n_direct`, `n_total`."""
    return tx.annotate(load_categories(), _members_by_category())


def create_category(name: str, parent_id: str | None = None,
                    position: int | None = None) -> dict:
    """Crée un nœud. `position=None` → placé en dernier parmi ses frères."""
    with _lock:
        nodes = load_categories()
        name = tx.clean_name(name)
        parent_id = parent_id or None
        if parent_id is not None:
            if not tx.is_valid_cat_id(parent_id):
                raise tx.TaxonomyError(f"parent invalide : {parent_id!r}")
            if parent_id not in tx.index_by_id(nodes):
                raise KeyError(parent_id)
            if tx.depth(nodes, parent_id) + 1 > tx.MAX_DEPTH:
                raise tx.TaxonomyConflict(
                    f"Profondeur maximale atteinte ({tx.MAX_DEPTH} niveaux).")
        if tx.sibling_conflict(nodes, parent_id, name):
            raise tx.TaxonomyConflict(
                f"Une catégorie sœur s'appelle déjà « {name} ».")
        if position is None:
            sibs = tx.children_of(nodes, parent_id)
            position = (max(int(s.get("position") or 0) for s in sibs) + 1) if sibs else 0
        now = _now()
        node = {"id": tx.new_cat_id(), "parent_id": parent_id, "name": name,
                "position": int(position), "created_at": now, "modified_at": now}
        _write_categories(nodes + [node])
        return node


def update_category(cat_id: str, *, name: str | None = None,
                    parent_id=UNSET, position: int | None = None) -> dict:
    """Renomme / déplace / réordonne. `parent_id=None` remonte à la racine,
    `parent_id` omis ne touche pas au parent (d'où la sentinelle UNSET).

    Ni le renommage ni le déplacement ne touchent aux affectations : les
    questions référencent l'`id`, pas le nom ni le chemin.
    """
    with _lock:
        nodes = load_categories()
        by_id = tx.index_by_id(nodes)
        if cat_id not in by_id:
            raise KeyError(cat_id)
        node = dict(by_id[cat_id])
        new_name = node["name"] if name is None else tx.clean_name(name)
        new_parent = node["parent_id"] if parent_id is UNSET else (parent_id or None)

        if new_parent != node["parent_id"]:
            if new_parent is not None:
                if not tx.is_valid_cat_id(new_parent):
                    raise tx.TaxonomyError(f"parent invalide : {new_parent!r}")
                if new_parent not in by_id:
                    raise KeyError(new_parent)
            if tx.would_create_cycle(nodes, cat_id, new_parent):
                raise tx.TaxonomyConflict(
                    f"« {node['name']} » ne peut pas être déplacée sous "
                    "elle-même ou sous l'une de ses sous-catégories.")
            # Le sous-arbre suit : vérifier la seule profondeur du nœud déplacé
            # laisserait passer un déplacement qui enfonce ses descendants.
            base = 0 if new_parent is None else tx.depth(nodes, new_parent)
            if base + tx.subtree_height(nodes, cat_id) > tx.MAX_DEPTH:
                raise tx.TaxonomyConflict(
                    f"Ce déplacement dépasserait {tx.MAX_DEPTH} niveaux.")
        if tx.sibling_conflict(nodes, new_parent, new_name, exclude_id=cat_id):
            raise tx.TaxonomyConflict(
                f"Une catégorie sœur s'appelle déjà « {new_name} ».")

        node["name"] = new_name
        node["parent_id"] = new_parent
        if position is not None:
            node["position"] = int(position)
        node["modified_at"] = _now()
        _write_categories([node if n["id"] == cat_id else n for n in nodes])
        return node


def delete_category(cat_id: str, mode: str = "refuse") -> dict:
    """Supprime un nœud. **Ne supprime jamais de question.**

    - `mode="refuse"` (défaut) : 409 si le nœud a des enfants ou des questions.
    - `mode="reparent"` : enfants et questions remontent au parent (ou perdent
      simplement la catégorie si le nœud était une racine).
    """
    if mode not in ("refuse", "reparent"):
        raise tx.TaxonomyError(f"mode inconnu : {mode!r}")
    with _lock:
        nodes = load_categories()
        by_id = tx.index_by_id(nodes)
        if cat_id not in by_id:
            raise KeyError(cat_id)
        node = by_id[cat_id]
        kids = tx.children_of(nodes, cat_id)
        members = _members_by_category().get(cat_id, set())
        if (kids or members) and mode != "reparent":
            raise tx.TaxonomyConflict(
                f"« {node['name']} » contient {len(kids)} sous-catégorie(s) "
                f"et {len(members)} question(s).")
        parent = node["parent_id"]
        rest = []
        for n in nodes:
            if n["id"] == cat_id:
                continue
            if n["parent_id"] == cat_id:
                n = {**n, "parent_id": parent, "modified_at": _now()}
            rest.append(n)
        _write_categories(rest)

        reassigned = 0
        for bid in sorted(x for x in members if x):
            try:
                q = load(bid)
            except KeyError:
                continue
            cats = [c for c in (q.get("categories") or []) if c != cat_id]
            if parent and parent not in cats:
                cats.append(parent)
            q["categories"] = cats
            save(q, reindex=False)      # un seul rebuild, à la fin
            reassigned += 1
        rebuild_index()
        return {"removed": cat_id, "reparented_children": len(kids),
                "reassigned_questions": reassigned}


def get_question_categories(bank_id: str) -> list[str]:
    """Catégories vivantes d'une question (les ids morts sont ignorés)."""
    return tx.sanitize_assignment(load(bank_id).get("categories"),
                                  load_categories())


def set_question_categories(bank_id: str, cat_ids) -> list[str]:
    """Remplace les catégories d'une question (comme `set_personal_tags`).

    ⚠ Ne touche pas à `modified_at` : classer n'est pas éditer. Sinon ranger
    une vieille question la ferait remonter en tête de la liste, qui est triée
    par date de modification.
    """
    with _lock:
        by_id = tx.index_by_id(load_categories())
        clean: list[str] = []
        for c in (cat_ids or []):
            c = str(c or "")
            if not tx.is_valid_cat_id(c):
                raise tx.TaxonomyError(f"identifiant de catégorie invalide : {c!r}")
            if c not in by_id:
                raise KeyError(c)
            if c not in clean:
                clean.append(c)
        q = load(bank_id)
        q["categories"] = clean
        save(q)
        return clean


def assign_category(cat_id: str, bank_ids, remove: bool = False) -> int:
    """Ajoute (ou retire) une catégorie sur un lot de questions. Retourne le
    nombre de questions effectivement modifiées. Un `bank_id` inconnu est
    ignoré ; un `bank_id` mal formé est une erreur."""
    with _lock:
        if not tx.is_valid_cat_id(cat_id):
            raise tx.TaxonomyError(f"identifiant de catégorie invalide : {cat_id!r}")
        if cat_id not in tx.index_by_id(load_categories()):
            raise KeyError(cat_id)
        n = 0
        for bid in (bank_ids or []):
            if not is_valid_bank_id(bid):
                raise ValueError(f"identifiant de question invalide : {bid!r}")
            try:
                q = load(bid)
            except KeyError:
                continue
            cats = list(q.get("categories") or [])
            if remove:
                if cat_id not in cats:
                    continue
                cats = [c for c in cats if c != cat_id]
            else:
                if cat_id in cats:
                    continue
                cats.append(cat_id)
            q["categories"] = cats
            save(q, reindex=False)
            n += 1
        if n:
            rebuild_index()
        return n
