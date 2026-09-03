"""Ce qu'il reste à relire sur une copie.

Ces tests fixent trois décisions qui viennent de défauts mesurés sur EXAM_2026 :
le signalement structurel est RECALCULÉ (il devenait faux dès la première
correction), les motifs d'une même case sont FUSIONNÉS (l'un masquait l'autre),
et une case traitée SORT de la file (rien ne distinguait deux passes).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import review_state as rs   # noqa: E402


SPECS = {
    1: {"type": "single", "tag": "q1", "options": ["A", "B", "C"], "correct": ["A"]},
    2: {"type": "mult", "tag": "q2", "options": ["A", "B", "C"], "correct": ["A", "B"]},
}
QS = [1, 2]


def spec_of(q):
    return SPECS[q]


def copy(answers=None, cv=None, **extra):
    d = {"answers": {str(k): v for k, v in (answers or {}).items()}}
    if cv is not None:
        d["_cv_answers"] = {str(k): v for k, v in cv.items()}
    d.update(extra)
    return d


class TestStructuralIsRecomputed(unittest.TestCase):
    def test_single_left_blank_is_one_question_level_signal(self):
        """Et NON un signalement par case : 496 des 885 cases signalées
        d'EXAM_2026 étaient les 5-6 cases d'une question laissée blanche."""
        r = rs.copy_review(copy({1: [], 2: ["A"]}), spec_of, QS)
        self.assertEqual([i["q"] for i in r["items"]], [1])
        self.assertEqual(r["items"][0]["reason"], rs.Q_NO_ANSWER)
        self.assertEqual(r["n_open"], 1)               # 1, pas 3
        self.assertEqual(len(r["items"][0]["cells"]), 3)   # les 3 cases en contexte
        self.assertFalse(any(c["flagged"] for c in r["items"][0]["cells"]))

    def test_single_with_two_answers(self):
        r = rs.copy_review(copy({1: ["A", "B"]}), spec_of, QS)
        self.assertEqual(r["items"][0]["reason"], rs.Q_MULTI_ANSWER)

    def test_mult_left_blank_is_not_signalled(self):
        self.assertEqual(rs.copy_review(copy({1: ["A"], 2: []}), spec_of, QS)["items"], [])

    def test_correcting_the_question_clears_the_signal(self):
        """Le point de la recomputation : le signal disparaît de lui-même.
        Stocké, il restait affiché après correction (constaté : 5 cases
        d'EXAM_2026 portaient encore le motif alors que la question était
        réparée)."""
        before = rs.copy_review(copy({1: []}), spec_of, QS)
        after = rs.copy_review(copy({1: ["C"]}), spec_of, QS)
        self.assertEqual(before["n_open"], 1)
        self.assertEqual(after["n_open"], 0)
        self.assertEqual(after["items"], [])

    def test_legacy_structural_cells_are_ignored(self):
        """Les JSON d'avant portent `structural` par case : ne pas le compter
        deux fois, ni le ressusciter une fois la question réparée."""
        d = copy({1: ["A"]}, _ambiguous_cells=[
            {"q": 1, "char": ch, "reasons": ["structural"]} for ch in "ABC"])
        self.assertEqual(rs.copy_review(d, spec_of, QS)["n_flagged"], 0)


class TestCellFlags(unittest.TestCase):
    def test_reasons_are_merged_not_shadowed(self):
        """Une case à la fois en désaccord AMC et douteuse n'affichait que le
        motif AMC : la déduplication de la vue jetait l'autre raison."""
        d = copy({1: ["A"]},
                 _cv_amc_diff=[{"q": 1, "char": "B", "cv": False, "amc": True}],
                 _ambiguous_cells=[{"q": 1, "char": "B", "reasons": ["disagree"],
                                    "proba": 0.44}])
        f = rs.flagged_cells(d)["1_B"]
        self.assertEqual(sorted(f["reasons"]), ["diff", "disagree"])
        self.assertEqual(f["proba"], 0.44)
        self.assertTrue(f["amc"])

    def test_flagged_cell_opens_an_item_with_context(self):
        d = copy({1: ["A"], 2: ["A"]}, _ambiguous_cells=[
            {"q": 2, "char": "C", "reasons": ["disagree"]}])
        items = rs.copy_review(d, spec_of, QS)["items"]
        self.assertEqual(len(items), 1)     # Q1 est complète : rien à en dire
        it = items[0]
        self.assertEqual(it["q"], 2)
        self.assertEqual([c["char"] for c in it["cells"]], ["A", "B", "C"])
        self.assertEqual([c["flagged"] for c in it["cells"]], [False, False, True])


