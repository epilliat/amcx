"""Copies dont les réponses tiennent sur plusieurs feuilles.

Un sujet à beaucoup de questions déborde : AMC imprime une feuille de réponses
par groupe de questions, chacune avec son propre code (copie, page, checksum).
Le CV lit une feuille à la fois, `seed_raw_responses` les recolle en une copie.

Deux façons de relier les feuilles d'un même étudiant, très inégales :
  - exemplaires numérotés → par le n° de copie imprimé, insensible à l'ordre ;
  - un seul exemplaire → par l'ordre du scan, qui se décale si une feuille
    manque. Ces tests fixent le comportement des deux, y compris le dégradé.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auto_grading"))
sys.path.insert(0, str(ROOT / "auto_grading" / "front"))

import layout_store as ls           # noqa: E402
import seed_raw_responses as seed   # noqa: E402


def cv(sheet, copy=1, src="printed", answers=None, sid="", **extra):
    d = {"_sheet_page": sheet, "_copy_id": copy, "_copy_id_source": src,
         "answers": answers or {}, "student_id": sid, "notes": ""}
    d.update(extra)
    return d


class TestGroupSheets(unittest.TestCase):
    def test_single_sheet_is_one_copy_per_page(self):
        """Le cas courant ne doit RIEN changer : une page = une copie."""
        pages = [("b", i, cv(12)) for i in range(1, 6)]
        self.assertEqual([len(g) for g in seed.group_sheets(pages)], [1] * 5)

    def test_numbered_copies_group_by_printed_id(self):
        pages = []
        for c in (1, 2, 3):
            for sh in (15, 16):
                pages.append(("b", len(pages) + 1, cv(sh, copy=c)))
        groups = seed.group_sheets(pages)
        self.assertEqual([len(g) for g in groups], [2, 2, 2])
        for g in groups:
            self.assertEqual(len({c["_copy_id"] for _, _, c in g}), 1)

    def test_numbered_copies_survive_scan_disorder(self):
        """Toutes les feuilles 1, puis toutes les feuilles 2 : l'ordre ne compte pas."""
        pages = ([("b", i, cv(15, copy=i)) for i in (1, 2, 3)]
                 + [("b", 3 + i, cv(16, copy=i)) for i in (1, 2, 3)])
        groups = seed.group_sheets(pages)
        self.assertEqual([sorted(c["_sheet_page"] for _, _, c in g) for g in groups],
                         [[15, 16]] * 3)

    def test_single_copy_falls_back_to_scan_order(self):
        pages = [("b", i + 1, cv(sh)) for i, sh in enumerate([15, 16, 15, 16])]
        groups = seed.group_sheets(pages)
        self.assertEqual([[p for _, p, _ in g] for g in groups], [[1, 2], [3, 4]])

    def test_missing_sheet_shifts_the_rest(self):
        """Comportement DÉGRADÉ, fixé ici pour qu'il ne surprenne pas : sans
        n° de copie, une feuille manquante décale les copies suivantes."""
        pages = [("b", i + 1, cv(sh)) for i, sh in enumerate([15, 16, 16, 15, 16])]
        groups = seed.group_sheets(pages)
        self.assertEqual([[c["_sheet_page"] for _, _, c in g] for g in groups],
                         [[15, 16], [16, 15], [16]])

    def test_grid_read_copy_id_is_not_trusted_for_grouping(self):
        """Un n° de copie lu sur la grille manuelle n'a pas de checksum : on ne
        s'en sert pas pour regrouper, on retombe sur l'ordre.

        L'ordre du scan est choisi pour que les deux stratégies donnent des
        résultats DIFFÉRENTS — sinon le test ne prouverait rien.
        """
        pages = [("b", 1, cv(15, copy=1, src="grid")),
                 ("b", 2, cv(15, copy=2, src="grid")),
                 ("b", 3, cv(16, copy=1, src="grid")),
                 ("b", 4, cv(16, copy=2, src="grid"))]
        # par n° de copie, on aurait [[1, 3], [2, 4]]
        self.assertEqual([[p for _, p, _ in g] for g in seed.group_sheets(pages)],
                         [[1], [2, 3], [4]])

    def test_printed_copy_id_does_group_that_same_order(self):
        """Le même désordre, avec un n° imprimé : là, le recollage est exact."""
        pages = [("b", 1, cv(15, copy=1)), ("b", 2, cv(15, copy=2)),
                 ("b", 3, cv(16, copy=1)), ("b", 4, cv(16, copy=2))]
        self.assertEqual([[p for _, p, _ in g] for g in seed.group_sheets(pages)],
                         [[1, 3], [2, 4]])

    def test_empty(self):
        self.assertEqual(seed.group_sheets([]), [])


