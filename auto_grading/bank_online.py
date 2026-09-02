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

import bank_taxonomy as tx  # noqa: E402
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
    """True si l'URL + clé anon Supabase sont posées sur la banque active."""
    entry = config.active_bank_cfg()
    return entry.get("type") == "online" and bool(
        entry.get("supabase_url") and entry.get("supabase_anon_key"))


def is_logged_in() -> bool:
    """True si un token user JWT est posé sur la banque active. (Ne valide
    pas l'expiration ici — `_request()` refresh à la volée si 401.)"""
    return bool(config.active_bank_cfg().get("user_token"))


def current_user_id() -> str | None:
    """UUID de l'user connecté sur la banque active (None si pas connecté)."""
    return config.active_bank_cfg().get("user_id") or None


def current_user_email() -> str | None:
    return config.active_bank_cfg().get("user_email") or None


def _require_auth() -> tuple[str, str, str]:
    """Retourne (base_url, anon_key, user_jwt) pour la banque active.
    Lève BankAuthError sinon."""
    entry = config.active_bank_cfg()
    url = (entry.get("supabase_url") or "").rstrip("/")
    anon = entry.get("supabase_anon_key") or ""
    jwt = entry.get("user_token") or ""
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

_SELECT_WITH_AUTHOR = ("*,author_profile:profiles!author_id(display_name,institution)"
                       ",question_categories(category_id)")


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
                        "/rest/v1/bank_questions?"
                        + urlencode({"id": f"eq.{question['bank_id']}"}),
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
    _request("DELETE", "/rest/v1/bank_questions?"
             + urlencode({"id": f"eq.{bank_id}"}))


