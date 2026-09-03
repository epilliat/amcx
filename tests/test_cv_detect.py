"""Détection des mires, recalage local et mesure de remplissage.

Pages synthétisées avec OpenCV (aucun PDF, aucun TeX) : 4 disques aux
positions canoniques d'EXAM_2026, des carrés noirs là où AMC en imprime (code
en haut de page, cases cochées), puis une homographie aléatoire connue. On
vérifie que les mires sont retrouvées au pixel près, qu'un carré n'est jamais
pris pour une mire, et qu'une mire manquante donne `None` — pas un carré.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import cv2                      # noqa: E402
import numpy as np              # noqa: E402
import cv_grade                 # noqa: E402
import layout_store as ls       # noqa: E402

W, H = 2480, 3508                                   # A4 à 300 dpi
MIRES = np.array([[317, 292], [2164, 292], [2164, 3309], [317, 3309]], np.float32)
DIAM = 42.5
BOX = 38


def blank():
    return np.full((H, W), 255, np.uint8)


def draw_mires(img, which=(0, 1, 2, 3)):
    for i in which:
        cv2.circle(img, tuple(int(round(v)) for v in MIRES[i]), int(DIAM / 2), 0, -1)


def draw_code_bits(img, bits="101101001011", x0=709, y0=196):
    """Le code imprimé : 12 cases de 38 px qui se touchent presque, en haut."""
    for i, b in enumerate(bits):
        x = int(x0 + i * 39.5)
        if b == "1":
            cv2.rectangle(img, (x, y0), (x + BOX, y0 + BOX), 0, -1)
        else:
            cv2.rectangle(img, (x, y0), (x + BOX, y0 + BOX), 0, 2)


def draw_box(img, x, y, filled, side=BOX, thick=2):
    cv2.rectangle(img, (x, y), (x + side, y + side), 0, -1 if filled else thick)


def random_homography(rng, max_shift=40, max_persp=2e-5):
    """Petite déformation projective : décalage, rotation, perspective."""
    src = np.array([[0, 0], [W, 0], [W, H], [0, H]], np.float32)
    dst = src + rng.uniform(-max_shift, max_shift, src.shape).astype(np.float32)
    Hm, _ = cv2.findHomography(src, dst)
    return Hm


def apply(img, Hm):
    return cv2.warpPerspective(img, Hm, (W, H), borderValue=255)


def project(pts, Hm):
    p = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), Hm)
    return p.reshape(-1, 2)


def make_layout():
    """Layout minimal : mires canoniques, diamètre, une page."""
    page = ls.PageInfo(page=1, width=W, height=H, mark_diameter=DIAM,
                       mires=tuple(map(tuple, MIRES.tolist())))
    return ls.Layout(dpi=300, pages={1: page}, boxes=[], zones=[], answer_sheet_page=1)


class TestDetectMires(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.lay = make_layout()

    def assertMires(self, found, expected, tol=1.5):
        self.assertIsNotNone(found)
        self.assertEqual(found.shape, (4, 2))
        err = np.linalg.norm(found - expected, axis=1)
        self.assertLess(err.max(), tol, f"écart max {err.max():.2f} px : {err}")

    def test_clean_page(self):
        img = blank(); draw_mires(img)
        self.assertMires(cv_grade.detect_mires(img), MIRES)

    def test_under_homography_with_squares(self):
        """Carrés noirs partout (code imprimé, cases cochées) + déformation."""
        for _ in range(5):
            img = blank(); draw_mires(img); draw_code_bits(img)
            for _k in range(60):
                x, y = self.rng.integers(300, W - 340), self.rng.integers(700, H - 340)
                draw_box(img, int(x), int(y), filled=bool(self.rng.integers(0, 2)))
            Hm = random_homography(self.rng)
            warped = apply(img, Hm)
            self.assertMires(cv_grade.detect_mires(warped), project(MIRES, Hm))

    def test_missing_mire_is_none_not_a_square(self):
        """Sans la mire haut-gauche, le bit de code le plus proche est à 736 px
        du coin — sous l'ancien seuil de 744. Il ne doit PAS devenir la mire."""
        img = blank(); draw_mires(img, which=(1, 2, 3)); draw_code_bits(img)
        # et une case cochée dans le coin, pour faire bonne mesure
        draw_box(img, 200, 600, filled=True)
        self.assertIsNone(cv_grade.detect_mires(img))

    def test_two_missing_is_none(self):
        img = blank(); draw_mires(img, which=(0, 2)); draw_code_bits(img)
        self.assertIsNone(cv_grade.detect_mires(img))

    def test_mire_glued_to_dark_border_is_none(self):
        """Bord de scan noir qui touche la mire haut-gauche : le contour fusionne
        avec le bord et n'est plus un disque → None, pas une autre forme."""
        img = blank(); draw_mires(img); draw_code_bits(img)
        img[:, :300] = 0                        # bande noire à gauche, jusqu'à la mire
        self.assertIsNone(cv_grade.detect_mires(img))

    def test_pale_scan(self):
        """Encre grise et papier gris : Otsu doit s'en sortir."""
        img = blank(); draw_mires(img); draw_code_bits(img)
        pale = (img.astype(np.float32) * 0.45 + 120).astype(np.uint8)   # noir→120, blanc→235
        self.assertMires(cv_grade.detect_mires(pale), MIRES)

    def test_with_layout_hint(self):
        """Avec le calage, la recherche se limite aux coins attendus."""
        img = blank(); draw_mires(img); draw_code_bits(img)
        Hm = random_homography(self.rng)
        warped = apply(img, Hm)
        self.assertMires(cv_grade.detect_mires(warped, layout=self.lay), project(MIRES, Hm))


