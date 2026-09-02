"""Arbre de catégories d'une banque de questions — logique pure, sans I/O.

Un arbre est une **liste plate** de nœuds ; le lien parent-enfant est porté par
`parent_id`, jamais par de l'imbrication :

```python
{"id": "<uuid4>", "parent_id": None | "<uuid4>", "name": "Inférence",
 "position": 0, "created_at": "…", "modified_at": "…"}
```

C'est la même forme que la table Postgres `bank_categories` : les deux backends
(`bank.py` local, `bank_online.py`) partagent donc ce module, et déplacer un
nœud reste l'écriture d'un seul champ.

⚠ **L'identité d'une catégorie est son `id`, jamais son nom ni son chemin.**
Les questions référencent des ids ; renommer ou déplacer un nœud ne touche donc
à aucune affectation. Le chemin (`path`) est recalculé à l'affichage.

Toutes les fonctions ici sont pures : elles prennent la liste de nœuds en
argument et ne lisent ni n'écrivent rien. Les invariants sont vérifiés par
`validate_nodes` avant chaque écriture côté backend.
"""

from __future__ import annotations

import re
import uuid

# Profondeur maximale de l'arbre (racine = niveau 1). Le besoin décrit 2-3
# niveaux (cours → chapitre → sous-chapitre) ; au-delà, l'indentation du
# panneau de gauche (280 px) devient illisible.
MAX_DEPTH = 4

# Longueur max d'un nom de catégorie (aligné sur le `check` SQL).
NAME_MAX = 80

# UUID canonique, casse indifférente. Volontairement PLUS STRICTE que
# `bank.is_valid_bank_id`, qui accepte 36 caractères quelconques de
# `[0-9a-fA-F-]` : suffisant pour empêcher un glob `*`, pas pour interpoler
# une valeur dans une URL PostgREST.
_CAT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class TaxonomyError(ValueError):
    """Arbre ou opération invalide (→ HTTP 400)."""


class TaxonomyConflict(TaxonomyError):
    """Opération refusée par un invariant : cycle, profondeur, doublon entre
    frères, suppression d'un nœud non vide (→ HTTP 409)."""


# --------------------------------------------------------------------------
# Identifiants et noms
# --------------------------------------------------------------------------

def new_cat_id() -> str:
    """UUID v4 canonique (minuscules)."""
    return str(uuid.uuid4())


def is_valid_cat_id(cat_id) -> bool:
    return bool(_CAT_ID_RE.match(str(cat_id or "")))


def clean_name(name) -> str:
    """Normalise un nom de catégorie. Lève TaxonomyError si vide ou trop long."""
    s = " ".join(str(name or "").split())
    if not s:
        raise TaxonomyError("Le nom de la catégorie ne peut pas être vide.")
    if len(s) > NAME_MAX:
        raise TaxonomyError(f"Nom trop long ({len(s)} > {NAME_MAX} caractères).")
    return s


def _key(name: str) -> str:
    """Clé de comparaison entre frères : insensible à la casse et aux accents
    de casse (`casefold`), pour que « Inférence » et « inférence » entrent en
    conflit."""
    return clean_name(name).casefold()


# --------------------------------------------------------------------------
# Lecture de l'arbre
# --------------------------------------------------------------------------

def index_by_id(nodes) -> dict:
    return {n["id"]: n for n in (nodes or []) if n.get("id")}


def _parent_of(node) -> str | None:
    p = node.get("parent_id")
    return p or None


def children_of(nodes, parent_id=None) -> list:
    """Enfants directs d'un nœud (`None` = racines), triés `(position, nom)`."""
    kids = [n for n in (nodes or []) if _parent_of(n) == (parent_id or None)]
    return sort_siblings(kids)


def sort_siblings(nodes) -> list:
    return sorted(nodes or [],
                  key=lambda n: (int(n.get("position") or 0),
                                 str(n.get("name") or "").casefold()))


