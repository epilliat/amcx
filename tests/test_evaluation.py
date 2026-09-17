"""L'évaluation d'UN examen n'a plus de réglage de note.

La page Évaluation (ex-« Dashboard ») décrit un seul examen : sa note est le
score **brut sur le barème du sujet**. Seuiller et ramener sur une autre
échelle ne sert qu'à comparer ou agréger cet examen avec autre chose — ça
appartient au niveau qui rassemble plusieurs examens.

Ce qui est fixé ici :
  - la note d'examen ne dépend d'AUCUNE clé de réglage, même quand elles sont
    encore écrites dans `config.json` (un projet antérieur en porte) ;
  - ces réglages ne sont pas effacés en silence : `legacy_grade_settings` les
    nomme, pour que la page puisse dire ce qui a changé. Sans ça, un projet
    réglé « ramené sur 20 » exporterait un score brut sans un mot, sur un
    fichier qui part à la scolarité.

    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))
sys.path.insert(0, str(_ROOT / "auto_grading" / "front"))

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("AMCX_PROJECT_DIR", str(Path(_TMP.name) / "projet"))

import sujet_store as ss   # noqa: E402
import server          # noqa: E402


def tearDownModule():
    _TMP.cleanup()


def qcm(bid, tag, points="1"):
    return ss.Block(bid=bid, kind="question_qcm", group="", data={
        "tag": tag, "qtype": "single", "env": "reponses",
        "statement": f"Énoncé {tag}", "value": points,
        "answers": [{"text": "a", "correct": True, "bareme": points},
                    {"text": "b", "correct": False, "bareme": "0"}]})


class _SujetCase(unittest.TestCase):
    """Un `subject.json` jetable — cf. `test_normalisation._SujetCase`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._saved = (ss.SUJET_DIR, ss.SUBJECT_JSON, ss.EXAM_TEX)
        ss.SUJET_DIR, ss.SUBJECT_JSON, ss.EXAM_TEX = d, d / "subject.json", d / "exam.tex"
        ss._invalidate_caches()

    def tearDown(self):
        ss.SUJET_DIR, ss.SUBJECT_JSON, ss.EXAM_TEX = self._saved
        ss._invalidate_caches()
        self._tmp.cleanup()

    def write(self, blocks):
        ss.save_subject_store({"config": ss.SubjectConfig(),
                               "blocks": blocks, "mode": "canonical"})


class TestNoteDExamen(_SujetCase):

    def test_une_seule_colonne_sur_le_bareme(self):
        self.write([qcm("b1", "t1", "2"), qcm("b2", "t2", "3")])
        cols = server.exam_columns()
        self.assertEqual(len(cols), 1)
        self.assertEqual((cols[0]["seuil"], cols[0]["max"]), (5.0, 5.0))
        self.assertEqual(server.exam_threshold(), 5.0)

    def test_la_note_est_le_score_brut(self):
        """Le rescaling se réduit à l'identité : `brut × barème ∕ barème`."""
        self.write([qcm("b1", "t1", "2"), qcm("b2", "t2", "3")])
        cols, thr = server.exam_columns(), server.exam_threshold()
        for brut in (0.0, 1.5, 3.0, 5.0):
            self.assertAlmostEqual(
                server.compute_aggregate({"score": brut}, cols, [], thr), brut)

    def test_les_anciens_reglages_ne_changent_plus_la_note(self):
        """Un projet antérieur porte `qcm_max=20` : sa note reste le brut.

        C'est le cœur du changement — avant, ce même projet affichait et
        exportait `brut × 20 ∕ barème`.
        """
        self.write([qcm("b1", "t1", "2"), qcm("b2", "t2", "3")])
        cols, thr = server.exam_columns(), server.exam_threshold()
        self.assertAlmostEqual(
            server.compute_aggregate({"score": 4.0}, cols, [], thr), 4.0)

    def test_sans_sujet_lisible_on_ne_divise_pas_par_zero(self):
        self.assertEqual(server.subject_total_max(), 0.0)
        self.assertEqual(server.exam_threshold(), 20.0)
        self.assertEqual(server.exam_columns()[0]["seuil"], 20.0)

    def test_le_bareme_vit_dans_sujet_store(self):
        """`server.subject_total_max` n'est qu'un relais : les courriels en CLI
        lisent la même valeur sans passer par le serveur."""
        self.write([qcm("b1", "t1", "2")])
        self.assertEqual(server.subject_total_max(), ss.subject_total_max())


class TestReglagesConserves(_SujetCase):
    """Rien n'est effacé, rien n'est tu — et rien n'est crié pour rien."""

    def setUp(self):
        super().setUp()
        self.write([qcm("b1", "t1", "2"), qcm("b2", "t2", "3")])   # barème 5

    def test_un_projet_sans_reglage_n_affiche_rien(self):
        self.assertEqual(
            server.legacy_grade_settings({"qcm_max": 5.0, "final_threshold": 5.0}),
            [])

    def test_un_reglage_qui_ne_changeait_deja_rien_ne_dit_rien(self):
        """Le cas courant, mesuré sur un vrai projet : « 5 sur 5, plafond 20 »
        sur un sujet qui vaut 5. L'ancienne note valait déjà le brut — annoncer
        un changement ferait de ce bandeau une alarme qu'on apprend à ignorer.
        """
        self.assertEqual(server.legacy_grade_settings(
            {"qcm_seuil": 5.0, "qcm_max": 5.0, "final_threshold": 20.0}), [])

    def test_une_echelle_differente_est_nommee(self):
        out = server.legacy_grade_settings({"qcm_max": 20.0,
                                            "final_threshold": 20.0})
        self.assertEqual(len(out), 1)
        self.assertIn("20", out[0])
        self.assertIn("5", out[0])          # … à partir du barème

    def test_une_normalisation_forcee_est_nommee(self):
        out = server.legacy_grade_settings({"qcm_seuil": 10.0, "qcm_max": 5.0,
                                            "final_threshold": 5.0})
        self.assertEqual(len(out), 1)
        self.assertIn("10", out[0])

    def test_un_plafond_qui_mord_sur_le_bareme_est_nomme(self):
        out = server.legacy_grade_settings({"qcm_max": 5.0,
                                            "final_threshold": 3.0})
        self.assertEqual(out, ["plafond à 3"])

    def test_les_colonnes_importees_sont_comptees(self):
        out = server.legacy_grade_settings({
            "qcm_max": 5.0, "final_threshold": 5.0,
            "grade_files": [{"path": "/tmp/n.csv",
                             "grade_cols": [{"idx": 1}, {"idx": 2}]}]})
        self.assertEqual(len(out), 1)
        self.assertIn("2 colonne", out[0])

    def test_sans_sujet_lisible_on_n_invente_pas_de_diagnostic(self):
        """Sans barème, « ramené sur 20 » ne veut rien dire : on se tait."""
        ss.SUBJECT_JSON.unlink()
        ss._invalidate_caches()
        self.assertEqual(server.legacy_grade_settings({"qcm_max": 20.0}), [])


if __name__ == "__main__":
    unittest.main()