class TestMergeSheets(unittest.TestCase):
    def test_answers_are_unioned(self):
        g = [("b", 1, cv(15, answers={"1": ["A"], "2": ["B"]})),
             ("b", 2, cv(16, answers={"40": ["C"]}))]
        m = seed.merge_sheets(g)
        self.assertEqual(m["answers"], {"1": ["A"], "2": ["B"], "40": ["C"]})

    def test_identity_comes_from_the_sheet_that_carries_the_grid(self):
        """La grille du code étudiant n'est imprimée que sur une feuille ;
        l'autre rend une chaîne vide qui ne doit pas l'emporter."""
        g = [("b", 1, cv(15, sid="3021")), ("b", 2, cv(16, sid=""))]
        self.assertEqual(seed.merge_sheets(g)["student_id"], "3021")
        g = [("b", 1, cv(15, sid="")), ("b", 2, cv(16, sid="3021"))]
        self.assertEqual(seed.merge_sheets(g)["student_id"], "3021")

    def test_most_complete_identity_wins(self):
        g = [("b", 1, cv(15, sid="30?1")), ("b", 2, cv(16, sid="3021"))]
        self.assertEqual(seed.merge_sheets(g)["student_id"], "3021")

    def test_a_later_sheet_does_not_degrade_the_identity(self):
        """Le sens qui compte : la feuille suivante ne doit pas écraser une
        identité plus complète par une moins bonne."""
        g = [("b", 1, cv(15, sid="3021")), ("b", 2, cv(16, sid="30?1"))]
        self.assertEqual(seed.merge_sheets(g)["student_id"], "3021")
        g = [("b", 1, cv(15, sid="3021")), ("b", 2, cv(16, sid="????"))]
        self.assertEqual(seed.merge_sheets(g)["student_id"], "3021")

    def test_counts_and_flags_are_summed(self):
        g = [("b", 1, cv(15, _n_frame_fail=3, _n_cells=100,
                         _ambiguous_cells=[{"q": 1, "char": "A"}])),
             ("b", 2, cv(16, _n_frame_fail=1, _n_cells=120,
                         _ambiguous_cells=[{"q": 40, "char": "B"}]))]
        m = seed.merge_sheets(g)
        self.assertEqual((m["_n_frame_fail"], m["_n_cells"]), (4, 220))
        self.assertEqual(len(m["_ambiguous_cells"]), 2)

    def test_single_sheet_merge_is_transparent(self):
        one = cv(12, answers={"1": ["A"]}, sid="3021", notes="method=cv_full")
        m = seed.merge_sheets([("b", 1, one)])
        self.assertEqual(m["answers"], one["answers"])
        self.assertEqual(m["student_id"], "3021")
        self.assertEqual(m["notes"], "method=cv_full")

    def test_notes_are_prefixed_by_sheet_when_several(self):
        g = [("b", 1, cv(15, notes="method=cv_full")),
             ("b", 2, cv(16, notes="method=cv_frames"))]
        self.assertEqual(seed.merge_sheets(g)["notes"],
                         "f15: method=cv_full | f16: method=cv_frames")


class TestLayoutSheets(unittest.TestCase):
    """`sheet_boxes()` sans argument = toutes les feuilles ; avec `page` = une."""

    def _layout(self, pages_and_counts):
        boxes, q = [], 1
        for pg, n in pages_and_counts:
            for i in range(n):
                boxes.append(ls.Box(page=pg, role=ls.ROLE_ANSWER, question=q,
                                    answer=i, char="ABCDE"[i % 5],
                                    xmin=0, xmax=10, ymin=0, ymax=10))
            q += 1
        info = {pg: ls.PageInfo(page=pg, width=100, height=100, mark_diameter=4,
                                mires=()) for pg, _ in pages_and_counts}
        main = max(pages_and_counts, key=lambda t: t[1])[0]
        return ls.Layout(dpi=300, pages=info, boxes=boxes, zones=[],
                         answer_sheet_page=main)

    def test_single_sheet(self):
        lay = self._layout([(12, 5)])
        self.assertEqual(lay.answer_sheet_pages, (12,))
        self.assertEqual(len(lay.sheet_boxes()), 5)
        self.assertEqual(len(lay.sheet_boxes(page=12)), 5)

    def test_two_sheets(self):
        lay = self._layout([(15, 3), (16, 7)])
        self.assertEqual(lay.answer_sheet_pages, (15, 16))
        self.assertEqual(lay.answer_sheet_page, 16)      # la plus fournie
        self.assertEqual(len(lay.sheet_boxes()), 10)     # les deux
        self.assertEqual(len(lay.sheet_boxes(page=15)), 3)
        self.assertEqual(len(lay.sheet_boxes(page=16)), 7)
        self.assertTrue(lay.is_answer_sheet_page(15))
        self.assertFalse(lay.is_answer_sheet_page(14))

    def test_no_answer_boxes(self):
        lay = ls.Layout(dpi=300, pages={}, boxes=[], zones=[], answer_sheet_page=0)
        self.assertEqual(lay.answer_sheet_pages, ())
        self.assertEqual(lay.sheet_boxes(), [])


if __name__ == "__main__":
    unittest.main()
