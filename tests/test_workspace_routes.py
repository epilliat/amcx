"""Routes `/api/workspace/*` — codes HTTP et refus.

Ces routes écrivent sur le disque : ce qui compte ici, c'est qu'un chemin qui
sort de la racine réponde 400 et pas 200, et qu'un fichier servi en ligne le
soit sur liste blanche stricte.

    .venv/bin/python -m unittest discover -s tests -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))
sys.path.insert(0, str(_ROOT / "auto_grading" / "front"))

_TMP = Path(tempfile.mkdtemp(prefix="amcx-wsr-test-"))
os.environ.setdefault("AMCX_PROJECT_DIR", str(_TMP / "projet"))

import project_state          # noqa: E402
import server                 # noqa: E402
import workspace as ws        # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class WsRouteTest(unittest.TestCase):
    def setUp(self):
        self.root = _TMP / "travail"
        shutil.rmtree(self.root, ignore_errors=True)
        (self.root / "QCM1" / "sujet").mkdir(parents=True)
        (self.root / "QCM1" / "sujet" / "exam.tex").write_text("\\documentclass")
        (self.root / "notes.csv").write_text("id,note\n1,12\n")
        (self.root / "page.html").write_text("<script>alert(1)</script>")
        os.environ["AMCX_COHORT_DIR"] = str(self.root)
        self._orig = project_state.browse_root
        project_state.browse_root = lambda: _TMP
        self.addCleanup(lambda: setattr(project_state, "browse_root", self._orig))
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    def post(self, url, body=None):
        r = self.c.post(url, json=body or {})
        return r.status_code, (r.get_json() or {})

    # -- état ---------------------------------------------------------------

    def test_etat_expose_la_racine(self):
        j = self.c.get("/api/workspace").get_json()
        self.assertEqual(j["root"], str(self.root))
        self.assertEqual(j["name"], "travail")

    def test_listing_et_reconnaissance_de_projet(self):
        j = self.c.get("/api/workspace/list").get_json()
        e = {x["name"]: x for x in j["entries"]}
        self.assertTrue(e["QCM1"]["project"])
        self.assertFalse(e["notes.csv"]["is_dir"])

    # -- refus --------------------------------------------------------------

    def test_sortir_de_la_racine_400(self):
        for bad in ("..", "../..", "QCM1/../.."):
            self.assertEqual(self.c.get(
                "/api/workspace/list?path=" + bad).status_code, 400, bad)

    def test_racine_hors_du_dossier_personnel_400(self):
        st, j = self.post("/api/workspace/root", {"path": "/etc"})
        self.assertEqual(st, 400)
        self.assertIn("error", j)

    def test_nom_invalide_400(self):
        self.assertEqual(self.post("/api/workspace/mkdir",
                                   {"parent": "", "name": ".."})[0], 400)

    def test_un_html_ne_se_sert_jamais_en_ligne(self):
        """⚠ Le cœur du garde-fou : un `.html` servi `inline` sur
        `localhost:5050` serait du script aux droits de l'interface."""
        st, j = self.c.get("/api/workspace/view?path=page.html").status_code, None
        self.assertEqual(st, 400)
        r = self.c.get("/api/workspace/download?path=page.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        r.close()          # `send_file` garde le descripteur ouvert sinon

    def test_un_pdf_se_sert_en_ligne_avec_nosniff(self):
        (self.root / "doc.pdf").write_bytes(b"%PDF-1.4\n")
        r = self.c.get("/api/workspace/view?path=doc.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        r.close()

    # -- remaniement --------------------------------------------------------

    def test_creer_renommer_deplacer_supprimer(self):
        self.assertEqual(self.post("/api/workspace/mkdir",
                                   {"parent": "", "name": "2026"})[1]["path"], "2026")
        self.assertEqual(self.post("/api/workspace/move",
                                   {"path": "notes.csv", "dest": "2026"})[1]["path"],
                         "2026/notes.csv")
        self.assertEqual(self.post("/api/workspace/rename",
                                   {"path": "2026", "name": "2027"})[1]["path"], "2027")
        st, j = self.post("/api/workspace/delete", {"path": "2027"})
        self.assertEqual((st, j["n_trash"]), (200, 1))
        self.assertFalse((self.root / "2027").exists())

    def test_une_suppression_partielle_est_rendue_pas_tue(self):
        """Supprimer 2 chemins dont 1 faux ne doit ni s'arrêter au premier, ni
        laisser croire que les deux sont partis."""
        st, j = self.post("/api/workspace/delete",
                          {"paths": ["notes.csv", "inexistant"]})
        self.assertFalse(j["ok"])
        self.assertEqual(len(j["trashed"]), 1)
        self.assertEqual(len(j["failed"]), 1)

    def test_corbeille_restaurer(self):
        self.post("/api/workspace/delete", {"path": "notes.csv"})
        slot = self.c.get("/api/workspace/trash").get_json()["entries"][0]["slot"]
        st, j = self.post("/api/workspace/trash/restore", {"slot": slot})
        self.assertEqual((st, j["path"]), (200, "notes.csv"))
        self.assertTrue((self.root / "notes.csv").is_file())

    def test_vider_la_corbeille_annonce_ce_quil_detruit(self):
        self.post("/api/workspace/delete", {"path": "QCM1"})
        st, j = self.post("/api/workspace/trash/empty")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(j["removed"]["n_files"], 2)
        self.assertEqual(self.c.get("/api/workspace/trash").get_json()["entries"], [])

    # -- dépôt --------------------------------------------------------------

    def test_depot_multipart(self):
        r = self.c.post("/api/workspace/upload", data={
            "dest": "QCM1",
            "files": (io.BytesIO(b"%PDF-1.4"), "scan.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["saved"], ["QCM1/scan.pdf"])

    # -- aperçu -------------------------------------------------------------

    def test_apercu_texte(self):
        j = self.c.get("/api/workspace/preview?path=notes.csv").get_json()
        self.assertEqual(j["kind"], "text")
        self.assertIn("id,note", j["text"])

    def test_apercu_refuse_de_deviner_un_binaire(self):
        (self.root / "img.dat").write_bytes(b"\x00\x01\x02")
        self.assertEqual(
            self.c.get("/api/workspace/preview?path=img.dat").get_json()["kind"],
            "binary")


if __name__ == "__main__":
    unittest.main()


class ProjetsEtEvaluationsRouteTest(WsRouteTest):
    """Créer un **projet** depuis l'onglet Fichiers, et le menu de la topbar."""

    def test_creer_un_projet(self):
        code, j = self.post("/api/workspace/cohorte", {"parent": "", "name": "L3"})
        self.assertEqual(code, 200)
        self.assertEqual(j["path"], "L3")
        self.assertTrue((self.root / "L3" / "cohorte.json").is_file())
        e = {x["name"]: x for x in
             self.c.get("/api/workspace/list").get_json()["entries"]}
        self.assertTrue(e["L3"]["cohort"])

    def test_un_nom_de_projet_qui_casse_un_chemin_400(self):
        for bad in ("..", "a/b", ""):
            self.assertEqual(
                self.post("/api/workspace/cohorte", {"parent": "", "name": bad})[0],
                400, msg=bad)

    def test_creer_un_projet_ne_change_pas_la_racine(self):
        self.post("/api/workspace/cohorte", {"parent": "", "name": "L3"})
        self.assertEqual(self.c.get("/api/workspace").get_json()["root"],
                         str(self.root))

    def test_letat_dit_si_la_racine_est_un_projet(self):
        self.assertFalse(self.c.get("/api/workspace").get_json()["cohort"])
        (self.root / "cohorte.json").write_text("{}")
        self.assertTrue(self.c.get("/api/workspace").get_json()["cohort"])

    def test_la_topbar_liste_les_evaluations_du_dossier(self):
        """⚠ Le menu ne montre plus des « récents » mais ce que contient le
        dossier de travail : sinon la topbar et l'onglet Fichiers décrivent
        deux mondes."""
        with server.app.test_request_context("/"):
            ctx = server._inject_project_context()
        self.assertEqual([e["name"] for e in ctx["project_evaluations"]], ["QCM1"])
        self.assertEqual(ctx["workspace_name"], "travail")
        self.assertNotIn("project_recent", ctx)

    def test_la_topbar_marque_levaluation_active(self):
        orig = server.config.project_root
        server.config.project_root = lambda: self.root / "QCM1" / "auto_grading"
        self.addCleanup(lambda: setattr(server.config, "project_root", orig))
        with server.app.test_request_context("/"):
            ctx = server._inject_project_context()
        self.assertEqual([e["active"] for e in ctx["project_evaluations"]], [True])

    def test_un_dossier_de_travail_illisible_ne_casse_pas_le_rendu(self):
        orig = server.workspace.evaluations
        server.workspace.evaluations = lambda: (_ for _ in ()).throw(OSError("x"))
        self.addCleanup(lambda: setattr(server.workspace, "evaluations", orig))
        with server.app.test_request_context("/"):
            ctx = server._inject_project_context()
        self.assertEqual(ctx["project_evaluations"], [])