def list_questions(filters: dict | None = None) -> list[dict]:
    """Liste les questions visibles (status='public' + les miennes).

    Filtres :
    - `q`           : substring case-insensitive sur title
    - `kind`        : type exact
    - `tags`        : list[str] - tags publics, overlap (au moins 1 commun)
    - `mes_favoris` : True → restreint à mes favoris (question_ratings)
    - `mon_tag`     : str → restreint à mes tags persos (question_personal_tags)
    - `status`      : ex 'draft' pour ne voir que mes brouillons (sinon défaut
                     = public + mes draft, géré par RLS)
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
        tags_quoted = ",".join(f'"{t}"' for t in filters["tags"] if t)
        params["tags"] = f"ov.{{{tags_quoted}}}"
    if filters.get("q"):
        params["title"] = f"ilike.*{filters['q'].strip()}*"
    if filters.get("status"):
        params["status"] = f"eq.{filters['status']}"

    # Filtres user-spécifiques : pré-fetch IDs via question_ratings/personal_tags
    restrict_ids: set[str] | None = None
    if filters.get("mes_favoris"):
        favs = _request("GET", "/rest/v1/question_ratings", params={
            "select":   "question_id",
            "user_id":  f"eq.{current_user_id()}",
            "favorite": "eq.true",
        }) or []
        restrict_ids = {f["question_id"] for f in favs}
    if filters.get("mon_tag"):
        tag = filters["mon_tag"].strip()
        ptags = _request("GET", "/rest/v1/question_personal_tags", params={
            "select":  "question_id",
            "user_id": f"eq.{current_user_id()}",
            "tags":    f'cs.{{"{tag}"}}',  # contains
        }) or []
        ids = {p["question_id"] for p in ptags}
        restrict_ids = ids if restrict_ids is None else (restrict_ids & ids)

    # Catégories : restreint aux questions du sous-arbre demandé.
    cat = (filters.get("category") or "").strip()
    if cat:
        if not tx.is_valid_cat_id(cat):
            raise tx.TaxonomyError(f"identifiant de catégorie invalide : {cat!r}")
        nodes = _raw_categories()
        wanted = (tx.descendants(nodes, cat)
                  if filters.get("descendants", True) else {cat})
        ids = _questions_in_categories(wanted)
        restrict_ids = ids if restrict_ids is None else (restrict_ids & ids)

    if restrict_ids is not None:
        if not restrict_ids:
            return []  # filtre exclusif sans match
        ids_quoted = ",".join(f'"{i}"' for i in restrict_ids)
        params["id"] = f"in.({ids_quoted})"

    rows = _request("GET", "/rest/v1/bank_questions", params=params) or []

    # Charge les évals du user courant pour TOUTES les questions en une req.
    if rows:
        ids = [r["id"] for r in rows]
        evals_by_q = _load_user_evals_bulk(ids)
        for r in rows:
            r["stats"] = {"by_project": evals_by_q.get(r["id"], {})}

    out = [_normalize_question(r) for r in rows]
    # « Sans catégorie » : filtre d'exclusion, donc côté client — `restrict_ids`
    # ne sait qu'inclure. Les affectations sont déjà embarquées, aucune requête
    # supplémentaire.
    if filters.get("uncategorized"):
        out = [q for q in out if not q.get("categories")]
    return out


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
    # Jonction embarquée → `categories: [uuid]`, même forme qu'en local.
    # Absente des réponses d'insert/update (le `select` ne s'y applique pas).
    join = out.pop("question_categories", None)
    if join is not None:
        out["categories"] = [r["category_id"] for r in join if r.get("category_id")]
    else:
        out.setdefault("categories", [])
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
            "author", "author_institution", "author_profile",
            # Affectations : table de jonction `question_categories`, pas une
            # colonne. Les laisser passer ferait échouer l'insert en PGRST204.
            "categories", "question_categories"}
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
    rows = _request("PATCH", "/rest/v1/profiles?" + urlencode({"user_id": f"eq.{uid}"}),
                    body=payload,
                    extra_headers={"Prefer": "return=representation"})
    return (rows or [{}])[0]


# --------------------------------------------------------------------------
# Phase B — Ratings, favoris, commentaires, tags persos, stats agrégées
# --------------------------------------------------------------------------

def get_my_rating(bank_id: str) -> dict:
    """Retourne mon rating sur 1 question : `{stars, favorite, comment}`,
    ou `{stars: None, favorite: False, comment: None}` si absent."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    rows = _request("GET", "/rest/v1/question_ratings", params={
        "select":      "*",
        "question_id": f"eq.{bank_id}",
        "user_id":     f"eq.{uid}",
    }) or []
    if rows:
        r = rows[0]
        return {
            "stars":    r.get("stars"),
            "favorite": bool(r.get("favorite", False)),
            "comment":  r.get("comment") or "",
        }
    return {"stars": None, "favorite": False, "comment": ""}


def rate(bank_id: str, *, stars: int | None = None,
         favorite: bool | None = None, comment: str | None = None) -> dict:
    """Upsert mon rating. Tous les params sont optionnels — seuls ceux fournis
    sont mis à jour (mais le upsert PostgREST nécessite tous les champs requis
    de la PK : on récupère donc d'abord l'existant, on fusionne, on upsert).
    """
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    if stars is not None and not (1 <= int(stars) <= 5):
        raise ValueError("stars doit être entre 1 et 5")

    existing = get_my_rating(bank_id)
    payload = {
        "question_id": bank_id,
        "user_id":     uid,
        "stars":       int(stars) if stars is not None else existing["stars"],
        "favorite":    bool(favorite) if favorite is not None else existing["favorite"],
        "comment":     comment if comment is not None else existing["comment"],
    }
    _request("POST", "/rest/v1/question_ratings", body=payload,
             extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    return {
        "stars":    payload["stars"],
        "favorite": payload["favorite"],
        "comment":  payload["comment"] or "",
    }


def delete_my_rating(bank_id: str) -> None:
    """Supprime mon rating (rare ; sert si l'user veut effacer son commentaire)."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    _request("DELETE", "/rest/v1/question_ratings", params={
        "question_id": f"eq.{bank_id}",
        "user_id":     f"eq.{uid}",
    })


def get_my_personal_tags(bank_id: str) -> list[str]:
    """Mes tags persos sur 1 question (en plus des tags publics)."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    rows = _request("GET", "/rest/v1/question_personal_tags", params={
        "select":      "tags",
        "question_id": f"eq.{bank_id}",
        "user_id":     f"eq.{uid}",
    }) or []
    return list((rows[0] or {}).get("tags") or []) if rows else []


