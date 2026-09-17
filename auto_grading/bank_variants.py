"""Variantes d'une même question de banque — **logique pure, zéro I/O**.

Deux QCM qui posent la **même** question à des valeurs près (le sujet du matin
et celui de l'après-midi, le même énoncé repris d'une année sur l'autre) ne
sont ni un doublon ni deux questions : ce sont des **variantes**. La banque
n'en affiche qu'une — sinon la liste triple et on ne voit plus le cours —, et
l'on peut déplier le groupe pour choisir celle qu'on veut imprimer.

Le modèle est volontairement minimal : chaque question porte un champ
`variant_of`, qui vaut `""` si elle est **chef** de son groupe (cas par défaut,
y compris pour une question seule) et sinon le `bank_id` du chef.

⚠ **Pas de chaîne.** Attacher A à B alors que B est déjà une variante range A
sous le **chef** de B. Une arborescence obligerait chaque page à remonter les
parents pour répondre à « quelles sont les variantes de cette question ? », et
deux pages finiraient par en compter deux nombres différents.

⚠ **Attacher une question qui a déjà des variantes fusionne les deux groupes**
(`attach`). Laisser ses variantes derrière elle créerait un second chef portant
exactement le même énoncé : le doublon que ce module existe pour éviter.

⚠ **Supprimer un chef ne supprime jamais ses variantes** : la plus ancienne est
promue et les autres la suivent (`promote_on_delete`). Même règle que pour les
catégories — aucune question n'est jamais supprimée implicitement.

⚠ **Un pointeur mort ou un cycle est RÉPARÉ à la lecture, pas levé**
(`normalize`). Ces fichiers se suppriment à la main, se synchronisent par git,
se restaurent depuis une sauvegarde : une banque ne doit pas devenir illisible
parce qu'un `variant_of` désigne un fichier absent.

Les entrées manipulées ici sont des dicts `{bank_id, variant_of, created_at}` —
c'est le sous-ensemble commun à une question complète et à une entrée d'index,
donc le module sert les deux sans les connaître.
"""

from __future__ import annotations


def _bid(row) -> str:
    return (row.get("bank_id") or "").strip()


def _ptr(row) -> str:
    return (row.get("variant_of") or "").strip()


def _order_key(row) -> tuple:
    """Ancienneté, puis identifiant — pour que l'ordre ne dépende pas du disque."""
    return (row.get("created_at") or "", _bid(row))


def index_by_id(rows) -> dict:
    return {_bid(r): r for r in rows if _bid(r)}


def normalize(rows) -> dict:
    """`{bank_id: variant_of corrigé}` pour **toutes** les lignes.

    Répare, dans cet ordre : pointeur vers un inconnu, auto-référence, cycle,
    chaîne (A → B → C devient A → C). Ne touche pas au disque : l'appelant
    décide s'il persiste (`bank.repair_variants()`) ou se contente de lire
    juste.
    """
    by_id = index_by_id(rows)

    def terminal(start: str) -> str:
        """Le chef du groupe de `start`, en remontant les pointeurs."""
        cur, seen = start, [start]
        while True:
            nxt = _ptr(by_id[cur])
            if not nxt or nxt not in by_id:
                return cur                      # chef, ou pointeur mort ignoré
            if nxt in seen:
                # Cycle : on le coupe sur son maillon le plus ancien, plutôt
                # que de rendre invisible tout le groupe.
                ring = seen[seen.index(nxt):]
                return min(ring, key=lambda b: _order_key(by_id[b]))
            cur = nxt
            seen.append(cur)

    fixed: dict[str, str] = {}
    for r in rows:
        me = _bid(r)
        if not me:
            continue
        head = terminal(me)
        fixed[me] = "" if head == me else head
    return fixed


def head_of(rows, bank_id: str) -> str:
    """Le chef du groupe de `bank_id` (lui-même s'il est chef ou inconnu)."""
    fixed = normalize(rows)
    return fixed.get(bank_id) or bank_id


def members(rows, bank_id: str) -> list[str]:
    """Le groupe entier : le chef d'abord, puis ses variantes par ancienneté."""
    fixed = normalize(rows)
    head = fixed.get(bank_id) or bank_id
    by_id = index_by_id(rows)
    if head not in by_id:
        return [bank_id] if bank_id else []
    vars_ = sorted((by_id[b] for b, h in fixed.items() if h == head and b in by_id),
                   key=_order_key)
    return [head] + [_bid(v) for v in vars_]


