"""Référence masquée : elle doit décrire LA COPIE qu'on a sous les yeux.

Deux défauts corrigés ici, tous deux silencieux et tous deux mesurés sur un
vrai lot de copies (39 pages, sujet à 2 versions, `shuffle_answers` actif) :

  - `ref_frames` était indexé par `(question, answer)`. `answer` est l'ordre de
    déclaration LaTeX, permuté d'une copie à l'autre, alors que la case `A` de
    la question 1 est toujours au même endroit sur la feuille. Dès que la copie
    scannée n'était pas la copie 1, la table rendait donc le cadre d'une AUTRE
    case (jusqu'à 300 px plus loin) : le masque d'encre imprimée tombait à
    côté, la mesure masquée devenait du bruit — **23 cases pourtant noircies à
    plus de 50 % étaient lues « non cochées »**, et 27 % des cases vides
    dépassaient le seuil d'encre E1, donc signalées « douteuses » pour rien.

  - sans mesure masquée, le GBM décidait quand même. Il est entraîné avec ces
    features toujours présentes ; sur une ligne où elles manquent, sa
    probabilité s'effondre vers ~0,42 — « non cochée », quelle que soit la
    noirceur. `decide_cell` rend alors la main au seuil.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import cv_grade                 # noqa: E402
import masked_detect            # noqa: E402

NAN = float("nan")


class Box:
    """Case de calage, réduite à ce que `sheet_signature` regarde."""

    def __init__(self, question, char, answer, xmin, ymin):
        self.question, self.char, self.answer = question, char, answer
        self.xmin, self.ymin = xmin, ymin
        self.xmax, self.ymax = xmin + 38, ymin + 38


class FakeLayout:
    def __init__(self, boxes, page=2, pdf=2):
        self._boxes, self._page, self._pdf = boxes, page, pdf
        self.answer_sheet_page = page

    def sheet_boxes(self, page=None):
        return list(self._boxes)

    def pdf_page(self, page):
        return self._pdf


def sheet(perm, page=2, pdf=2):
    """Une feuille : mêmes positions, mapping lettre↔réponse `perm`."""
    return FakeLayout([Box(1, ch, perm[i], 693 + 75 * i, 2351)
                       for i, ch in enumerate("ABCDE")], page, pdf)


class TestSheetSignature(unittest.TestCase):
    """La clé du cache : la géométrie, pas le numéro de copie."""

    def test_deux_copies_dune_meme_version_partagent_la_reference(self):
        # Même feuille, réponses permutées : re-rendre le PDF pour chaque copie
        # scannée coûterait un rendu 300 dpi par page corrigée.
        a = masked_detect.sheet_signature(sheet([2, 3, 5, 4, 1]))
        b = masked_detect.sheet_signature(sheet([1, 3, 5, 2, 4]))
        self.assertEqual(a, b)

    def test_deux_versions_ont_des_references_distinctes(self):
        # Version 2 : autres questions, autres ordonnées → autre feuille.
        v2 = FakeLayout([Box(10, ch, i + 1, 693 + 75 * i, 2287)
                         for i, ch in enumerate("ABCDEF")])
        self.assertNotEqual(masked_detect.sheet_signature(sheet([2, 3, 5, 4, 1])),
                            masked_detect.sheet_signature(v2))


class TestDecideCell(unittest.TestCase):
    """Sans mesure masquée, le GBM est hors de son domaine."""

    def test_masquee_absente_le_seuil_reprend_la_main(self):
        ticked, proba, no_masked = cv_grade.decide_cell(0.42, True, NAN)
        self.assertTrue(ticked)          # le GBM disait « vide » sur une case pleine
        self.assertIsNone(proba)         # sa probabilité ne veut plus rien dire
        self.assertTrue(no_masked)       # et la case part en relecture

    def test_masquee_absente_sans_desaccord_ne_signale_rien(self):
        # Signaler toutes les cases sans mesure masquée remplirait la file.
        self.assertEqual(cv_grade.decide_cell(0.42, False, NAN), (False, None, False))

    def test_mesure_presente_le_gbm_decide(self):
        self.assertEqual(cv_grade.decide_cell(0.42, True, 0.9), (False, 0.42, False))
        self.assertEqual(cv_grade.decide_cell(0.93, False, 0.9), (True, 0.93, False))

    def test_sans_modele_le_seuil_seul(self):
        self.assertEqual(cv_grade.decide_cell(None, True, NAN), (True, None, False))
        self.assertEqual(cv_grade.decide_cell(None, False, 0.9), (False, None, False))


if __name__ == "__main__":
    unittest.main()