def set_personal_tags(bank_id: str, tags: list[str]) -> list[str]:
    """Remplace mes tags persos. Vide la ligne si tags == []."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    clean = [t.strip() for t in (tags or []) if t and t.strip()]
    if not clean:
        # Supprime carrément la ligne pour ne pas garder des '{}' inutiles
        _request("DELETE", "/rest/v1/question_personal_tags", params={
            "question_id": f"eq.{bank_id}",
            "user_id":     f"eq.{uid}",
        })
        return []
    payload = {"question_id": bank_id, "user_id": uid, "tags": clean}
    _request("POST", "/rest/v1/question_personal_tags", body=payload,
             extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    return clean


def get_global_stats(bank_id: str) -> dict:
    """Stats agrégées d'une question à travers tous les users.

    - n_users / n_projects / total_n_eval / total_n_perfect / avg_normalized
      via la fonction RPC `get_question_eval_stats` (SECURITY DEFINER, bypass
      RLS sur question_evals pour ne retourner que des agrégats anonymes).
    - avg_stars + n_favorites + n_ratings : agrégation côté client depuis
      question_ratings (lecture publique).
    """
    # Eval stats via RPC
    try:
        rpc = _request("POST", "/rest/v1/rpc/get_question_eval_stats",
                       body={"qid": bank_id}) or []
        eval_stats = rpc[0] if rpc else {}
    except BankNetworkError:
        eval_stats = {}

    # Ratings stats : agrège côté client
    rt = _request("GET", "/rest/v1/question_ratings", params={
        "select":      "stars,favorite",
        "question_id": f"eq.{bank_id}",
    }) or []
    stars_vals = [int(r["stars"]) for r in rt if r.get("stars") is not None]
    n_favorites = sum(1 for r in rt if r.get("favorite"))

    return {
        "n_users":         int(eval_stats.get("n_users") or 0),
        "n_projects":      int(eval_stats.get("n_projects") or 0),
        "total_n_eval":    int(eval_stats.get("total_n_eval") or 0),
        "total_n_perfect": int(eval_stats.get("total_n_perfect") or 0),
        "avg_normalized":  eval_stats.get("avg_normalized"),
        "avg_stars":       (sum(stars_vals) / len(stars_vals)) if stars_vals else None,
        "n_ratings":       len(stars_vals),
        "n_favorites":     n_favorites,
    }


# --------------------------------------------------------------------------
# Publication (draft ↔ public) + édition d'une question existante
# --------------------------------------------------------------------------

def set_status(bank_id: str, status: str) -> dict:
    """Change le `status` d'une question (RLS : auteur seul peut)."""
    if status not in ("draft", "public", "archived"):
        raise ValueError(f"status invalide : {status}")
    rows = _request("PATCH", "/rest/v1/bank_questions?" + urlencode({"id": f"eq.{bank_id}"}),
                    body={"status": status},
                    extra_headers={"Prefer": "return=representation"})
    if not rows:
        raise BankNotFoundError(bank_id)
    return _normalize_question(rows[0])


