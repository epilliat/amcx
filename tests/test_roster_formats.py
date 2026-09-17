"""Souplesse du format de la liste étudiants — ce qui doit continuer à passer.

La détection est **par contenu** : ni un intitulé de colonne, ni une position,
ni une extension ne sont imposés. Ces cas fixent l'étendue de cette tolérance,
pour qu'une future retouche de la détection (ou du choix d'onglet) ne la rétré-
cisse pas en silence. Ils sont tirés d'exports de scolarité réels.

    .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))

import openpyxl                        # noqa: E402
import student_list as sl              # noqa: E402


class FormatCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def book(self, rows, name="Liste", ext=".xlsx"):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = name
        for r in rows:
            ws.append(list(r))
        p = self.dir / ("f" + ext)
        wb.save(p)
        return p

    def text(self, content, enc="utf-8", name="f.csv"):
        p = self.dir / name
        p.write_text(content, encoding=enc)
        return p

    def load(self, path, sheet=None):
        """Le chemin complet de l'import : analyse puis construction."""
        a = sl.analyze_roster(path, sheet)
        return sl.students_from_file(path, {**a["suggested"], "sheet": a["sheet"]})

    def assertRoster(self, path, sheet=None, n=3, first=("3021", "DUPONT", "Jean")):
        st = self.load(path, sheet)
        self.assertEqual(len(st), n)
        self.assertEqual((st[0].id, st[0].nom, st[0].prenom), first)


ROWS = [("nom", "prenom", "id"), ("DUPONT", "Jean", 3021),
        ("MARTIN", "Luc", 3022), ("DURAND", "Eva", 3023)]


class TestFormatsDeFichier(FormatCase):
    def test_csv_virgule(self):
        self.assertRoster(self.text("nom,prenom,id\nDUPONT,Jean,3021\n"
                                    "MARTIN,Luc,3022\nDURAND,Eva,3023\n"))

    def test_csv_point_virgule_precede_d_un_titre(self):
        """⚠ Le séparateur se devine sur PLUSIEURS lignes : un export commence
        souvent par un titre sans séparateur, et tout le fichier était alors lu
        comme une seule colonne."""
        self.assertRoster(self.text(
            "Export scolarité du 14/09/2026\nnom;prenom;id\n"
            "DUPONT;Jean;3021\nMARTIN;Luc;3022\nDURAND;Eva;3023\n"))

    def test_csv_tabulations_avec_BOM(self):
        self.assertRoster(self.text(
            "nom\tprenom\tid\nDUPONT\tJean\t3021\nMARTIN\tLuc\t3022\n"
            "DURAND\tEva\t3023\n", enc="utf-8-sig"))

    def test_xlsx(self):
        self.assertRoster(self.book(ROWS))

    def test_xlsm(self):
        self.assertRoster(self.book(ROWS, ext=".xlsm"))


