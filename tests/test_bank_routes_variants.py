"""Routes `/api/bank/<id>/variants` sur une banque locale isolée.

Le code HTTP compte autant que les données : un rattachement impossible doit
répondre 400 et pas 500, une question inconnue 404, et une banque en ligne 501
— pas un `AttributeError` opaque.

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
os.environ["AMCX_BANK_DIR"] = str(Path(_TMP.name) / "banque")

import bank      # noqa: E402
import server    # noqa: E402


def tearDownModule():
    _TMP.cleanup()


class VariantRouteTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["AMCX_BANK_DIR"] = self._dir.name
        self.addCleanup(self._dir.cleanup)
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    def add(self, title, tags=None):
        q = bank.from_block(
            {"kind": "question_qcm",
             "data": {"tag": "q", "qtype": "single", "statement": title,
                      "answers": [{"text": "a", "correct": True}]}},
            title=title, tags=tags or [])
        bank.save(q)
        return q["bank_id"]

    def test_get_rend_le_groupe(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        r = self.c.post(f"/api/bank/{b}/variants", json={"head_id": a})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["head"], a)
        r = self.c.get(f"/api/bank/{a}/variants")
        self.assertEqual([m["bank_id"] for m in r.get_json()["members"]], [a, b])

    def test_liste_repliee_et_option_a_plat(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        self.c.post(f"/api/bank/{b}/variants", json={"head_id": a})
        items = self.c.get("/api/bank").get_json()["items"]
        self.assertEqual([i["bank_id"] for i in items], [a])
        self.assertEqual(items[0]["n_variants"], 1)
        flat = self.c.get("/api/bank?variants=all").get_json()["items"]
        self.assertEqual(len(flat), 2)

    def test_detacher(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        self.c.post(f"/api/bank/{b}/variants", json={"head_id": a})
        r = self.c.post(f"/api/bank/{b}/variants", json={"head_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["head"], b)
        self.assertEqual(len(self.c.get("/api/bank").get_json()["items"]), 2)

    def test_question_inconnue_404(self):
        a = self.add("Matin")
        self.assertEqual(self.c.get("/api/bank/8badf00d/variants").status_code, 404)
        r = self.c.post(f"/api/bank/{a}/variants", json={"head_id": "8badf00d"})
        self.assertEqual(r.status_code, 400)

    def test_rattachement_a_soi_meme_400(self):
        a = self.add("Matin")
        r = self.c.post(f"/api/bank/{a}/variants", json={"head_id": a})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_detacher_un_chef_400(self):
        a = self.add("Matin")
        r = self.c.post(f"/api/bank/{a}/variants", json={"head_id": None})
        self.assertEqual(r.status_code, 400)

    def test_les_facettes_voient_les_tags_des_variantes(self):
        """Un tag porté par la seule variante doit rester cochable.

        La liste par défaut replie les variantes : calculer les facettes
        dessus ferait disparaître le tag, donc la case qui le retrouverait.
        """
        a = self.add("Matin", ["QCM1"])
        b = self.add("Après-midi", ["QCM7"])
        self.c.post(f"/api/bank/{b}/variants", json={"head_id": a})
        tags = self.c.get("/api/bank/facets").get_json()["all_tags"]
        self.assertIn("QCM7", tags)

    def test_banque_en_ligne_501(self):
        """`bank_online` n'a pas encore les variantes : 501, pas AttributeError."""
        import bank_online
        orig = server._bank
        server._bank = lambda: bank_online
        self.addCleanup(lambda: setattr(server, "_bank", orig))
        self.assertEqual(self.c.get("/api/bank/abcdef12/variants").status_code, 501)


if __name__ == "__main__":
    unittest.main()
