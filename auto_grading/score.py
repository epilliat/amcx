"""Calcul du score d'une copie à partir des réponses extraites par le modèle.

Entrée: dict {1: ["A","C"], 2: [], ...} (lettres cochées par question)
Sortie: dict {q: float score, "total": float}

L'ensemble des questions et le barème sont dérivés de `sujet/exam.tex`
(via `sujet_store`) — source de vérité unique. Il n'y a **aucun** repli : une
question absente du sujet ne vaut pas de points, plutôt que d'emprunter la clé
de correction d'un autre examen.
"""

from sujet_store import (amc_question_map, effective_spec, get_bareme,
                         parse_tex, total_max)


# Sentinelle : distingue « pas d'argument » (→ lire la config) de None (= aucun
# plancher). Permet aux call sites existants d'hériter automatiquement des
# planchers configurés sans aucune modification.
_UNSET = object()

# Cache (mtime, floors) pour éviter des milliers de lectures disque dans les
# boucles de notation (onglet Questions, sync banque).
# `mtime` initialisé à `_UNSET` et non à None : un projet sans config.json a
# justement un mtime None, ce qui aurait fait passer le cache vide pour frais.
_FLOORS_CACHE: dict = {"mtime": _UNSET, "floors": (None, None, None)}


def _opt_float(v):
    """None/"" → None ; sinon float (autorise négatif et zéro)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def floors_from_config() -> tuple:
    """`(question_floor, total_floor, question_ceiling)` du projet actif.

    Ce sont les défauts globaux (`None` = désactivé). Mis en cache sur le mtime
    de `config.json` (import paresseux de `config` pour éviter tout cycle).
    """
    try:
        import config  # lazy : config n'importe pas score
        path = config._config_path()
        mtime = path.stat().st_mtime if path.exists() else None
    except Exception:
        return (None, None, None)
    if _FLOORS_CACHE["mtime"] != mtime:
        try:
            cfg = config.load_config()
            _FLOORS_CACHE["floors"] = (_opt_float(cfg.get("question_floor")),
                                       _opt_float(cfg.get("total_floor")),
                                       _opt_float(cfg.get("question_ceiling")))
        except Exception:
            _FLOORS_CACHE["floors"] = (None, None, None)
        _FLOORS_CACHE["mtime"] = mtime
    return _FLOORS_CACHE["floors"]


def effective_bounds(q: int, copy: int = 1) -> tuple:
    """`(plancher, plafond)` effectifs d'une question : override du bloc
    (`floor`/`ceiling` dans exam.tex) sinon défaut global. `None` = pas de borne."""
    gf, _total, gc = floors_from_config()
    try:
        info = parse_tex().get(q) or {}
    except Exception:
        info = {}
    lo = info.get("floor")
    hi = info.get("ceiling")
    return (gf if lo is None else lo, gc if hi is None else hi)


def question_set() -> list[int]:
    """Numéros des questions QCM à noter, dérivés du sujet."""
    try:
        return sorted(parse_tex().keys())
    except Exception:
        return []


def score_question(q: int, selected: list[str], copy: int = 1,
                   floor=_UNSET, ceiling=_UNSET) -> float:
    """Score d'une question pour une copie donnée.

    `copy=1` par défaut → JSON legacy (sans `_copy_id`) continue à fonctionner.
    `floor`/`ceiling` : bornes par question (points barème). `_UNSET` → résolues
    via `effective_bounds` (override du bloc sinon défaut global) ; `None` → borne
    désactivée. Le score final est clampé `min(max(s, floor), ceiling)`.
    """
    # `q` est un numéro de question du calage AMC ; le sujet indexe ses QCM à
    # part. La carte est l'identité dans le cas normal, mais elle écarte les
    # cases de notation des questions ouvertes (non notées automatiquement).
    qmap = amc_question_map(copy)["qcm"]
    q = qmap.get(q, q if not qmap else None)
    if q is None:
        return 0.0
    if floor is _UNSET or ceiling is _UNSET:
        ef, ec = effective_bounds(q, copy)
        if floor is _UNSET:
            floor = ef
        if ceiling is _UNSET:
            ceiling = ec
    # type / options / correct dérivés d'exam.tex pour CETTE copie
    spec = effective_spec(q, copy=copy)
    bareme = get_bareme(copy=copy).get(q, {})
    correct = set(spec["correct"])
    sel = set(selected or [])
    # filtrer les lettres hors-options (sécurité contre hallucinations modèle)
    sel &= set(spec["options"])

    if spec["type"] == "single":
        # `value` pt ssi exactement la bonne lettre est cochée (rien d'autre)
        s = float(bareme.get("value", 1.0)) if sel == correct else 0.0
    else:
        # mult — barème par réponse : points propres à chaque case cochée
        # Un mauvais cochage (points négatifs) peut rendre la question négative.
        chars = bareme.get("chars")
        # Pas de barème exploitable dans le sujet (points manquants sur une
        # réponse) → la question ne vaut rien. On ne devine pas : un barème
        # inventé produirait des notes fausses et silencieuses.
        s = round(sum(chars.get(letter, 0.0) for letter in sel), 6) if chars else 0.0

    # Bornes par question (plancher/plafond) — désactivées par défaut.
    if floor is not None:
        s = max(s, float(floor))
    if ceiling is not None:
        s = min(s, float(ceiling))
    return s


def score_copy(answers: dict[int, list[str]], copy: int = 1, copy_floor=_UNSET) -> dict:
    """Note d'une copie. `copy=1` par défaut (rétrocompat JSON legacy).

    Les bornes par question (plancher/plafond, override ou global) sont gérées
    dans `score_question`. `copy_floor` = plancher global du total de copie
    (`total_floor`) : `_UNSET` → lu depuis la config ; `None` → désactivé.
    Ordre : bornes par question → somme → plancher global de copie.
    """
    if copy_floor is _UNSET:
        copy_floor = floors_from_config()[1]
    per_q = {q: score_question(q, answers.get(q, []), copy=copy)
             for q in question_set()}
    total = round(sum(per_q.values()), 4)
    if copy_floor is not None:
        total = max(total, float(copy_floor))
    return {"per_question": per_q, "total": total}


if __name__ == "__main__":
    qset = question_set()
    # auto-test: copie parfaite = total max du barème courant
    perfect = {q: list(effective_spec(q)["correct"]) for q in qset}
    r = score_copy(perfect)
    print("Copie parfaite:", r["total"], "/ total_max =", total_max())
    assert abs(r["total"] - total_max()) < 1e-6, (r["total"], total_max())

    # copie vide = 0
    empty = {q: [] for q in qset}
    r = score_copy(empty)
    print("Copie vide:", r["total"])
    assert r["total"] == 0.0

    # cocher tout: les single donnent 0 (pas exact), les mult cumulent malus (sans plancher)
    all_ticked = {q: list(effective_spec(q)["options"]) for q in qset}
    r = score_copy(all_ticked)
    print("Tout coché:", r["total"])

    # une mauvaise réponse seule sur une question mult donne bien un score négatif
    mult_qs = [q for q in qset if effective_spec(q)["type"] == "mult"]
    if mult_qs:
        neg = {q: [] for q in qset}
        wrong_q = mult_qs[0]
        spec = effective_spec(wrong_q)
        wrong = [c for c in spec["options"] if c not in spec["correct"]]
        if wrong:
            neg[wrong_q] = [wrong[0]]
            r = score_copy(neg)
            print(f"Une mauvaise case (Q{wrong_q}):", r["total"])
    print("OK")
