"""Client HTTP pour la banque AMCx en ligne (Supabase).

Réplique l'API publique de [bank.py](bank.py) (local, disque) pour le
backend en ligne. Tape sur PostgREST de Supabase via JWT utilisateur.

Fonctions publiques (mêmes signatures que `bank.py`) :
- `load(bank_id) -> dict`
- `save(question) -> dict`     (insert ou update selon présence d'id)
- `delete(bank_id) -> None`
- `list_questions(filters) -> list[dict]`
- `update_project_stats(bank_id, project_name, n_eval, ...)`
- `stats_summary(question) -> dict`  (réutilise bank.stats_summary)
- `from_block(...)`, `to_block(...)` réutilisés depuis bank.py

Différences avec le backend local :
- `bank_id` = UUID v4 (36 chars) au lieu de 8 hex.
- `stats.by_project` n'est PAS embarqué dans la question : 1 ligne par
  (question, user, projet) dans la table `question_evals`. `load()` et
  `list_questions()` rapatrient les évals du user courant et reconstruisent
  un `stats.by_project` synthétique pour compat UI.
- `status` ∈ {draft, public, archived} : seuls les 'public' (de tous) +
  les questions de l'auteur sont visibles (RLS côté Supabase).

Réseau : `urllib.request` (stdlib, zéro dep). Auth : JWT user dans
`config.bank_user_token`, refresh transparent via `bank_auth.refresh_token`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# Le package est plat : ajoute le dossier parent au sys.path pour `import config`.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import config  # noqa: E402

# Helpers communs réutilisés depuis bank.py (transformations de Block).
from bank import (  # noqa: E402,F401
    from_block,
    to_block,
    stats_summary,
    _auto_title_from_data,
)


class BankAuthError(RuntimeError):
    """Levée quand l'user n'est pas connecté ou que son token est invalide."""


class BankNetworkError(RuntimeError):
    """Levée quand Supabase est injoignable (réseau, 5xx)."""


class BankNotFoundError(KeyError):
    """Levée quand une question demandée n'existe pas (404)."""


# --------------------------------------------------------------------------
# Config / auth
# --------------------------------------------------------------------------

def is_configured() -> bool:
    """True si l'URL + clé anon Supabase sont posées dans la config."""
    cfg = config.load_config()
    return bool(cfg.get("bank_supabase_url") and cfg.get("bank_supabase_anon_key"))


def is_logged_in() -> bool:
    """True si un token user JWT est posé. (Ne valide pas l'expiration ici —
    `_request()` refresh à la volée si 401.)"""
    return bool(config.load_config().get("bank_user_token"))


def current_user_id() -> str | None:
    """UUID de l'user connecté (None si pas connecté)."""
    return config.load_config().get("bank_user_id") or None


def current_user_email() -> str | None:
    return config.load_config().get("bank_user_email") or None


def _require_auth() -> tuple[str, str, str]:
    """Retourne (base_url, anon_key, user_jwt). Lève BankAuthError sinon."""
    cfg = config.load_config()
    url = (cfg.get("bank_supabase_url") or "").rstrip("/")
    anon = cfg.get("bank_supabase_anon_key") or ""
    jwt = cfg.get("bank_user_token") or ""
    if not (url and anon):
        raise BankAuthError("Banque en ligne non configurée (URL + clé anon manquantes).")
    if not jwt:
        raise BankAuthError("Non connecté à la banque en ligne.")
    return url, anon, jwt


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

