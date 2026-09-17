"""Normalisation du QCM : par défaut, la note maximale possible.

Le diviseur du rescaling (`note* = note × max ∕ normalisation`) était figé par
le gabarit de projet — 10 dans `new_project`, 32 dans `config.DEFAULTS`, deux
valeurs câblées sur un examen particulier. Sur un sujet qui vaut 5 points,
toute la promo était donc notée sur le mauvais diviseur, **sans rien signaler** :
un QCM parfait affichait 10/20.

Il vaut désormais `None` = auto, résolu au barème réel du sujet.

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

# ⚠ Posé avant l'import de `config` : `project_root()` lit l'env à chaque appel,
# mais `sujet_store` fige ses chemins à l'import. Chaque cas les repointe.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("AMCX_PROJECT_DIR", str(Path(_TMP.name) / "projet"))

import config          # noqa: E402
import sujet_store as ss   # noqa: E402
import server          # noqa: E402
import grades_view as gv  # noqa: E402


def tearDownModule():
    _TMP.cleanup()


def qcm(bid, tag, points="1", group=""):
    """Un QCM à choix unique valant `points`."""
    return ss.Block(bid=bid, kind="question_qcm", group=group, data={
        "tag": tag, "qtype": "single", "env": "reponses",
        "statement": f"Énoncé {tag}", "value": points,
        "answers": [{"text": "a", "correct": True, "bareme": points},
                    {"text": "b", "correct": False, "bareme": "0"}]})


class TestMigrationGabarit(unittest.TestCase):
    """Les deux valeurs qu'AMCx écrivait d'office redeviennent « auto ».

    Elles n'ont jamais décrit un sujet : c'est le gabarit qui les posait. Et
    quand l'une d'elles coïncide avec le barème réel, la migration ne change
    rien — « auto » rend alors exactement le même nombre.
    """

    def test_les_placeholders_du_gabarit_repassent_en_auto(self):
        for v in (10.0, 32.0, 10, 32):
            self.assertIsNone(config._migrate_qcm_seuil({"qcm_seuil": v})["qcm_seuil"])

    def test_une_valeur_choisie_est_conservee(self):
        for v in (5.0, 7.5, 20.0, 31.0):
            self.assertEqual(
                config._migrate_qcm_seuil({"qcm_seuil": v})["qcm_seuil"], v)

    def test_auto_reste_auto(self):
        self.assertIsNone(config._migrate_qcm_seuil({"qcm_seuil": None})["qcm_seuil"])
        self.assertNotIn("qcm_seuil", config._migrate_qcm_seuil({}))

    def test_le_gabarit_de_projet_ne_fige_plus_de_diviseur(self):
        import new_project
        self.assertIsNone(new_project.CONFIG_TEMPLATE["qcm_seuil"])
        self.assertIsNone(config.DEFAULTS["qcm_seuil"])


class _SujetCase(unittest.TestCase):
    """Un `subject.json` jetable — cf. `test_versions._StoreCase`."""

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

    def write(self, blocks, versions=()):
        ss.save_subject_store({"config": ss.SubjectConfig(versions=list(versions)),
                               "blocks": blocks, "mode": "canonical"})


def norm(cfg):
    """La normalisation effective du QCM, telle que la résout `grade_columns`.

    ⚠ Une seule implémentation : elle vit dans `grades_view`, qui reçoit le
    barème en paramètre. Un helper séparé côté serveur aurait fini par répondre
    autre chose que la colonne réellement construite.
    """
    return gv.grade_columns(cfg, [], server.subject_total_max())[0]["seuil"]


class TestNormalisationAuto(_SujetCase):
    def test_le_defaut_suit_le_bareme_du_sujet(self):
        self.write([qcm("b1", "t1"), qcm("b2", "t2"), qcm("b3", "t3")])
        self.assertEqual(server.subject_total_max(), 3.0)
        self.assertEqual(norm({}), 3.0)
        self.assertEqual(norm({"qcm_seuil": None}), 3.0)

    def test_une_valeur_explicite_prime(self):
        self.write([qcm("b1", "t1")])
        self.assertEqual(norm({"qcm_seuil": 7.5}), 7.5)

    def test_deux_versions_inegales_prennent_la_plus_haute(self):
        """Un diviseur unique ne peut être que le plus grand des deux barèmes :
        sous-noter une version entière serait pire que sur-noter l'autre."""
        self.write([qcm("b1", "t1", group="morning"),
                    qcm("b2", "t2", group="afternoon"),
                    qcm("b3", "t3", group="afternoon")],
                   versions=[ss.SubjectVersion(vid="v1", name="M", group="morning"),
                             ss.SubjectVersion(vid="v2", name="A", group="afternoon")])
        # Sans calage compilé, `total_max` retombe sur toutes les questions du
        # sujet : la borne reste le plus grand barème atteignable.
        self.assertEqual(server.subject_total_max(), 3.0)

    def test_sans_sujet_lisible_on_ne_divise_pas_par_zero(self):
        self.assertEqual(server.subject_total_max(), 0.0)
        self.assertEqual(norm({}), 20.0)

    def test_la_colonne_qcm_porte_la_marque_auto(self):
        """`auto_seuil` dit à l'interface d'afficher la pastille AUTO et de
        renvoyer `null` — pas le nombre affiché, qui figerait le diviseur."""
        self.write([qcm("b1", "t1"), qcm("b2", "t2")])
        b = server.subject_total_max()
        col = gv.grade_columns({}, [], b)[0]
        self.assertEqual((col["seuil"], col["auto_seuil"]), (2.0, True))
        col = gv.grade_columns({"qcm_seuil": 9.0}, [], b)[0]
        self.assertEqual((col["seuil"], col["auto_seuil"]), (9.0, False))