class TestRefineBoxOffset(unittest.TestCase):
    # `cv2.rectangle` centre son trait sur les coordonnées : le contour d'un
    # cadre d'épaisseur 2 déborde d'un pixel → tolérance de 1 px.
    def assertOffset(self, got, want, delta=1):
        self.assertLessEqual(abs(got[0] - want[0]), delta, f"{got} ≠ {want}")
        self.assertLessEqual(abs(got[1] - want[1]), delta, f"{got} ≠ {want}")

    def test_recovers_known_offset(self):
        img = blank()
        box = ls.Box(page=1, role=1, question=3, answer=1, char="A",
                     xmin=1000, xmax=1038, ymin=1500, ymax=1538)
        for dx, dy in ((0, 0), (7, -5), (-12, 15), (20, 20)):
            im = img.copy()
            draw_box(im, 1000 + dx, 1500 + dy, filled=False)
            self.assertOffset(cv_grade.refine_box_offset(im, box), (dx, dy))

    def test_filled_box_still_found(self):
        img = blank()
        box = ls.Box(page=1, role=1, question=3, answer=1, char="A",
                     xmin=1000, xmax=1038, ymin=1500, ymax=1538)
        draw_box(img, 1006, 1497, filled=True)
        self.assertOffset(cv_grade.refine_box_offset(img, box), (6, -3))

    def test_nothing_nearby_gives_zero(self):
        img = blank()
        box = ls.Box(page=1, role=1, question=3, answer=1, char="A",
                     xmin=1000, xmax=1038, ymin=1500, ymax=1538)
        self.assertEqual(cv_grade.refine_box_offset(img, box), (0, 0))

    def test_median_per_question(self):
        """Une case dont le contour est aberrant ne doit pas entraîner la question."""
        img = blank()
        boxes = [ls.Box(page=1, role=1, question=5, answer=k, char=c,
                        xmin=1000 + 60 * k, xmax=1038 + 60 * k, ymin=1500, ymax=1538)
                 for k, c in enumerate("ABCDE")]
        for b in boxes:
            draw_box(img, int(b.xmin) + 4, int(b.ymin) + 9, filled=False)
        cv2.rectangle(img, (1000 + 4, 1500 + 9), (1000 + 4 + 30, 1500 + 9 + 30), 0, -1)  # bavure sur A
        offs = cv_grade.compute_per_question_offsets(img, boxes)
        self.assertOffset(offs[5], (4, 9))


class TestFillRatio(unittest.TestCase):
    def setUp(self):
        self.box = ls.Box(page=1, role=1, question=1, answer=1, char="A",
                          xmin=1000, xmax=1038, ymin=1500, ymax=1538)

    def test_filled_and_empty(self):
        img = blank()
        self.assertEqual(cv_grade.box_fill_ratio(img, self.box), 0.0)
        draw_box(img, 1000, 1500, filled=True)
        self.assertGreater(cv_grade.box_fill_ratio(img, self.box), 0.95)

    def test_empty_frame_is_excluded_by_shrink(self):
        img = blank(); draw_box(img, 1000, 1500, filled=False)
        self.assertLess(cv_grade.box_fill_ratio(img, self.box), 0.05)

    def test_offset_applied(self):
        img = blank(); draw_box(img, 1010, 1512, filled=True)
        without = cv_grade.box_fill_ratio(img, self.box)
        with_off = cv_grade.box_fill_ratio(img, self.box, offset=(10, 12))
        self.assertLess(without, 0.9)
        self.assertGreater(with_off, 0.95)
        self.assertLess(without, with_off)


if __name__ == "__main__":
    unittest.main()