class TestFormesDeTableau(FormatCase):
    def test_colonnes_dans_n_importe_quel_ordre(self):
        self.assertRoster(self.book([
            ("id", "prenom", "nom", "voie"),
            (3021, "Jean", "DUPONT", "2A ING"),
            (3022, "Luc", "MARTIN", "2A ING"),
            (3023, "Eva", "DURAND", "2A ATT")]))

    def test_une_colonne_de_courriels_n_est_pas_une_colonne_de_noms(self):
        """⚠ Une adresse sans chiffre passe le test « alphabétique ». Quand le
        courriel s'intercale entre le numéro et le nom, c'est LUI qui était
        proposé comme nom de famille — plausible dans l'aperçu, et faux."""
        self.assertRoster(self.book([
            ("id", "courriel", "prenom", "nom"),
            (3021, "j.d@x.fr", "Jean", "DUPONT"),
            (3022, "l.m@x.fr", "Luc", "MARTIN"),
            (3023, "e.d@x.fr", "Eva", "DURAND")]))

    def test_plusieurs_lignes_de_titre_au_dessus_de_l_en_tete(self):
        self.assertRoster(self.book([
            ("ENSAI — promotion 2A", None, None), (None, None, None),
            ("Extrait du 14/09", None, None), *ROWS]))

    def test_colonnes_vides_a_gauche(self):
        """Le gabarit d'un export réel : trois colonnes vides avant les données."""
        self.assertRoster(self.book([
            (None, None, "nom", "prenom", "id"),
            (None, None, "DUPONT", "Jean", 3021),
            (None, None, "MARTIN", "Luc", 3022),
            (None, None, "DURAND", "Eva", 3023)]))

    def test_aucun_en_tete(self):
        st = self.load(self.book(ROWS[1:]))
        self.assertEqual(len(st), 3)
        self.assertEqual(st[0].nom, "DUPONT")

    def test_sans_colonne_prenom(self):
        st = self.load(self.book([("nom", "id"), ("DUPONT Jean", 3021),
                                  ("MARTIN Luc", 3022), ("DURAND Eva", 3023)]))
        self.assertEqual([(s.id, s.nom, s.prenom) for s in st][0],
                         ("3021", "DUPONT Jean", ""))

    def test_lignes_vides_intercalees(self):
        self.assertRoster(self.book([ROWS[0], ROWS[1], (None, None, None),
                                     ROWS[2], ROWS[3]]))

    def test_noms_accentues_traits_d_union_apostrophes(self):
        st = self.load(self.book([
            ("nom", "prenom", "id"),
            ("O'BRIEN-DUPONT", "Jean-Éric", 3021),
            ("ÉTIENNE", "Noëlle", 3022), ("MÜLLER", "Zoé", 3023)]))
        self.assertEqual([s.nom for s in st],
                         ["O'BRIEN-DUPONT", "ÉTIENNE", "MÜLLER"])


class TestLargeurDesNumeros(FormatCase):
    """⚠ Rien n'impose 4 chiffres : `StudentMatcher.by_id` essaie l'identifiant
    complet puis le suffixe de la largeur lue par la grille."""

    def test_numeros_a_cinq_chiffres_avec_zero_de_tete(self):
        st = self.load(self.book([
            ("nom", "prenom", "id"), ("DUPONT", "Jean", "03021"),
            ("MARTIN", "Luc", "03022"), ("DURAND", "Eva", "03023")]))
        self.assertEqual([s.id for s in st], ["03021", "03022", "03023"])

    def test_numeros_longs(self):
        st = self.load(self.book([
            ("nom", "prenom", "id"), ("DUPONT", "Jean", 2024001234),
            ("MARTIN", "Luc", 2024001235), ("DURAND", "Eva", 2024001236)]))
        self.assertEqual(st[0].id, "2024001234")