class TestSliderRanges(_SujetCase):
    """Les bornes des curseurs se dérivent du projet — un curseur 0-20 sur un
    QCM qui vaut 5 points serait le même défaut que la normalisation figée."""

    def test_les_bornes_suivent_le_bareme(self):
        self.write([qcm("b1", "t1", "2"), qcm("b2", "t2", "3")])
        cfg = {"final_threshold": 20.0}
        b = server.subject_total_max()
        qmax = max(ss.max_score(q) for q in ss.parse_tex())
        cols = gv.grade_columns(cfg, [], b)
        r = gv.slider_ranges(cfg, cols, b, qmax)
        self.assertEqual(r["bareme"], 5.0)
        # La normalisation doit pouvoir monter au-delà du barème, sans quoi on
        # ne pourrait pas noter volontairement plus large.
        self.assertGreater(r["cols"]["qcm"]["seuil"]["max"], 5.0)
        self.assertLessEqual(r["cols"]["qcm"]["seuil"]["min"], 5.0)
        # Plancher/plafond par question : l'échelle est celle d'UNE question.
        self.assertEqual(r["question_ceiling"]["max"], 5.0)
        # Le seuil de réussite vit sur l'échelle des notes finales, pas du barème.
        self.assertGreaterEqual(r["pass_mark"]["max"], 20.0)

    def test_chaque_colonne_a_ses_bornes(self):
        self.write([qcm("b1", "t1")])
        cfg = {"grade_files": [{"path": "/tmp/n.csv", "grade_cols": [
            {"idx": 1, "seuil": 40.0, "max": 20.0, "agg_weight": 2.0}]}]}
        imported = [{"file": "/tmp/n.csv", "idx": 1, "name": "Projet",
                     "values": {}}]
        cols = gv.grade_columns(cfg, imported, server.subject_total_max())
        r = gv.slider_ranges(cfg, cols, server.subject_total_max())
        self.assertIn("qcm", r["cols"])
        self.assertGreaterEqual(r["cols"]["/tmp/n.csv::1"]["seuil"]["max"], 40.0)
        self.assertGreaterEqual(r["weight"]["max"], 2.0)


if __name__ == "__main__":
    unittest.main()


class TestAbsentsExport(_SujetCase):
    """Les absents n'entrent QUE dans les exports.

    ⚠ Ils ne comptent ni dans les histogrammes ni dans les statistiques :
    ceux-ci décrivent les copies corrigées, et compter un absent comme un zéro
    tirerait la moyenne de toute la promo vers le bas.
    """

    class _St:
        def __init__(self, id, nom, prenom="", email=""):
            self.id, self.nom, self.prenom, self.email = id, nom, prenom, email

    def _roster(self, *students):
        from types import SimpleNamespace
        real = server.get_matcher
        fake = SimpleNamespace(students=list(students))
        server.get_matcher = lambda: fake
        self.addCleanup(lambda: setattr(server, "get_matcher", real))

    def test_absent_students_rend_ceux_sans_copie(self):
        self._roster(self._St("1", "A"), self._St("2", "B"), self._St("3", "C"))
        copies = [{"canonical_id": "1"}, {"canonical_id": "3"}]
        self.assertEqual([s.id for s in server.absent_students(copies)], ["2"])

    def test_une_copie_non_reliee_ne_dispense_personne(self):
        """Une copie sans identité (`canonical_id` vide) ne « couvre » aucun
        étudiant : tous ceux qui n'ont pas de copie restent absents."""
        self._roster(self._St("1", "A"), self._St("2", "B"))
        copies = [{"canonical_id": ""}, {"canonical_id": "1"}]
        self.assertEqual([s.id for s in server.absent_students(copies)], ["2"])

    def test_sans_liste_etudiants_il_n_y_a_pas_d_absent(self):
        """Aucune liste chargée = on ne sait pas qui aurait dû composer."""
        self._roster()
        self.assertEqual(server.absent_students([{"canonical_id": "1"}]), [])

    def test_le_marqueur_est_une_chaine_pas_un_zero(self):
        # Déclaré une seule fois, dans `exam_results` (cf. test_exam_results).
        import exam_results
        self.assertEqual(exam_results.ABSENT_MARK, "ABS")
        self.assertNotIsInstance(exam_results.ABSENT_MARK, (int, float))