def attach(rows, bank_id: str, head_id: str) -> dict:
    """Ce qu'il faut écrire pour faire de `bank_id` une variante de `head_id`.

    Retourne `{bank_id: variant_of}` **limité à ce qui change** — les groupes
    fusionnés peuvent représenter plusieurs écritures, et réécrire tout le
    reste ferait remonter des questions intactes en tête de la liste (triée par
    date de modification).

    Lève `ValueError` si l'opération n'a pas de sens : question inconnue, ou
    tentative de se rattacher à son propre groupe (qui ne ferait rien tout en
    ayant l'air d'agir).
    """
    by_id = index_by_id(rows)
    if bank_id not in by_id:
        raise ValueError(f"question inconnue : {bank_id}")
    if head_id not in by_id:
        raise ValueError(f"question inconnue : {head_id}")
    fixed = normalize(rows)
    target = fixed.get(head_id) or head_id          # jamais une chaîne
    if target == bank_id or (fixed.get(bank_id) or bank_id) == target:
        raise ValueError("cette question est déjà dans ce groupe de variantes")
    out = {}
    if fixed.get(bank_id, "") != target:
        out[bank_id] = target
    for b, h in fixed.items():                      # ses variantes la suivent
        if h == bank_id and h != target:
            out[b] = target
    return out


def detach(rows, bank_id: str) -> dict:
    """Ce qu'il faut écrire pour sortir `bank_id` de son groupe.

    Une **variante** redevient une question autonome. Un **chef** n'a rien à
    détacher : c'est `promote_on_delete` qui traite sa disparition, et
    « détacher le chef » disperserait le groupe sans que personne ne l'ait
    demandé.
    """
    fixed = normalize(rows)
    if bank_id not in fixed:
        raise ValueError(f"question inconnue : {bank_id}")
    if not fixed[bank_id]:
        raise ValueError("cette question est le chef de son groupe : rien à détacher")
    return {bank_id: ""}


def promote_on_delete(rows, bank_id: str) -> dict:
    """Ce qu'il faut écrire **avant** de supprimer `bank_id`.

    Si c'est un chef, sa variante la plus ancienne devient chef et les autres
    la suivent. Si c'est une variante, rien à faire.
    """
    fixed = normalize(rows)
    by_id = index_by_id(rows)
    kids = sorted((by_id[b] for b, h in fixed.items()
                   if h == bank_id and b in by_id), key=_order_key)
    if not kids:
        return {}
    new_head = _bid(kids[0])
    out = {new_head: ""}
    for k in kids[1:]:
        out[_bid(k)] = new_head
    return out


def fold(items) -> list[dict]:
    """Replie une liste d'entrées : ne garde que les chefs, chacun annoté.

    Chaque chef reçoit `variants` (ses variantes, par ancienneté) et
    `n_variants`. Une variante dont le chef **ne fait pas partie de la liste**
    reçue — c'est le cas d'un filtre qui ne retient qu'elle — est rendue
    telle quelle, en chef de fait : la faire disparaître serait pire, elle
    deviendrait introuvable alors qu'elle correspond à la recherche.
    """
    fixed = normalize(items)
    by_id = index_by_id(items)
    kids: dict[str, list] = {}
    for it in items:
        h = fixed.get(_bid(it), "")
        if h and h in by_id:
            kids.setdefault(h, []).append(it)
    out = []
    for it in items:
        me = _bid(it)
        h = fixed.get(me, "")
        if h and h in by_id:
            continue                                 # variante : repliée
        vs = sorted(kids.get(me, []), key=_order_key)
        out.append({**it, "variants": vs, "n_variants": len(vs)})
    return out


def expand_matches(all_rows, matched_ids) -> set:
    """Les groupes touchés par une recherche, chefs compris.

    ⚠ Un filtre qui porte sur une **variante** doit ramener son groupe : sinon
    la variante est repliée sous un chef que le filtre n'a pas retenu, et elle
    disparaît de l'écran — introuvable alors qu'elle correspond exactement à ce
    qu'on cherchait.
    """
    fixed = normalize(all_rows)
    keep = set()
    for b in matched_ids:
        keep.add(fixed.get(b) or b)
    return keep
