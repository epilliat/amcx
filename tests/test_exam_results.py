"""La table des résultats d'un examen — une seule construction.

`/export.csv` et `compte_rendu/notes.csv` la bâtissaient chacun de leur côté,
avec deux tris et deux façons de marquer un absent : deux fichiers censés dire
la même chose, écrits par deux codes. [exam_results.py](auto_grading/exam_results.py)
est désormais la seule, et c'est aussi elle qu'une vue d'ensemble lira par
sous-processus.

    .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import exam_results as er   # noqa: E402


@dataclass
class St:
    id: str
    nom: str
    prenom: str
    email: str = ""


def copy(name, cid, score, *, batch="b1", page=1, validated=False,
         flags=(), email="", id_lu=""):
    return {"batch": batch, "page": page, "canonical_id": cid,
            "canonical_name": name, "canonical_email": email,
            "student_id": id_lu or cid, "score": score,
            "validated": validated, "flags": list(flags)}


class TestTable(unittest.TestCase):

    def test_une_ligne_par_copie_triee_par_nom(self):
        rows = er.student_rows([copy("ZOLA Émile", "1", 3.0),
                                copy("ADAM Ève", "2", 4.0)], [])
        self.assertEqual([r["nom_prenom"] for r in rows],
                         ["ADAM Ève", "ZOLA Émile"])

    def test_un_absent_a_sa_ligne_a_sa_place_alphabetique(self):
        """Sans elle, il disparaît du fichier remis à la scolarité et
        « absent » devient indiscernable de « oublié dans l'export »."""
        rows = er.student_rows([copy("ADAM Ève", "2", 4.0),
                                copy("ZOLA Émile", "1", 3.0)],
                               [St("3", "MOREL", "Jean")])
        self.assertEqual([r["nom_prenom"] for r in rows],
                         ["ADAM Ève", "MOREL Jean", "ZOLA Émile"])
        absent = rows[1]
        self.assertTrue(absent["absent"])
        self.assertIsNone(absent["note"])
        self.assertEqual(absent["batch"], "")

    def test_la_note_vient_de_l_appelant(self):
        """Ce module ne décide pas de la note : `server.exam_columns()` +
        `compute_aggregate` le font, et une seconde définition ici finirait
        par les contredire."""
        rows = er.student_rows([copy("ADAM Ève", "2", 4.0)], [],
                               note_of=lambda c: c["score"] * 2)
        self.assertEqual((rows[0]["brut"], rows[0]["note"]), (4.0, 8.0))

    def test_le_defaut_est_le_score_brut(self):
        rows = er.student_rows([copy("ADAM Ève", "2", 4.0)], [])
        self.assertEqual((rows[0]["brut"], rows[0]["note"]), (4.0, 4.0))


class TestCsv(unittest.TestCase):

    def setUp(self):
        self.rows = er.student_rows(
            [copy("ADAM Ève", "2", 4.0, email="e@x.fr", validated=True,
                  flags=["validated"])],
            [St("3", "MOREL", "Jean", "j@x.fr")])

    def test_un_absent_vaut_ABS_jamais_zero(self):
        """⚠ Une chaîne, jamais 0 : un absent n'a pas eu zéro, il n'a pas
        composé. Les confondre fausse toute moyenne recalculée en aval."""
        out = er.export_csv_rows(self.rows)
        absent = out[1]
        self.assertIn(er.ABSENT_MARK, absent)
        self.assertNotIn(0, absent)
        self.assertNotIn(0.0, absent)
        self.assertIsInstance(er.ABSENT_MARK, str)

    def test_les_deux_fichiers_portent_les_memes_notes(self):
        exp = er.export_csv_rows(self.rows)
        rep = er.report_csv_rows(self.rows)
        self.assertEqual(len(exp), len(rep))
        for e, r in zip(exp, rep):
            self.assertEqual(e[3], r[3])                       # nom
            self.assertEqual((e[6], e[7]), (r[5], r[6]))       # brut, note

    def test_les_entetes_gardent_leurs_noms_historiques(self):
        """32 était le barème d'EXAM_2026, pas une constante — mais des
        scripts de la scolarité et l'onglet Courriels lisent ces intitulés."""
        self.assertIn("note_sur_32", er.EXPORT_HEADER)
        self.assertIn("note_finale", er.EXPORT_HEADER)
        self.assertIn("QCM_brut_sur_32", er.REPORT_HEADER)
        self.assertIn("note_finale", er.REPORT_HEADER)

    def test_la_validation_d_un_absent_reste_vide(self):
        """« non » dirait qu'une copie existe et n'a pas été relue."""
        self.assertEqual(er.export_csv_rows(self.rows)[1][8], "")
        self.assertEqual(er.report_csv_rows(self.rows)[1][7], "")

    def test_toutes_les_lignes_ont_la_largeur_de_leur_entete(self):
        for row in er.export_csv_rows(self.rows):
            self.assertEqual(len(row), len(er.EXPORT_HEADER))
        for row in er.report_csv_rows(self.rows):
            self.assertEqual(len(row), len(er.REPORT_HEADER))


class TestSummary(unittest.TestCase):

    def test_les_absents_ne_comptent_pas_comme_des_copies(self):
        rows = er.student_rows(
            [copy("ADAM Ève", "2", 4.0, validated=True),
             copy("BREL Jacques", "", 1.0)],           # non reliée
            [St("3", "MOREL", "Jean")])
        s = er.summary(rows)
        self.assertEqual((s["n_students"], s["n_copies"], s["n_absent"]),
                         (3, 2, 1))
        self.assertEqual((s["n_validated"], s["n_unlinked"]), (1, 1))


if __name__ == "__main__":
    unittest.main()


class TestResolutionDeProjet(unittest.TestCase):
    """⚠ Un dossier qui n'est pas un projet doit le DIRE.

    Avant le contrôle, `amcx results --project <mauvais chemin>` rendait
    « 0 copie, barème 0 » avec un code de sortie 0, et un chemin inexistant
    retombait sur le dossier d'installation : un examen vide se serait glissé
    dans un relevé d'ensemble sans que rien ne le signale.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, rel):
        d = self.root / rel / "sujet"
        d.mkdir(parents=True)
        (d / "exam.tex").write_text("% sujet", encoding="utf-8")

    def test_un_dossier_de_projet_direct(self):
        self._make("p")
        self.assertEqual(er.resolve_project(self.root / "p"), self.root / "p")

    def test_le_sous_dossier_canonique_est_accepte(self):
        """`new_project.create_project(dest)` rend `dest/auto_grading` : c'est
        lui le projet, alors qu'on nomme « projet » le dossier parent."""
        self._make("p/auto_grading")
        self.assertEqual(er.resolve_project(self.root / "p"),
                         self.root / "p" / "auto_grading")

    def test_le_parent_prime_quand_les_deux_existent(self):
        self._make("p")
        self._make("p/auto_grading")
        self.assertEqual(er.resolve_project(self.root / "p"), self.root / "p")

    def test_ni_l_un_ni_l_autre_leve_en_nommant_les_deux(self):
        with self.assertRaises(er.ResultsError) as cm:
            er.resolve_project(self.root / "absent")
        msg = str(cm.exception)
        self.assertIn("sujet/exam.tex", msg)
        self.assertIn("auto_grading", msg)
