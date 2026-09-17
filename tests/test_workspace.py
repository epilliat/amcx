"""Dossier de travail : lecture, remaniement, corbeille, et surtout ce qu'on
REFUSE de faire.

Ce module déplace et supprime des fichiers sur une machine dont le serveur n'a
aucune authentification : les tests qui comptent sont ceux qui vérifient les
refus. Tout se passe dans un dossier jetable, pointé par `AMCX_COHORT_DIR`.

    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))

_TMP = Path(tempfile.mkdtemp(prefix="amcx-ws-test-"))
os.environ["AMCX_PROJECT_DIR"] = str(_TMP / "projet")

import project_state          # noqa: E402
import workspace as ws        # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.root = _TMP / "travail"
        shutil.rmtree(self.root, ignore_errors=True)
        (self.root / "QCM1" / "sujet").mkdir(parents=True)
        (self.root / "QCM1" / "sujet" / "exam.tex").write_text("x")
        (self.root / "brouillon").mkdir()
        (self.root / "liste.xlsx").write_text("y")
        (self.root / ".cache").mkdir()
        os.environ["AMCX_COHORT_DIR"] = str(self.root)
        # ⚠ `browse_root()` vaut le dossier personnel ; le dossier de test est
        # ailleurs, donc on le repointe le temps du test plutôt que d'écrire
        # dans le vrai HOME.
        self._orig = project_state.browse_root
        project_state.browse_root = lambda: _TMP
        self.addCleanup(lambda: setattr(project_state, "browse_root", self._orig))

    # -- lecture -----------------------------------------------------------

    def test_racine_et_listing(self):
        self.assertEqual(ws.root(), self.root)
        names = [e["name"] for e in ws.listdir()]
        self.assertEqual(names, ["brouillon", "QCM1", "liste.xlsx"])  # casse ignorée

    def test_les_dossiers_dabord_puis_par_nom(self):
        (self.root / "aaa.txt").write_text("")
        self.assertEqual([e["is_dir"] for e in ws.listdir()],
                         [True, True, False, False])

    def test_un_projet_amcx_est_reconnu(self):
        e = {x["name"]: x for x in ws.listdir()}
        self.assertTrue(e["QCM1"]["project"])
        self.assertFalse(e["brouillon"]["project"])

    def test_projet_dans_un_sous_dossier_auto_grading(self):
        d = self.root / "Rattrapage" / "auto_grading" / "sujet"
        d.mkdir(parents=True)
        (d / "exam.tex").write_text("x")
        self.assertTrue(ws.is_project(self.root / "Rattrapage"))
        self.assertEqual(ws.project_root_of(self.root / "Rattrapage"),
                         self.root / "Rattrapage" / "auto_grading")

    def test_les_caches_sont_masques_par_defaut(self):
        self.assertNotIn(".cache", [e["name"] for e in ws.listdir()])
        self.assertIn(".cache", [e["name"] for e in ws.listdir(hidden=True)])

    # -- refus -------------------------------------------------------------

    def test_sortir_de_la_racine_est_refuse(self):
        for bad in ("..", "../..", "QCM1/../..", "/etc"):
            with self.assertRaises(ws.WorkspaceError, msg=bad):
                ws.resolve(bad)

    def test_un_lien_symbolique_qui_sort_est_refuse(self):
        """⚠ Le contrôle porte sur le chemin RÉSOLU : sans ça, un lien déposé
        dans le dossier de travail ouvrirait le reste du disque."""
        dehors = _TMP / "dehors"
        dehors.mkdir(exist_ok=True)
        (dehors / "secret.txt").write_text("s")
        try:
            (self.root / "piege").symlink_to(dehors)
        except OSError:
            self.skipTest("liens symboliques indisponibles")
        with self.assertRaises(ws.WorkspaceError):
            ws.resolve("piege/secret.txt")

    def test_un_nom_qui_casse_un_chemin_est_refuse(self):
        for bad in ("..", "a/b", "CON", "fin.", " "):
            with self.assertRaises(ws.WorkspaceError, msg=bad):
                ws.mkdir("", bad)

    def test_racine_hors_du_dossier_personnel_refusee(self):
        with self.assertRaises(ws.WorkspaceError):
            ws.set_root("/etc")

    def test_on_ne_supprime_pas_la_racine(self):
        with self.assertRaises(ws.WorkspaceError):
            ws.trash("")

    def test_on_ne_deplace_pas_un_dossier_dans_lui_meme(self):
        (self.root / "QCM1" / "sous").mkdir()
        with self.assertRaises(ws.WorkspaceError):
            ws.move("QCM1", "QCM1/sous")
        with self.assertRaises(ws.WorkspaceError):
            ws.move("QCM1", "QCM1")

    def test_un_deplacement_nécrase_jamais(self):
        (self.root / "brouillon" / "liste.xlsx").write_text("autre")
        with self.assertRaises(ws.WorkspaceError):
            ws.move("liste.xlsx", "brouillon")
        self.assertEqual((self.root / "brouillon" / "liste.xlsx").read_text(),
                         "autre")

    def test_un_renommage_nécrase_jamais(self):
        with self.assertRaises(ws.WorkspaceError):
            ws.rename("liste.xlsx", "brouillon")

    # -- remaniement -------------------------------------------------------

    def test_creer_renommer_deplacer(self):
        ws.mkdir("", "2026")
        self.assertTrue((self.root / "2026").is_dir())
        self.assertEqual(ws.rename("2026", "2026-2027"), "2026-2027")
        self.assertEqual(ws.move("liste.xlsx", "2026-2027"),
                         "2026-2027/liste.xlsx")
        self.assertFalse((self.root / "liste.xlsx").exists())

    def test_deplacer_au_meme_endroit_ne_fait_rien(self):
        self.assertEqual(ws.move("liste.xlsx", ""), "liste.xlsx")
        self.assertTrue((self.root / "liste.xlsx").exists())

    # -- corbeille ---------------------------------------------------------

    def test_supprimer_met_a_la_corbeille_sans_rien_detruire(self):
        ws.trash("QCM1")
        self.assertFalse((self.root / "QCM1").exists())
        self.assertNotIn("QCM1", [e["name"] for e in ws.listdir()])
        t = ws.list_trash()
        self.assertEqual([x["name"] for x in t], ["QCM1"])
        self.assertEqual(t[0]["origin"], "QCM1")

    def test_la_corbeille_ne_figure_pas_dans_les_listings(self):
        ws.trash("liste.xlsx")
        for kw in ({}, {"hidden": True}):
            self.assertNotIn(ws.TRASH, [e["name"] for e in ws.listdir(**kw)])

    def test_deux_suppressions_du_meme_nom_coexistent(self):
        ws.trash("liste.xlsx")
        (self.root / "liste.xlsx").write_text("v2")
        ws.trash("liste.xlsx")
        self.assertEqual(len(ws.list_trash()), 2)

    def test_restaurer_remet_a_sa_place(self):
        ws.trash("QCM1/sujet")
        slot = ws.list_trash()[0]["slot"]
        self.assertEqual(ws.restore(slot), "QCM1/sujet")
        self.assertTrue((self.root / "QCM1" / "sujet" / "exam.tex").is_file())
        self.assertEqual(ws.list_trash(), [])

    def test_restaurer_necrase_pas_ce_qui_a_repris_la_place(self):
        ws.trash("liste.xlsx")
        (self.root / "liste.xlsx").write_text("nouveau")
        slot = ws.list_trash()[0]["slot"]
        with self.assertRaises(ws.WorkspaceError):
            ws.restore(slot)
        self.assertEqual((self.root / "liste.xlsx").read_text(), "nouveau")

    def test_vider_annonce_ce_quil_detruit(self):
        ws.trash("QCM1")
        before = ws.trash_size()
        self.assertEqual(before["n_entries"], 1)
        self.assertGreaterEqual(before["n_files"], 2)   # exam.tex + origine.txt
        self.assertEqual(ws.empty_trash()["n_files"], before["n_files"])
        self.assertEqual(ws.list_trash(), [])

    # -- dépôt -------------------------------------------------------------

    def test_depot_de_fichier_sans_ecrasement(self):
        import io
        self.assertEqual(ws.save_upload("brouillon", "scan.pdf", io.BytesIO(b"a")),
                         "brouillon/scan.pdf")
        self.assertEqual(ws.save_upload("brouillon", "scan.pdf", io.BytesIO(b"b")),
                         "brouillon/scan-2.pdf")
        self.assertEqual((self.root / "brouillon" / "scan.pdf").read_bytes(), b"a")

    def test_depot_un_nom_de_chemin_est_reduit_a_son_dernier_segment(self):
        import io
        rel = ws.save_upload("", "../../../etc/passwd", io.BytesIO(b"x"))
        self.assertEqual(rel, "passwd")


if __name__ == "__main__":
    unittest.main()