def ancestors(nodes, cat_id) -> list:
    """Ids des ancêtres, du parent direct vers la racine. Robuste à un cycle
    (s'arrête au premier id déjà vu) : cette fonction sert aussi à *détecter*
    les arbres corrompus, elle ne doit donc pas boucler dessus."""
    by_id = index_by_id(nodes)
    out: list[str] = []
    seen = {cat_id}
    cur = _parent_of(by_id.get(cat_id) or {})
    while cur and cur in by_id and cur not in seen:
        out.append(cur)
        seen.add(cur)
        cur = _parent_of(by_id[cur])
    return out


def depth(nodes, cat_id) -> int:
    """Profondeur 1-basée (une racine vaut 1)."""
    return len(ancestors(nodes, cat_id)) + 1


def path(nodes, cat_id) -> list:
    """Chemin lisible, de la racine au nœud : `["Inférence", "Tests"]`."""
    by_id = index_by_id(nodes)
    if cat_id not in by_id:
        return []
    names = [by_id[a].get("name", "") for a in ancestors(nodes, cat_id)]
    names.reverse()
    names.append(by_id[cat_id].get("name", ""))
    return names


def descendants(nodes, cat_id, include_self: bool = True) -> set:
    """Ids du sous-arbre. C'est ce qui rend le filtre « inclure les
    sous-catégories » possible côté backend comme côté PostgREST."""
    by_id = index_by_id(nodes)
    if cat_id not in by_id:
        return set()
    kids_by_parent: dict = {}
    for n in nodes:
        kids_by_parent.setdefault(_parent_of(n), []).append(n["id"])
    out: set = set()
    stack = [cat_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue          # garde-fou : un arbre corrompu ne doit pas boucler
        out.add(cur)
        stack.extend(kids_by_parent.get(cur, []))
    if not include_self:
        out.discard(cat_id)
    return out


def subtree_height(nodes, cat_id) -> int:
    """Nombre de niveaux occupés par le sous-arbre (le nœud seul → 1).

    Sert à refuser un déplacement qui ferait dépasser MAX_DEPTH *aux
    descendants* : vérifier la seule profondeur du nœud déplacé ne suffit pas.
    """
    if cat_id not in index_by_id(nodes):
        return 0
    base = depth(nodes, cat_id)
    return max(depth(nodes, d) - base + 1 for d in descendants(nodes, cat_id))


def would_create_cycle(nodes, cat_id, new_parent_id) -> bool:
    """True si rattacher `cat_id` sous `new_parent_id` crée un cycle — c.-à-d.
    si la cible est le nœud lui-même ou l'un de ses descendants."""
    if not new_parent_id:
        return False
    return new_parent_id in descendants(nodes, cat_id, include_self=True)


def sibling_conflict(nodes, parent_id, name, exclude_id=None) -> bool:
    """True si un frère porte déjà ce nom (casse indifférente)."""
    k = _key(name)
    for n in children_of(nodes, parent_id):
        if n["id"] != exclude_id and _key(n.get("name", "")) == k:
            return True
    return False


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_nodes(nodes) -> list:
    """Vérifie tous les invariants de l'arbre. Retourne les nœuds normalisés
    (noms nettoyés, `position` entière, `parent_id` à None plutôt qu'à "").

    Appelée avant CHAQUE écriture : c'est le seul endroit qui garantit qu'un
    `categories.json` édité à la main ne casse pas l'UI.
    """
    if not isinstance(nodes, list):
        raise TaxonomyError("L'arbre doit être une liste de nœuds.")
    out = []
    seen: set = set()
    for n in nodes:
        if not isinstance(n, dict):
            raise TaxonomyError("Nœud invalide (pas un objet).")
        cid = str(n.get("id") or "")
        if not is_valid_cat_id(cid):
            raise TaxonomyError(f"Identifiant de catégorie invalide : {cid!r}")
        if cid in seen:
            raise TaxonomyError(f"Identifiant dupliqué : {cid}")
        seen.add(cid)
        m = dict(n)
        m["id"] = cid
        m["name"] = clean_name(n.get("name"))
        m["parent_id"] = _parent_of(n)
        try:
            m["position"] = int(n.get("position") or 0)
        except (TypeError, ValueError):
            m["position"] = 0
        out.append(m)

    by_id = index_by_id(out)
    for n in out:
        p = n["parent_id"]
        if p is not None and p not in by_id:
            raise TaxonomyError(f"Parent inconnu pour {n['name']!r} : {p}")
        if p == n["id"]:
            raise TaxonomyConflict(f"{n['name']!r} ne peut pas être son propre parent.")

    # Cycles : `ancestors` s'arrête au premier id revu ; un cycle se trahit donc
    # par une remontée qui ne se termine pas sur une racine.
    for n in out:
        chain = ancestors(out, n["id"])
        if chain and by_id[chain[-1]]["parent_id"] is not None:
            raise TaxonomyConflict(f"Cycle détecté autour de {n['name']!r}.")
        if len(chain) + 1 > MAX_DEPTH:
            raise TaxonomyConflict(
                f"Profondeur maximale dépassée ({len(chain) + 1} > {MAX_DEPTH}).")

    # Unicité des noms entre frères (racine incluse).
    for parent in {n["parent_id"] for n in out} | {None}:
        keys: set = set()
        for kid in children_of(out, parent):
            k = _key(kid["name"])
            if k in keys:
                raise TaxonomyConflict(
                    f"Deux catégories sœurs portent le nom {kid['name']!r}.")
            keys.add(k)
    return out


# --------------------------------------------------------------------------
# Sortie pour l'UI
# --------------------------------------------------------------------------

def annotate(nodes, members=None) -> list:
    """Aplatit l'arbre en ordre préfixe, avec de quoi le rendre directement.

    `members` : `{cat_id: iterable d'identifiants de questions}` (affectations
    DIRECTES). Chaque nœud reçoit :
    - `depth`    profondeur 1-basée (pour l'indentation) ;
    - `path`     chemin lisible depuis la racine ;
    - `n_direct` questions affectées à ce nœud précis ;
    - `n_total`  questions **distinctes** de tout le sous-arbre — une question
      classée dans deux sous-catégories d'un même chapitre n'y compte qu'une
      fois (c'est un ensemble, pas une somme).
    """
    members = {k: set(v or ()) for k, v in (members or {}).items()}
    by_id = index_by_id(nodes)
    out: list = []

    def walk(parent_id, d):
        for n in children_of(nodes, parent_id):
            sub = descendants(nodes, n["id"])
            total: set = set()
            for c in sub:
                total |= members.get(c, set())
            out.append({
                **n,
                "depth":    d,
                "path":     path(nodes, n["id"]),
                "n_direct": len(members.get(n["id"], set())),
                "n_total":  len(total),
            })
            walk(n["id"], d + 1)

    walk(None, 1)
    # Un nœud orphelin (parent disparu) serait invisible : `validate_nodes`
    # l'interdit, mais l'annotation ne doit jamais perdre silencieusement une
    # ligne si l'arbre vient d'ailleurs (import, édition manuelle).
    if len(out) != len(by_id):
        placed = {n["id"] for n in out}
        for n in sort_siblings([x for x in nodes if x["id"] not in placed]):
            out.append({**n, "depth": 1, "path": [n.get("name", "")],
                        "n_direct": len(members.get(n["id"], set())),
                        "n_total": len(members.get(n["id"], set())),
                        "orphan": True})
    return out


def sanitize_assignment(cat_ids, nodes) -> list:
    """Filtre une liste d'affectations : ne garde que des ids valides et
    présents dans l'arbre, sans doublon, dans l'ordre d'apparition.

    Une catégorie supprimée laisse des ids morts sur les questions d'autres
    projets : on les **ignore en lecture** plutôt que de lever, sinon une
    question deviendrait illisible à cause d'un nœud effacé ailleurs.
    """
    by_id = index_by_id(nodes)
    out: list = []
    for c in (cat_ids or []):
        c = str(c or "")
        if is_valid_cat_id(c) and c in by_id and c not in out:
            out.append(c)
    return out
