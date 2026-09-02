"""Lecture du code d'identification imprimé en haut de page.

AMC dessine ce code en cases noircies et le calage en donne les positions : le
numéro de copie et le numéro de page sont donc lisibles sans que l'étudiant
remplisse quoi que ce soit. Les images sont synthétisées ici (pas de PDF) pour
que la suite reste rapide et sans dépendance externe.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import numpy as np              # noqa: E402
import cv_grade                 # noqa: E402
import layout_store as ls       # noqa: E402

BOX = 38          # côté d'une case, px (mesuré : 37.8 à 300 dpi)
PITCH = 39.5      # pas entre deux cases — elles se TOUCHENT presque
X0, Y0 = 700, 200
NBITS = {1: 12, 2: 6, 3: 6}


def make_layout(page_ids, pages=(1,)):
    """Layout minimal ne portant que les cases du code imprimé."""
    code = []
    for pg in pages:
        for kind in (1, 2, 3):
            # kinds 1 sur une ligne, 2 et 3 côte à côte sur la suivante
            row = 0 if kind == 1 else 1
            start = 0 if kind in (1, 2) else NBITS[2]
            for rank in range(1, NBITS[kind] + 1):
                x = X0 + (start + rank - 1) * PITCH
                y = Y0 + row * (PITCH + 10)
                code.append(ls.CodeBox(page=pg, kind=kind, rank=rank,
                                       xmin=x, xmax=x + BOX,
                                       ymin=y, ymax=y + BOX))
    lay = ls.Layout(dpi=300, pages={}, boxes=[], zones=[], answer_sheet_page=1)
    lay.code_boxes = code
    lay.page_ids = tuple(page_ids)
    return lay


def render(lay, triple, *, offset=(0, 0), ink=0, paper=255, page=1):
    """Peint le code d'un triplet sur une image blanche."""
    img = np.full((520, 1400), paper, dtype=np.uint8)
    dx, dy = offset
    vals = {1: triple[0], 2: triple[1], 3: triple[2]}
    for c in lay.code_boxes:
        if c.page != page:
            continue
        bits = format(vals[c.kind], "0%db" % NBITS[c.kind])
        on = bits[c.rank - 1] == "1"          # rang 1 = poids fort
        x1, y1 = int(c.xmin) + dx, int(c.ymin) + dy
        x2, y2 = int(c.xmax) + dx, int(c.ymax) + dy
        img[y1:y2, x1:x2] = ink if on else paper
        if not on:                             # case vide : seulement son cadre
            img[y1:y1 + 2, x1:x2] = ink
            img[y2 - 2:y2, x1:x2] = ink
            img[y1:y2, x1:x1 + 2] = ink
            img[y1:y2, x2 - 2:x2] = ink
    return img


class TestBitOrder(unittest.TestCase):
    def test_rank_one_is_most_significant(self):
        # Vérifié sur un sujet réellement compilé : copie 1 → 000000000001.
        bits = [(r, 1.0 if r == 12 else 0.0) for r in range(1, 13)]
        self.assertEqual(cv_grade._code_bits_value(bits, 0.5), 1)
        bits = [(r, 1.0 if r == 1 else 0.0) for r in range(1, 13)]
        self.assertEqual(cv_grade._code_bits_value(bits, 0.5), 2048)

    def test_checksum_sixty(self):
        bits = [(r, 1.0 if r <= 4 else 0.0) for r in range(1, 7)]
        self.assertEqual(cv_grade._code_bits_value(bits, 0.5), 60)

    def test_empty(self):
        self.assertIsNone(cv_grade._code_bits_value([], 0.5))


class TestThreshold(unittest.TestCase):
    def test_splits_on_largest_gap(self):
        thr = cv_grade._code_threshold([0.95, 0.92, 0.05, 0.03])
        self.assertTrue(0.05 < thr < 0.92, thr)

    def test_falls_back_when_no_gap(self):
        # Tous les bits identiques : aucun écart exploitable.
        self.assertEqual(cv_grade._code_threshold([0.4] * 10), 0.5)

    def test_pale_scan_still_separates(self):
        # Encre grise : les valeurs sont basses mais l'écart demeure.
        thr = cv_grade._code_threshold([0.45, 0.44, 0.04, 0.03])
        self.assertTrue(0.04 < thr < 0.44, thr)