def update_question_content(bank_id: str, data: dict, *,
                             title: str | None = None,
                             tags: list[str] | None = None,
                             bump_version: bool = True) -> dict:
    """Met à jour le contenu d'une question existante (data + title + tags).

    Utilisé par l'UI "↻ Mettre à jour dans la banque" : l'auteur édite la
    question dans un sujet, puis pousse les changements vers la version
    publiée. `bump_version` incrémente le compteur (audit léger).
    """
    payload: dict = {"data": data}
    if title is not None:
        payload["title"] = title.strip()
    if tags is not None:
        payload["tags"] = [t.strip() for t in tags if t and t.strip()]
    if bump_version:
        # PATCH avec arithmétique : pas supporté par PostgREST direct. On
        # lit l'actuelle puis +1.
        cur = _request("GET", "/rest/v1/bank_questions", params={
            "select": "version", "id": f"eq.{bank_id}"}) or []
        if cur:
            payload["version"] = int(cur[0].get("version", 1)) + 1
    rows = _request("PATCH", "/rest/v1/bank_questions?" + urlencode({"id": f"eq.{bank_id}"}),
                    body=payload,
                    extra_headers={"Prefer": "return=representation"})
    if not rows:
        raise BankNotFoundError(bank_id)
    return _normalize_question(rows[0])


# --------------------------------------------------------------------------
# Catégories hiérarchiques — parité avec bank.py (voir BANK_CATEGORIES_PLAN.md)
#
# L'arbre est un objet PARTAGÉ de la banque (tables `bank_categories` +
# `question_categories`), pas une vue par utilisateur : la vue perso reste
# `question_personal_tags`. Les invariants (cycle, profondeur, unicité entre
# frères) sont garantis par le trigger `bank_categories_check_tree` ; on les
# vérifie AUSSI ici pour rendre un message lisible plutôt qu'une erreur SQL.
# --------------------------------------------------------------------------

_CAT_SELECT = "id,parent_id,name,position,created_by,created_at,modified_at"


def _raw_categories() -> list[dict]:
    """Nœuds bruts de l'arbre (validés côté client)."""
    rows = _request("GET", "/rest/v1/bank_categories",
                    params={"select": _CAT_SELECT, "order": "position.asc,name.asc",
                            "limit": "2000"}) or []
    return tx.validate_nodes(rows)


def _in_list(ids) -> str:
    """`in.("a","b")` — les ids sont des UUID déjà validés, mais on les cite
    quand même : c'est la seule forme sûre pour un filtre PostgREST."""
    return "in.(" + ",".join(f'"{i}"' for i in sorted(ids)) + ")"


def _questions_in_categories(cat_ids) -> set:
    """Ids des questions affectées à l'une de ces catégories (RLS : seulement
    celles que l'utilisateur peut voir)."""
    if not cat_ids:
        return set()
    rows = _request("GET", "/rest/v1/question_categories",
                    params={"select": "question_id",
                            "category_id": _in_list(cat_ids),
                            "limit": "10000"}) or []
    return {r["question_id"] for r in rows}


def _category_members() -> dict:
    """`{cat_id: {question_id, …}}` pour les compteurs de l'arbre."""
    rows = _request("GET", "/rest/v1/question_categories",
                    params={"select": "category_id,question_id",
                            "limit": "10000"}) or []
    out: dict = {}
    for r in rows:
        out.setdefault(r["category_id"], set()).add(r["question_id"])
    return out


def list_categories() -> list[dict]:
    """Arbre aplati en ordre préfixe (`depth`, `path`, `n_direct`, `n_total`)."""
    return tx.annotate(_raw_categories(), _category_members())


