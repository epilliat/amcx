"""Import de la liste étudiants et rattachement d'une copie.

Ces tests fixent trois décisions qui viennent de défauts reproduits sur des
exports réalistes : le numéro se rattache quelle que soit la LARGEUR de la
grille (elle est réglable de 1 à 9, le matcher n'acceptait que 4) ; rien n'est
deviné en silence quand une colonne configurée a disparu ; et un suffixe
partagé par deux étudiants ne désigne personne plutôt que le premier venu.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auto_grading"))

import openpyxl                      # noqa: E402


def _fresh_modules():
    """Recharge config + student_list : la config est mise en cache au module."""
    import config
    config._config_cache = None
    import importlib
    import student_list
    importlib.reload(student_list)
    return student_list


class RosterCase(unittest.TestCase):
    """Chaque test travaille dans son propre projet jetable.

    ⚠ `unittest discover` importe tous les modules avant d'en exécuter un :
    poser AMCX_PROJECT_DIR au niveau module le poserait pour tout le monde.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("AMCX_PROJECT_DIR")
        os.environ["AMCX_PROJECT_DIR"] = str(self.dir)
        (self.dir / "sujet").mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.dir / "config.json"
        self.cfg_path.write_text("{}", encoding="utf-8")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AMCX_PROJECT_DIR", None)
        else:
            os.environ["AMCX_PROJECT_DIR"] = self._old
        self.tmp.cleanup()

    def configure(self, **kw):
        self.cfg_path.write_text(json.dumps(kw), encoding="utf-8")
        return _fresh_modules()

    def xlsx(self, name, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        p = self.dir / name
        wb.save(p)
        return p

    def csv(self, name, rows, delim=";"):
        p = self.dir / name
        with open(p, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter=delim).writerows(rows)
        return p


SCOLARITE = [
    ["Université — L2 — Session 1"],
    [],
    ["Nom", "Prénom", "N° étudiant", "Groupe"],
    ["DUPONT", "Jean", "13021", "TD1"],
    ["MARTIN", "Alice", "13022", "TD2"],
    ["BERNARD", "Luc", "13023", "TD1"],
]


class TestMatchWidth(RosterCase):
    """La grille du numéro est réglable : le rattachement doit suivre."""

    def _matcher(self):
        self.xlsx("l.xlsx", [["id", "nom", "prenom"],
                             ["13021", "DUPONT", "Jean"],
                             ["13022", "MARTIN", "Alice"]])
        sl = self.configure(student_xlsx="l.xlsx", xlsx_id_idx=0, xlsx_nom_idx=1,
                            xlsx_prenom_idx=2, xlsx_data_start=1)
        return sl.StudentMatcher()

    def test_any_grid_width_resolves(self):
        """4 chiffres marchait déjà ; 5 et 3 ne rattachaient AUCUNE copie."""
        m = self._matcher()
        for lu in ("13021", "3021", "021"):
            with self.subTest(lu=lu):
                self.assertEqual(m.resolve(lu, "")["method"], "id")
                self.assertEqual(m.by_id(lu).nom, "DUPONT")

    def test_leading_zeros_from_a_wider_grid(self):
        self.assertEqual(self._matcher().by_id("013021").nom, "DUPONT")

    def test_partial_read_matches_nothing(self):
        m = self._matcher()
        self.assertIsNone(m.by_id("30?1"))
        self.assertEqual(m.resolve("30?1", "")["method"], "none")


class TestCollisions(RosterCase):
    def _matcher(self):
        self.xlsx("l.xlsx", [["id", "nom", "prenom"],
                             ["12021", "DUPONT", "Jean"],
                             ["22021", "MARTIN", "Alice"],
                             ["13045", "BERNARD", "Luc"]])
        sl = self.configure(student_xlsx="l.xlsx", xlsx_id_idx=0, xlsx_nom_idx=1,
                            xlsx_prenom_idx=2, xlsx_data_start=1)
        return sl.StudentMatcher()

    def test_ambiguous_suffix_matches_nobody(self):
        """Attribuer au hasard entre deux étudiants est pire que ne pas
        attribuer : la copie remonte alors dans /identites."""
        m = self._matcher()
        self.assertIsNone(m.by_id("2021"))
        self.assertEqual(m.by_id("12021").nom, "DUPONT")   # le complet reste net

    def test_collision_is_announced(self):
        m = self._matcher()
        self.assertEqual(list(m.collisions(4)), ["2021"])
        self.assertTrue(any("2021" in w for w in m.warnings(4)))
        self.assertEqual(m.collisions(5), {})              # 5 chiffres séparent
        self.assertEqual(m.warnings(5), [])


class TestNoSilentGuess(RosterCase):
    def test_missing_configured_column_is_an_error(self):
        """Le repli sur les colonnes 0/1/2 donnait un identifiant « DUPONT »
        sans un mot, et l'interface annonçait « ✓ 2 étudiants »."""
        self.xlsx("l.xlsx", [["Nom", "Prénom", "N° étudiant"],
                             ["DUPONT", "Jean", "13021"]])
        sl = self.configure(student_xlsx="l.xlsx", xlsx_id_col="id_etudiant",
                            xlsx_nom_col="nom", xlsx_prenom_col="",
                            xlsx_data_start=1)
        with self.assertRaises(sl.RosterError):
            sl.load_students()
        m = sl.StudentMatcher()          # le matcher, lui, ne lève jamais
        self.assertEqual(m.students, [])
        self.assertIn("id_etudiant", m.error)

    def test_legacy_config_by_name_still_works(self):
        self.xlsx("l.xlsx", [["id_etudiant", "nom", "prenom_etat_civil"],
                             ["13021", "DUPONT", "Jean"]])
        sl = self.configure(student_xlsx="l.xlsx", xlsx_id_col="id_etudiant",
                            xlsx_nom_col="nom", xlsx_prenom_col="prenom_etat_civil",
                            xlsx_data_start=1)
        self.assertEqual([s.id for s in sl.load_students()], ["13021"])


class TestAnalyze(RosterCase):
    """Détection PAR CONTENU : on ne peut pas confronter au roster, c'est lui
    qu'on charge (`grade_imports.analyze_table` le fait, et est donc inutilisable ici)."""

    def test_school_export_with_a_title_row(self):
        sl = self.configure()
        a = sl.analyze_roster(self.csv("s.csv", SCOLARITE))
        self.assertEqual(a["suggested"], {"id_idx": 2, "nom_idx": 0,
                                          "prenom_idx": 1, "data_start": 3})

    def test_xlsx_columns_in_any_order(self):
        sl = self.configure()
        a = sl.analyze_roster(self.xlsx("d.xlsx", [
            ["Nom", "Prénom", "N° étudiant"],
            ["DUPONT", "Jean", "13021"], ["MARTIN", "Alice", "13022"]]))
        self.assertEqual(a["suggested"]["id_idx"], 2)
        self.assertEqual(a["suggested"]["data_start"], 1)

    def test_uppercase_column_is_the_family_name(self):
        sl = self.configure()
        a = sl.analyze_roster(self.xlsx("u.xlsx", [
            ["Prénom", "Nom", "Num"],
            ["Jean", "DUPONT", "13021"], ["Alice", "MARTIN", "13022"],
            ["Luc", "BERNARD", "13023"]]))
        self.assertEqual((a["suggested"]["nom_idx"], a["suggested"]["prenom_idx"]),
                         (1, 0))


class TestCsvDelimiter(RosterCase):
    def test_title_line_does_not_fool_the_sniffer(self):
        """La 1re ligne d'un export n'a souvent aucun séparateur : le deviner
        sur elle seule faisait lire tout le fichier comme UNE colonne."""
        sl = self.configure()
        a = sl.analyze_roster(self.csv("s.csv", SCOLARITE))
        self.assertEqual(a["ncol"], 4)


class TestReport(RosterCase):
    def test_counts_students_not_rows(self):
        sl = self.configure()
        p = self.csv("s.csv", SCOLARITE)
        good = sl.students_from_file(p, {"id_idx": 2, "nom_idx": 0,
                                         "prenom_idx": 1, "data_start": 3})
        self.assertEqual(sl.roster_report(good)["n_students"], 3)
        self.assertEqual(sl.roster_report(good)["problems"], [])

    def test_a_wrong_mapping_is_called_out(self):
        sl = self.configure()
        p = self.csv("s.csv", SCOLARITE)
        bad = sl.students_from_file(p, {"id_idx": 0, "nom_idx": 1,
                                        "prenom_idx": 2, "data_start": 0})
        rep = sl.roster_report(bad)
        self.assertTrue(any("ne sont pas des nombres" in x for x in rep["problems"]))

    def test_grid_wider_than_the_numbers(self):
        sl = self.configure()
        p = self.csv("s.csv", SCOLARITE)
        st = sl.students_from_file(p, {"id_idx": 2, "nom_idx": 0,
                                       "prenom_idx": 1, "data_start": 3})
        self.assertTrue(any("grille" in x for x in sl.roster_report(st, 6)["problems"]))
        self.assertEqual(sl.roster_report(st, 4)["problems"], [])

    def test_same_column_twice(self):
        sl = self.configure()
        p = self.csv("s.csv", SCOLARITE)
        st = sl.students_from_file(p, {"id_idx": 0, "nom_idx": 0,
                                       "prenom_idx": -1, "data_start": 3})
        rep = sl.roster_report(st, 4, {"id_idx": 0, "nom_idx": 0})
        self.assertTrue(any("même colonne" in x for x in rep["problems"]))

    def test_missing_column_choice_is_refused(self):
        sl = self.configure()
        p = self.csv("s.csv", SCOLARITE)
        with self.assertRaises(sl.RosterError):
            sl.students_from_file(p, {"id_idx": -1, "nom_idx": 0, "data_start": 3})


if __name__ == "__main__":
    unittest.main()