class TestDecode(unittest.TestCase):
    def setUp(self):
        self.ids = [(1, 1, 60), (1, 2, 59), (2, 1, 58), (2, 2, 57)]
        self.lay = make_layout(self.ids)

    def test_reads_each_known_triple(self):
        for t in self.ids:
            img = render(self.lay, t)
            self.assertEqual(cv_grade.decode_page_code(img, self.lay), t)

    def test_tolerates_misalignment(self):
        # Le rattrapage balaye ±16 px : au-delà, la fenêtre de mesure mordrait
        # sur la case voisine.
        for off in ((4, 0), (0, 4), (-6, 5), (10, -8), (14, 14)):
            img = render(self.lay, (2, 1, 58), offset=off)
            self.assertEqual(cv_grade.decode_page_code(img, self.lay), (2, 1, 58),
                             f"décalage {off}")

    def test_pale_scan(self):
        # Encre à 130 : `box_fill_ratio` (seuil 128 en dur) échouerait ici.
        img = render(self.lay, (2, 2, 57), ink=130, paper=250)
        self.assertEqual(cv_grade.decode_page_code(img, self.lay), (2, 2, 57))

    def test_noise(self):
        rs = np.random.RandomState(0)
        img = render(self.lay, (1, 2, 59)).astype(np.int16)
        img = np.clip(img + rs.normal(0, 30, img.shape), 0, 255).astype(np.uint8)
        self.assertEqual(cv_grade.decode_page_code(img, self.lay), (1, 2, 59))

    def test_unknown_triple_is_refused(self):
        # Un code lisible mais absent du calage = lecture fausse, pas une
        # copie inconnue : on refuse plutôt que d'attribuer au hasard.
        img = render(self.lay, (7, 3, 11))
        self.assertIsNone(cv_grade.decode_page_code(img, self.lay))

    def test_blank_page_is_refused(self):
        img = np.full((520, 1400), 255, dtype=np.uint8)
        self.assertIsNone(cv_grade.decode_page_code(img, self.lay))

    def test_black_page_is_refused(self):
        img = np.zeros((520, 1400), dtype=np.uint8)
        self.assertIsNone(cv_grade.decode_page_code(img, self.lay))

    def test_layout_without_code_boxes(self):
        # Calage produit par une version du style qui ne trace pas le code, ou
        # layout.sqlite d'AMC : on rend la main, le repli grille prend le relais.
        lay = make_layout(self.ids)
        lay.code_boxes = []
        img = render(self.lay, (1, 1, 60))
        self.assertIsNone(cv_grade.decode_page_code(img, lay))

    def test_layout_without_known_ids(self):
        lay = make_layout([])
        img = render(lay, (1, 1, 60))
        self.assertIsNone(cv_grade.decode_page_code(img, lay))

    def test_downward_drift(self):
        # Constaté sur les 173 scans réels d'EXAM_2026 : les copies mal engagées
        # dans le chargeur dérivent de 70 à 85 px VERS LE BAS malgré le recalage
        # sur les mires. Une fenêtre symétrique de ±16 px en ratait 19 sur 173.
        for dy in (40, 60, 70, 85, 100, 118):
            img = render(self.lay, (2, 1, 58), offset=(0, dy))
            self.assertEqual(cv_grade.decode_page_code(img, self.lay), (2, 1, 58),
                             f"dérive de {dy} px vers le bas")

    def test_upward_drift_stays_narrow(self):
        # La dérive vers le haut n'a jamais été observée : le balayage y est
        # volontairement court (élargir des deux côtés quadruplerait le coût).
        img = render(self.lay, (2, 1, 58), offset=(0, -16))
        self.assertEqual(cv_grade.decode_page_code(img, self.lay), (2, 1, 58))
        img = render(self.lay, (2, 1, 58), offset=(0, -60))
        self.assertIn(cv_grade.decode_page_code(img, self.lay), (None, (2, 1, 58)))

    def test_never_returns_a_wrong_triple(self):
        # Balayage de décalages jusqu'à la rupture : lu juste, ou refusé.
        for d in range(0, 60, 3):
            img = render(self.lay, (2, 1, 58), offset=(d, d))
            got = cv_grade.decode_page_code(img, self.lay)
            self.assertIn(got, (None, (2, 1, 58)), f"décalage {d} → {got}")

    def test_wide_sweep_does_not_invent_a_valid_triple(self):
        # Le balayage large augmente les chances de tomber par hasard sur un
        # triplet connu : c'est ce que `len(found) == 1` protège. Ici la page
        # ne porte AUCUN code — aucun décalage ne doit produire de lecture.
        rs = np.random.RandomState(3)
        img = rs.randint(0, 255, (520, 1400), dtype=np.uint8)
        self.assertIsNone(cv_grade.decode_page_code(img, self.lay))


class TestLayoutParsing(unittest.TestCase):
    def test_code_boxes_sorted_by_kind_then_rank(self):
        lay = make_layout([(1, 1, 60)])
        got = [(c.kind, c.rank) for c in lay.code_boxes_on_page(1)]
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), sum(NBITS.values()))

    def test_other_pages_excluded(self):
        lay = make_layout([(1, 1, 60), (1, 2, 59)], pages=(1, 2))
        self.assertEqual(len(lay.code_boxes_on_page(2)), sum(NBITS.values()))
        self.assertEqual(len(lay.code_boxes_on_page(3)), 0)


if __name__ == "__main__":
    unittest.main()