class TestCeQuiExigeUnCoupDeMain(FormatCase):
    """Ce que la détection ne devine PAS — mais qui reste chargeable à la main
    depuis la modale. L'important est le message : il doit dire quoi faire."""

    def test_identifiant_non_numerique_se_choisit_a_la_main(self):
        p = self.book([("nom", "prenom", "matricule"),
                       ("DUPONT", "Jean", "E3021"), ("MARTIN", "Luc", "E3022"),
                       ("DURAND", "Eva", "E3023")])
        with self.assertRaises(sl.RosterError) as cm:
            self.load(p)
        self.assertIn("colonne du numéro", str(cm.exception))
        st = sl.students_from_file(p, {"id_idx": 2, "nom_idx": 0,
                                       "prenom_idx": 1, "data_start": 1})
        self.assertEqual([s.id for s in st], ["E3021", "E3022", "E3023"])
        # …et le contrôle prévient que ces identifiants ne sont pas des nombres.
        self.assertTrue(any("ne sont pas des nombres" in p
                            for p in sl.roster_report(st, 4)["problems"]))

    def test_fichier_vide_dit_qu_il_est_vide(self):
        with self.assertRaises(sl.RosterError) as cm:
            self.load(self.book([]))
        self.assertIn("vide", str(cm.exception))

    def test_liste_sans_numero_du_tout(self):
        with self.assertRaises(sl.RosterError) as cm:
            self.load(self.book([("nom",), ("DUPONT",), ("MARTIN",)]))
        self.assertIn("colonne du numéro", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class TestCourriel(FormatCase):
    """Le courriel voyage avec l'étudiant jusqu'à l'export CSV.

    ⚠ Il ne sert **jamais** au rattachement d'une copie — rien ne l'écrit sur
    une feuille de réponses. C'est une donnée de sortie : pouvoir renvoyer les
    notes sans re-croiser la liste à la main.
    """

    MAILED = [("nom", "prenom", "mail", "id"),
              ("DUPONT", "Jean", "j.dupont@x.fr", 3021),
              ("MARTIN", "Luc", "l.martin@x.fr", 3022),
              ("DURAND", "Eva", "e.durand@x.fr", 3023)]

    def test_la_colonne_courriel_est_reconnue_par_les_arobases(self):
        a = sl.analyze_roster(self.book(self.MAILED))
        self.assertEqual(a["suggested"]["mail_idx"], 2)
        self.assertTrue(a["columns"][2]["looks_mail"])

    def test_le_courriel_arrive_sur_l_etudiant(self):
        st = self.load(self.book(self.MAILED))
        self.assertEqual([s.email for s in st],
                         ["j.dupont@x.fr", "l.martin@x.fr", "e.durand@x.fr"])

    def test_une_liste_sans_courriel_reste_valide(self):
        """Pas de repli : aucune colonne à « @ » ⇒ aucune proposition, et
        l'étudiant porte simplement un courriel vide."""
        a = sl.analyze_roster(self.book(ROWS))
        self.assertEqual(a["suggested"]["mail_idx"], -1)
        self.assertEqual([s.email for s in self.load(self.book(ROWS))], ["", "", ""])

    def test_la_colonne_courriel_ne_se_devine_pas_sur_une_seule_ligne(self):
        """Une seule cellule à « @ » (un contact en pied de tableau, p. ex.)
        ne fait pas une colonne de courriels."""
        a = sl.analyze_roster(self.book([
            ("nom", "prenom", "note", "id"),
            ("DUPONT", "Jean", "voir scolarite@x.fr", 3021),
            ("MARTIN", "Luc", None, 3022), ("DURAND", "Eva", None, 3023)]))
        self.assertEqual(a["suggested"]["mail_idx"], -1)

    def test_le_zero_de_tete_ajoute_ne_perd_pas_le_courriel(self):
        """⚠ `_pad_leading_zeros` reconstruit les étudiants : reconstruire à la
        main (id/nom/prenom) aurait effacé le courriel en silence."""
        # ⚠ Deux seuils à respecter pour que le complément se déclenche : au
        # moins 80 % des numéros à la largeur modale, et un bloc de trois
        # lignes consécutives à cette largeur AVANT le numéro court (sinon
        # c'est la 1re ligne de données qui se décale).
        st = self.load(self.book([
            ("nom", "prenom", "mail", "id"),
            ("DUPONT", "Jean", "j.d@x.fr", "3021"),
            ("DURAND", "Eva", "e.d@x.fr", "3023"),
            ("MOREAU", "Zoé", "z.m@x.fr", "3024"),
            ("MARTIN", "Luc", "l.m@x.fr", "421"),
            ("PETIT", "Ali", "a.p@x.fr", "3025")]))
        by_nom = {s.nom: s for s in st}
        self.assertEqual(by_nom["MARTIN"].id, "0421")      # complété
        self.assertEqual(by_nom["MARTIN"].email, "l.m@x.fr")

    def test_la_colonne_courriel_se_choisit_ou_se_retire_a_la_main(self):
        p = self.book(self.MAILED)
        cols = {"id_idx": 3, "nom_idx": 0, "prenom_idx": 1, "data_start": 1}
        self.assertEqual(sl.students_from_file(p, cols)[0].email, "")
        self.assertEqual(
            sl.students_from_file(p, {**cols, "mail_idx": 2})[0].email,
            "j.dupont@x.fr")

    def test_un_index_courriel_hors_bornes_est_ignore_pas_fatal(self):
        st = sl.students_from_file(self.book(ROWS), {
            "id_idx": 2, "nom_idx": 0, "prenom_idx": 1, "data_start": 1,
            "mail_idx": 99})
        self.assertEqual(st[0].email, "")
