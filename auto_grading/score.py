"""Calcul du score d'une copie à partir des réponses extraites par le modèle.

Entrée: dict {1: ["A","C"], 2: [], ...} (lettres cochées par question)
Sortie: dict {q: float score, "total": float}

L'ensemble des questions et le barème sont dérivés de `sujet/exam.tex`
(via `sujet_store`) ; `answer_key.py` n'est qu'un repli structurel.
"""

from sujet_store import effective_spec, get_bareme, parse_tex, total_max

try:                                  # repli structurel — absent sur un projet vierge
    from answer_key import ANSWER_KEY
except Exception:
    ANSWER_KEY = {}


def question_set() -> list[int]:
    """Numéros des questions QCM à noter (depuis exam.tex, repli answer_key.py)."""
    try:
        qs = sorted(parse_tex().keys())
        if qs:
            return qs
    except Exception:
        pass
    return sorted(ANSWER_KEY.keys())


def score_question(q: int, selected: list[str], copy: int = 1) -> float:
    """Score d'une question pour une copie donnée.

    `copy=1` par défaut → JSON legacy (sans `_copy_id`) continue à fonctionner.
    """
    # type / options / correct dérivés d'exam.tex pour CETTE copie (repli answer_key.py)
    spec = effective_spec(q, copy=copy)
    bareme = get_bareme(copy=copy).get(q, {})
    correct = set(spec["correct"])
    sel = set(selected or [])
    # filtrer les lettres hors-options (sécurité contre hallucinations modèle)
    sel &= set(spec["options"])

    if spec["type"] == "single":
        # `value` pt ssi exactement la bonne lettre est cochée (rien d'autre)
        return float(bareme.get("value", 1.0)) if sel == correct else 0.0

    # mult — barème par réponse : points propres à chaque case cochée
    chars = bareme.get("chars")
    if chars:
        return round(sum(chars.get(letter, 0.0) for letter in sel), 6)

    # repli : barème par question d'answer_key.py
    ak = ANSWER_KEY.get(q, {})
    b, m = ak.get("b", 0.0), ak.get("m", 0.0)
    s = 0.0
    for letter in spec["options"]:
        if letter in sel:
            s += b if letter in correct else m
    # Pas de plancher : un mauvais cochage (malus m<0) peut rendre la question négative.
    return round(s, 6)


def score_copy(answers: dict[int, list[str]], copy: int = 1) -> dict:
    """Note d'une copie. `copy=1` par défaut (rétrocompat JSON legacy)."""
    per_q = {q: score_question(q, answers.get(q, []), copy=copy)
             for q in question_set()}
    total = round(sum(per_q.values()), 4)
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