def _request(method: str, path: str, *, body: Any = None, params: dict | None = None,
             extra_headers: dict | None = None, retry_on_401: bool = True) -> Any:
    """Requête HTTP vers Supabase. Retourne le body décodé (dict/list) ou None.

    - `path` : ex. "/rest/v1/bank_questions" (sans l'URL de base).
    - 401 → tente un refresh du JWT (via bank_auth) puis retry une fois.
    - 404 → BankNotFoundError. 5xx/réseau → BankNetworkError.
    """
    base, anon, jwt = _require_auth()
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)

    headers = {
        "apikey":        anon,
        "Authorization": f"Bearer {jwt}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as e:
        if e.code == 401 and retry_on_401:
            # JWT expiré ? on tente un refresh + retry une fois.
            try:
                import bank_auth
                if bank_auth.refresh_token_if_possible():
                    return _request(method, path, body=body, params=params,
                                    extra_headers=extra_headers, retry_on_401=False)
            except Exception:
                pass
            raise BankAuthError("Session expirée — se reconnecter.") from e
        if e.code == 404:
            raise BankNotFoundError(path) from e
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise BankNetworkError(f"HTTP {e.code} sur {path} : {err_body[:200]}") from e
    except URLError as e:
        raise BankNetworkError(f"Réseau injoignable : {e.reason}") from e


# --------------------------------------------------------------------------
# CRUD questions
# --------------------------------------------------------------------------

_SELECT_WITH_AUTHOR = "*,author_profile:profiles!author_id(display_name,institution)"


def load(bank_id: str) -> dict:
    """Charge 1 question par UUID. Lève BankNotFoundError si absente.

    JOIN avec profiles pour exposer `author` = display_name (compat UI).
    Greffe `stats.by_project` reconstruit depuis question_evals (mes propres
    évals seulement — RLS).
    """
    rows = _request("GET", "/rest/v1/bank_questions",
                    params={"select": _SELECT_WITH_AUTHOR, "id": f"eq.{bank_id}"})
    if not rows:
        raise BankNotFoundError(bank_id)
    q = _normalize_question(rows[0])
    q["stats"] = {"by_project": _load_user_evals(bank_id)}
    return q


def save(question: dict) -> dict:
    """Insert (si pas de `bank_id`) ou update (si présent) une question.

    Retourne la question telle que renvoyée par le serveur (avec id généré
    le cas échéant). Compat avec `bank.save()` qui retourne un Path : on
    retourne ici la question complète à la place.
    """
    payload = _question_to_row(question)
    if question.get("bank_id"):
        # Update
        rows = _request("PATCH",
                        f"/rest/v1/bank_questions?id=eq.{question['bank_id']}",
                        body=payload,
                        extra_headers={"Prefer": "return=representation"})
    else:
        # Insert
        uid = current_user_id()
        if not uid:
            raise BankAuthError("user_id absent — se reconnecter.")
        payload["author_id"] = uid
        rows = _request("POST", "/rest/v1/bank_questions", body=payload,
                        extra_headers={"Prefer": "return=representation"})
    if not rows:
        raise BankNetworkError("Réponse vide à l'insert/update.")
    return _normalize_question(rows[0])


def delete(bank_id: str) -> None:
    """Supprime définitivement (RLS : seul l'auteur peut)."""
    _request("DELETE", f"/rest/v1/bank_questions?id=eq.{bank_id}")


def list_questions(filters: dict | None = None) -> list[dict]:
    """Liste les questions visibles (status='public' + les miennes).

    Filtres : `q`, `kind`, `tags` (list), `author` (substring sur display_name
    via une jointure profiles — pour MVP on filtre côté client).
    """
    filters = filters or {}
    params = {
        "select": _SELECT_WITH_AUTHOR,
        "order":  "modified_at.desc",
        "limit":  "500",
    }
    if filters.get("kind"):
        params["kind"] = f"eq.{filters['kind']}"
    if filters.get("tags"):
        # PostgREST array overlap : tags=ov.{t1,t2,...}
        tags_quoted = ",".join(f'"{t}"' for t in filters["tags"] if t)
        params["tags"] = f"ov.{{{tags_quoted}}}"
    if filters.get("q"):
        q = filters["q"].strip()
        # ilike (case-insensitive substring) sur title
        params["title"] = f"ilike.*{q}*"

    rows = _request("GET", "/rest/v1/bank_questions", params=params) or []

    # Charge les évals du user courant pour TOUTES les questions en une req.
    if rows:
        ids = [r["id"] for r in rows]
        evals_by_q = _load_user_evals_bulk(ids)
        for r in rows:
            r["stats"] = {"by_project": evals_by_q.get(r["id"], {})}

    return [_normalize_question(r) for r in rows]


# --------------------------------------------------------------------------
# Stats (question_evals)
# --------------------------------------------------------------------------

def update_project_stats(bank_id: str, project_name: str, n_eval: int,
                          sum_normalized: float, n_perfect: int,
                          max_score_at_sync: float) -> None:
    """Upsert dans question_evals : (question_id, user_id, project_name) unique."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("user_id absent.")
    payload = {
        "question_id":       bank_id,
        "user_id":           uid,
        "project_name":      project_name,
        "n_eval":            int(n_eval),
        "sum_normalized":    round(float(sum_normalized), 4),
        "n_perfect":         int(n_perfect),
        "max_score_at_sync": round(float(max_score_at_sync), 4),
    }
    # Upsert via Prefer: resolution=merge-duplicates (sur la contrainte unique)
    _request("POST", "/rest/v1/question_evals", body=payload,
             extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


def _load_user_evals(bank_id: str) -> dict[str, dict]:
    """Charge les évals du user courant pour 1 question. Retourne
    `{project_name: {n_eval, sum_normalized, n_perfect, max_score_at_sync, last_sync}}`."""
    uid = current_user_id()
    if not uid:
        return {}
    rows = _request("GET", "/rest/v1/question_evals", params={
        "select":      "*",
        "question_id": f"eq.{bank_id}",
        "user_id":     f"eq.{uid}",
    }) or []
    return {r["project_name"]: _eval_row_to_summary(r) for r in rows}


def _load_user_evals_bulk(bank_ids: list[str]) -> dict[str, dict[str, dict]]:
    """Idem pour N questions en 1 req. Retourne `{bank_id: {project: summary}}`."""
    uid = current_user_id()
    if not uid or not bank_ids:
        return {}
    ids_quoted = ",".join(f'"{i}"' for i in bank_ids)
    rows = _request("GET", "/rest/v1/question_evals", params={
        "select":      "*",
        "question_id": f"in.({ids_quoted})",
        "user_id":     f"eq.{uid}",
    }) or []
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["question_id"], {})[r["project_name"]] = _eval_row_to_summary(r)
    return out


def _eval_row_to_summary(row: dict) -> dict:
    return {
        "n_eval":            int(row.get("n_eval", 0)),
        "sum_normalized":    float(row.get("sum_normalized", 0)),
        "n_perfect":         int(row.get("n_perfect", 0)),
        "max_score_at_sync": float(row.get("max_score_at_sync") or 0),
        "last_sync":         row.get("last_sync", ""),
    }


# --------------------------------------------------------------------------
# Normalisation question Supabase → format UI/bank.py
# --------------------------------------------------------------------------

def _normalize_question(row: dict) -> dict:
    """Convertit une ligne Supabase au format attendu par l'UI / bank.py.

    - Mapping `id` → `bank_id`.
    - Extrait le `display_name` du JOIN `author_profile` → champ `author`
      (compat UI : la modale Banque affiche `author` comme texte).
    - `stats.by_project` est posé séparément par `load()` / `list_questions()`.
    """
    out = dict(row)
    if "id" in out and "bank_id" not in out:
        out["bank_id"] = out["id"]
    # JOIN profiles : extrait display_name + institution
    prof = out.pop("author_profile", None) or {}
    out["author"] = prof.get("display_name") or out.get("author") or ""
    out["author_institution"] = prof.get("institution") or ""
    # L'UI attend `stats.by_project` ; si pas encore greffé, default vide.
    out.setdefault("stats", {"by_project": {}})
    return out


def _question_to_row(question: dict) -> dict:
    """Convertit une question (format bank.py) en payload PostgREST.

    On retire les champs non-colonnes : `bank_id`/`id` (gérés par Supabase),
    `stats` (table dédiée), `created_at`/`modified_at` (triggers serveur),
    `author`/`author_institution` (calculés via JOIN profiles), `author_profile`
    (résultat de JOIN).
    """
    skip = {"bank_id", "id", "stats", "created_at", "modified_at",
            "author", "author_institution", "author_profile"}
    return {k: v for k, v in question.items() if k not in skip}


# --------------------------------------------------------------------------
# Profil utilisateur (display_name + institution)
# --------------------------------------------------------------------------

def get_my_profile() -> dict:
    """Retourne mon profil `{user_id, display_name, institution}` ou {} si absent."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    rows = _request("GET", "/rest/v1/profiles",
                    params={"select": "*", "user_id": f"eq.{uid}"})
    return (rows or [{}])[0]


def update_my_profile(updates: dict) -> dict:
    """Met à jour mon profil (PATCH). Retourne le profil mis à jour."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    payload = {}
    if "display_name" in updates:
        v = (updates["display_name"] or "").strip()
        if not v:
            raise ValueError("display_name ne peut pas être vide")
        payload["display_name"] = v
    if "institution" in updates:
        payload["institution"] = (updates["institution"] or "").strip()
    if not payload:
        return get_my_profile()
    rows = _request("PATCH", f"/rest/v1/profiles?user_id=eq.{uid}",
                    body=payload,
                    extra_headers={"Prefer": "return=representation"})
    return (rows or [{}])[0]