class TestReviewedState(unittest.TestCase):
    def _doubtful(self, **extra):
        return copy({1: ["A"]}, _ambiguous_cells=[
            {"q": 1, "char": "B", "reasons": ["disagree"]}], **extra)

    def test_untouched_cell_stays_open(self):
        self.assertEqual(rs.copy_review(self._doubtful(), spec_of, QS)["n_open"], 1)

    def test_explicit_mark_closes_it(self):
        d = self._doubtful(_reviewed_cells=["1_B"])
        r = rs.copy_review(d, spec_of, QS)
        self.assertEqual(r["n_open"], 0)
        self.assertEqual(r["n_flagged"], 1)          # le total ne bouge pas
        self.assertTrue(r["items"][0]["cells"][1]["reviewed"])

    def test_an_edit_counts_as_a_decision(self):
        """Basculer une case EST la réponse du relecteur : pas de second clic."""
        d = copy({1: ["A", "B"]}, cv={1: ["A"]}, _ambiguous_cells=[
            {"q": 1, "char": "B", "reasons": ["disagree"]}])
        self.assertIn("1_B", rs.reviewed_cells(d))

    def test_marking_a_question_seen_does_not_touch_other_questions(self):
        d = copy({1: [], 2: []}, _reviewed_questions=[1])
        r = rs.copy_review(d, spec_of, QS)
        self.assertEqual(r["n_open"], 0)   # Q2 est `mult` → jamais signalée
        self.assertTrue(r["items"][0]["reviewed"])

    def test_review_marks_survive_an_undo(self):
        """Une case basculée puis re-basculée revient à l'état CV : sans marque
        explicite elle réapparaîtrait dans la file alors qu'on vient de
        l'examiner. `/api/toggle` pose donc la marque."""
        d = copy({1: ["A"]}, cv={1: ["A"]}, _reviewed_cells=["1_B"],
                 _ambiguous_cells=[{"q": 1, "char": "B", "reasons": ["disagree"]}])
        self.assertEqual(rs.copy_review(d, spec_of, QS)["n_open"], 0)


class TestFullyReviewed(unittest.TestCase):
    def test_validated_copy_has_nothing_open(self):
        """« Relue en entier » couvre tous les signalements — sinon une copie
        relue de bout en bout garderait ses halos et la file ne convergerait
        jamais."""
        d = copy({1: []}, _flags=["validated"], _ambiguous_cells=[
            {"q": 2, "char": "C", "reasons": ["disagree"]}])
        r = rs.copy_review(d, spec_of, QS)
        self.assertEqual(r["n_open"], 0)
        self.assertEqual(r["n_flagged"], 2)     # ils restent comptés
        self.assertEqual(r["risk"], 0.0)


class TestRisk(unittest.TestCase):
    def test_an_uncertain_cell_weighs_more_than_a_confident_one(self):
        """Le tri par risque : ce qui reste après une interruption doit être ce
        qui compte le moins. 665 des 885 cases signalées d'EXAM_2026 ont une
        probabilité GBM < 0,01 — elles ne doivent pas passer devant un vrai
        doute."""
        sure = copy({1: ["A"]}, _ambiguous_cells=[
            {"q": 1, "char": "B", "reasons": ["disagree"], "proba": 0.001}])
        unsure = copy({1: ["A"]}, _ambiguous_cells=[
            {"q": 1, "char": "B", "reasons": ["disagree"], "proba": 0.5}])
        self.assertLess(rs.copy_review(sure, spec_of, QS)["risk"],
                        rs.copy_review(unsure, spec_of, QS)["risk"])

    def test_treated_cells_carry_no_risk(self):
        d = copy({1: ["A"]}, _reviewed_cells=["1_B"], _ambiguous_cells=[
            {"q": 1, "char": "B", "reasons": ["disagree"], "proba": 0.5}])
        self.assertEqual(rs.copy_review(d, spec_of, QS)["risk"], 0.0)


class TestEmpty(unittest.TestCase):
    def test_clean_copy_has_nothing_to_show(self):
        r = rs.copy_review(copy({1: ["A"], 2: ["A", "B"]}), spec_of, QS)
        self.assertEqual((r["items"], r["n_open"], r["n_flagged"]), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