def create_category(name: str, parent_id: str | None = None,
                    position: int | None = None) -> dict:
    """Crée un nœud. `position=None` → en dernier parmi ses frères."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    name = tx.clean_name(name)
    parent_id = parent_id or None
    nodes = _raw_categories()
    if parent_id is not None:
        if not tx.is_valid_cat_id(parent_id):
            raise tx.TaxonomyError(f"parent invalide : {parent_id!r}")
        if parent_id not in tx.index_by_id(nodes):
            raise BankNotFoundError(parent_id)
        if tx.depth(nodes, parent_id) + 1 > tx.MAX_DEPTH:
            raise tx.TaxonomyConflict(
                f"Profondeur maximale atteinte ({tx.MAX_DEPTH} niveaux).")
    if tx.sibling_conflict(nodes, parent_id, name):
        raise tx.TaxonomyConflict(f"Une catégorie sœur s'appelle déjà « {name} ».")
    if position is None:
        sibs = tx.children_of(nodes, parent_id)
        position = (max(int(s.get("position") or 0) for s in sibs) + 1) if sibs else 0
    rows = _request("POST", "/rest/v1/bank_categories",
                    body={"name": name, "parent_id": parent_id,
                          "position": int(position), "created_by": uid},
                    extra_headers={"Prefer": "return=representation"})
    if not rows:
        raise BankNetworkError("Réponse vide à la création de catégorie.")
    return rows[0]


def update_category(cat_id: str, *, name: str | None = None,
                    parent_id=tx, position: int | None = None) -> dict:
    """Renomme / déplace / réordonne.

    ⚠ La sentinelle par défaut de `parent_id` est le module `tx` lui-même —
    n'importe quelle valeur qu'un appelant ne passerait jamais. `None` doit
    rester utilisable pour dire « remonter à la racine », donc il ne peut pas
    servir de « paramètre absent ».

    Ni le renommage ni le déplacement ne touchent aux affectations : elles
    référencent l'`id`.
    """
    nodes = _raw_categories()
    by_id = tx.index_by_id(nodes)
    if cat_id not in by_id:
        raise BankNotFoundError(cat_id)
    node = by_id[cat_id]
    new_name = node["name"] if name is None else tx.clean_name(name)
    new_parent = node["parent_id"] if parent_id is tx else (parent_id or None)

    payload: dict = {}
    if new_name != node["name"]:
        payload["name"] = new_name
    if new_parent != node["parent_id"]:
        if new_parent is not None:
            if not tx.is_valid_cat_id(new_parent):
                raise tx.TaxonomyError(f"parent invalide : {new_parent!r}")
            if new_parent not in by_id:
                raise BankNotFoundError(new_parent)
        if tx.would_create_cycle(nodes, cat_id, new_parent):
            raise tx.TaxonomyConflict(
                f"« {node['name']} » ne peut pas être déplacée sous elle-même "
                "ou sous l'une de ses sous-catégories.")
        base = 0 if new_parent is None else tx.depth(nodes, new_parent)
        if base + tx.subtree_height(nodes, cat_id) > tx.MAX_DEPTH:
            raise tx.TaxonomyConflict(
                f"Ce déplacement dépasserait {tx.MAX_DEPTH} niveaux.")
        payload["parent_id"] = new_parent
    if position is not None:
        payload["position"] = int(position)
    if tx.sibling_conflict(nodes, new_parent, new_name, exclude_id=cat_id):
        raise tx.TaxonomyConflict(f"Une catégorie sœur s'appelle déjà « {new_name} ».")
    if not payload:
        return node
    rows = _request("PATCH", "/rest/v1/bank_categories?"
                    + urlencode({"id": f"eq.{cat_id}"}), body=payload,
                    extra_headers={"Prefer": "return=representation"})
    if not rows:
        # RLS : un utilisateur non connecté n'a aucune ligne à mettre à jour.
        raise BankAuthError("Modification refusée (pas connecté ?).")
    return rows[0]


def delete_category(cat_id: str, mode: str = "refuse") -> dict:
    """Supprime un nœud. **Ne supprime jamais de question.**

    ⚠ En `mode="reparent"`, les affectations posées par d'AUTRES utilisateurs
    sur ce nœud sont perdues : la RLS ne les rend ni lisibles ni supprimables,
    donc impossible de les recréer sur le parent. Le `on delete cascade` les
    efface. L'UI doit le dire dans sa confirmation.
    """
    if mode not in ("refuse", "reparent"):
        raise tx.TaxonomyError(f"mode inconnu : {mode!r}")
    nodes = _raw_categories()
    by_id = tx.index_by_id(nodes)
    if cat_id not in by_id:
        raise BankNotFoundError(cat_id)
    node = by_id[cat_id]
    kids = tx.children_of(nodes, cat_id)
    members = _questions_in_categories({cat_id})
    if (kids or members) and mode != "reparent":
        raise tx.TaxonomyConflict(
            f"« {node['name']} » contient {len(kids)} sous-catégorie(s) "
            f"et {len(members)} question(s).")

    parent = node["parent_id"]
    for kid in kids:
        _request("PATCH", "/rest/v1/bank_categories?"
                 + urlencode({"id": f"eq.{kid['id']}"}),
                 body={"parent_id": parent},
                 extra_headers={"Prefer": "return=minimal"})
    reassigned = 0
    if parent and members:
        uid = current_user_id()
        _request("POST", "/rest/v1/question_categories",
                 body=[{"question_id": q, "category_id": parent, "added_by": uid}
                       for q in sorted(members)],
                 extra_headers={"Prefer": "resolution=ignore-duplicates,"
                                          "return=minimal"})
        reassigned = len(members)
    # Le cascade de question_categories nettoie les affectations restantes.
    _request("DELETE", "/rest/v1/bank_categories?"
             + urlencode({"id": f"eq.{cat_id}"}))
    return {"removed": cat_id, "reparented_children": len(kids),
            "reassigned_questions": reassigned}


def get_question_categories(bank_id: str) -> list[str]:
    """Catégories d'une question (la FK garantit qu'elles sont vivantes)."""
    rows = _request("GET", "/rest/v1/question_categories",
                    params={"select": "category_id",
                            "question_id": f"eq.{bank_id}"}) or []
    return [r["category_id"] for r in rows]


def set_question_categories(bank_id: str, cat_ids) -> list[str]:
    """Remplace les catégories d'une question.

    ⚠ Ne touche ni à `modified_at` ni à `version` de la question : classer
    n'est pas éditer (une écriture sur `bank_questions` déclencherait
    `touch_modified_at` et ferait remonter la question en tête de liste).

    ⚠ **« Remplace » n'est pas garanti en ligne.** Un refus RLS sur un DELETE
    ne lève pas : il filtre les lignes (vérifié sur Postgres 16). Retirer le
    classement qu'un AUTRE utilisateur a posé sur la question d'un tiers est
    donc un no-op silencieux, et l'opération se comporte en fusion. D'où la
    relecture finale : on retourne l'état RÉEL, pas la liste demandée, pour que
    l'UI affiche ce qui s'est vraiment passé.
    """
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    known = tx.index_by_id(_raw_categories())
    clean: list[str] = []
    for c in (cat_ids or []):
        c = str(c or "")
        if not tx.is_valid_cat_id(c):
            raise tx.TaxonomyError(f"identifiant de catégorie invalide : {c!r}")
        if c not in known:
            raise BankNotFoundError(c)
        if c not in clean:
            clean.append(c)
    _request("DELETE", "/rest/v1/question_categories",
             params={"question_id": f"eq.{bank_id}"})
    if clean:
        _request("POST", "/rest/v1/question_categories",
                 body=[{"question_id": bank_id, "category_id": c, "added_by": uid}
                       for c in clean],
                 extra_headers={"Prefer": "resolution=ignore-duplicates,"
                                          "return=minimal"})
    return get_question_categories(bank_id)


def assign_category(cat_id: str, bank_ids, remove: bool = False) -> int:
    """Ajoute (ou retire) une catégorie sur un lot de questions. Retourne le
    nombre de questions effectivement modifiées.

    Le compte est mesuré **après coup** : un refus RLS sur DELETE est silencieux
    (0 ligne, pas d'erreur), donc annoncer le nombre demandé mentirait dès qu'on
    touche au classement d'un tiers.
    """
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    if not tx.is_valid_cat_id(cat_id):
        raise tx.TaxonomyError(f"identifiant de catégorie invalide : {cat_id!r}")
    if cat_id not in tx.index_by_id(_raw_categories()):
        raise BankNotFoundError(cat_id)
    ids = [str(b or "") for b in (bank_ids or [])]
    for b in ids:
        # Même exigence qu'en local : un id mal formé n'est pas ignoré, il est
        # refusé — il finirait interpolé dans une URL PostgREST.
        if not _is_uuid(b):
            raise ValueError(f"identifiant de question invalide : {b!r}")
    if not ids:
        return 0
    already = _questions_in_categories({cat_id})
    todo = [b for b in ids if (b in already) == bool(remove)]
    if not todo:
        return 0
    if remove:
        _request("DELETE", "/rest/v1/question_categories",
                 params={"category_id": f"eq.{cat_id}",
                         "question_id": _in_list(todo)})
    else:
        _request("POST", "/rest/v1/question_categories",
                 body=[{"question_id": b, "category_id": cat_id, "added_by": uid}
                       for b in todo],
                 extra_headers={"Prefer": "resolution=ignore-duplicates,"
                                          "return=minimal"})
    after = _questions_in_categories({cat_id})
    return len(set(todo) - after) if remove else len(after & set(todo))


def _is_uuid(v) -> bool:
    return tx.is_valid_cat_id(v)


# --------------------------------------------------------------------------
# Import (migration locale → en ligne) — voir bank_migrate.py
#
# Ces deux fonctions préservent les identifiants d'origine, contrairement à
# `create_category` qui en génère un neuf. Les ids locaux sont déjà des UUID v4
# (`bank_taxonomy.new_cat_id`), donc ils se transposent tels quels : les
# affectations d'une question migrée continuent de pointer sur les bons nœuds,
# sans table de correspondance à tenir pour les catégories.
# --------------------------------------------------------------------------

def import_category(node: dict) -> dict:
    """Insère un nœud **en conservant son `id`**. Idempotent sur l'id.

    ⚠ `ignore-duplicates` ne couvre que la clé primaire. Un conflit sur l'index
    d'unicité (deux sœurs de même nom) lève : c'est voulu, l'appelant doit le
    signaler plutôt que de fusionner deux chapitres homonymes en silence.
    """
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    cid = str(node.get("id") or "")
    if not tx.is_valid_cat_id(cid):
        raise tx.TaxonomyError(f"identifiant de catégorie invalide : {cid!r}")
    parent = node.get("parent_id") or None
    if parent is not None and not tx.is_valid_cat_id(parent):
        raise tx.TaxonomyError(f"parent invalide : {parent!r}")
    body = {"id": cid, "parent_id": parent,
            "name": tx.clean_name(node.get("name")),
            "position": int(node.get("position") or 0),
            "created_by": uid}
    rows = _request("POST", "/rest/v1/bank_categories", body=body,
                    extra_headers={"Prefer": "resolution=ignore-duplicates,"
                                             "return=representation"})
    return (rows or [body])[0]


def import_assignments(question_id: str, cat_ids) -> int:
    """Affecte une question à plusieurs catégories en une requête. Idempotent
    (clé primaire `(question_id, category_id)`). Retourne le nombre d'ids
    soumis, pas le nombre de lignes réellement créées."""
    uid = current_user_id()
    if not uid:
        raise BankAuthError("Pas connecté.")
    ids = [str(c) for c in (cat_ids or []) if tx.is_valid_cat_id(str(c))]
    if not ids:
        return 0
    _request("POST", "/rest/v1/question_categories",
             body=[{"question_id": question_id, "category_id": c, "added_by": uid}
                   for c in ids],
             extra_headers={"Prefer": "resolution=ignore-duplicates,"
                                      "return=minimal"})
    return len(ids)
