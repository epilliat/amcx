"""Routes `/api/bank/categories*` sur une banque locale isolée.

Vérifie les codes HTTP autant que les données : un cycle doit répondre 409 et
pas 400, un nœud inconnu 404 et pas 500 — c'est ce que l'UI utilisera pour
distinguer « ta requête est mauvaise » de « l'arbre refuse ».

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

import bank         # noqa: E402
import bank_online  # noqa: E402
import config       # noqa: E402
import server       # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_postgrest import FakePostgrest, install  # noqa: E402


def tearDownModule():
    _TMP.cleanup()


class RouteCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["AMCX_BANK_DIR"] = self._dir.name
        self.addCleanup(self._dir.cleanup)
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    # -- helpers -----------------------------------------------------------
    def get(self, url):
        r = self.c.get(url)
        return r.status_code, r.get_json()

    def post(self, url, body=None):
        r = self.c.post(url, json=body or {})
        return r.status_code, r.get_json()

    def patch(self, url, body):
        r = self.c.patch(url, json=body)
        return r.status_code, r.get_json()

    def put(self, url, body):
        r = self.c.put(url, json=body)
        return r.status_code, r.get_json()

    def delete(self, url):
        r = self.c.delete(url)
        return r.status_code, r.get_json()

    def mkcat(self, name, parent=None):
        code, j = self.post("/api/bank/categories",
                            {"name": name, "parent_id": parent})
        self.assertEqual(code, 200, j)
        return j["node"]["id"]

    def mkquestion(self, title="Q", tags=None):
        q = bank.from_block({"kind": "question_qcm",
                             "data": {"tag": "q", "statement": title,
                                      "qtype": "single", "answers": []}},
                            title=title, tags=tags or [])
        bank.save(q)
        return q["bank_id"]


class TestTreeRoutes(RouteCase):
    def test_empty_tree(self):
        code, j = self.get("/api/bank/categories")
        self.assertEqual(code, 200)
        self.assertEqual(j["nodes"], [])
        self.assertEqual(j["max_depth"], 4)
        self.assertTrue(j["can_edit"], "une banque locale est toujours éditable")

    def test_create_and_list(self):
        inf = self.mkcat("Inférence")
        self.mkcat("Tests", inf)
        code, j = self.get("/api/bank/categories")
        self.assertEqual([(n["name"], n["depth"]) for n in j["nodes"]],
                         [("Inférence", 1), ("Tests", 2)])

    def test_static_route_wins_over_bank_id(self):
        # `/api/bank/categories` ne doit pas être capturé par `/api/bank/<bank_id>`.
        code, j = self.get("/api/bank/categories")
        self.assertEqual(code, 200)
        self.assertIn("nodes", j)

    def test_empty_name_is_400(self):
        code, j = self.post("/api/bank/categories", {"name": "  "})
        self.assertEqual(code, 400)
        self.assertIn("error", j)

    def test_unknown_parent_is_404(self):
        import bank_taxonomy as tx
        code, _ = self.post("/api/bank/categories",
                            {"name": "X", "parent_id": tx.new_cat_id()})
        self.assertEqual(code, 404)

    def test_malformed_parent_is_400(self):
        code, _ = self.post("/api/bank/categories",
                            {"name": "X", "parent_id": "*"})
        self.assertEqual(code, 400)

    def test_sibling_conflict_is_409(self):
        self.mkcat("Inférence")
        code, j = self.post("/api/bank/categories", {"name": "inférence"})
        self.assertEqual(code, 409)
        self.assertTrue(j.get("conflict"))

    def test_depth_limit_is_409(self):
        parent = None
        for i in range(4):
            parent = self.mkcat(f"N{i}", parent)
        code, _ = self.post("/api/bank/categories",
                            {"name": "trop", "parent_id": parent})
        self.assertEqual(code, 409)

    def test_rename(self):
        cid = self.mkcat("Inférence")
        code, j = self.patch(f"/api/bank/categories/{cid}", {"name": "Tests"})
        self.assertEqual(code, 200)
        self.assertEqual(j["node"]["name"], "Tests")

    def test_move_and_cycle_is_409(self):
        a = self.mkcat("A")
        child = self.mkcat("Enfant", a)
        code, _ = self.patch(f"/api/bank/categories/{a}", {"parent_id": child})
        self.assertEqual(code, 409)

    def test_parent_absent_vs_null(self):
        a = self.mkcat("A")
        child = self.mkcat("Enfant", a)
        # absent → parent inchangé
        _, j = self.patch(f"/api/bank/categories/{child}", {"name": "Renommé"})
        self.assertEqual(j["node"]["parent_id"], a)
        # null → racine
        _, j = self.patch(f"/api/bank/categories/{child}", {"parent_id": None})
        self.assertIsNone(j["node"]["parent_id"])

    def test_unknown_node_is_404(self):
        import bank_taxonomy as tx
        code, _ = self.patch(f"/api/bank/categories/{tx.new_cat_id()}", {"name": "X"})
        self.assertEqual(code, 404)
        code, _ = self.delete(f"/api/bank/categories/{tx.new_cat_id()}")
        self.assertEqual(code, 404)

    def test_delete_empty_then_nonempty(self):
        a = self.mkcat("A")
        self.mkcat("Enfant", a)
        code, j = self.delete(f"/api/bank/categories/{a}")
        self.assertEqual(code, 409, j)
        code, j = self.delete(f"/api/bank/categories/{a}?mode=reparent")
        self.assertEqual(code, 200)
        self.assertEqual(j["reparented_children"], 1)

    def test_bad_mode_is_400(self):
        a = self.mkcat("A")
        code, _ = self.delete(f"/api/bank/categories/{a}?mode=nuke")
        self.assertEqual(code, 400)


class TestQuestionCategories(RouteCase):
    def test_get_put_roundtrip(self):
        a = self.mkcat("A")
        b = self.mkcat("B")
        qid = self.mkquestion("Q1")
        code, j = self.get(f"/api/bank/{qid}/categories")
        self.assertEqual((code, j["categories"]), (200, []))
        r = self.c.put(f"/api/bank/{qid}/categories", json={"categories": [b, a]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["categories"], [b, a])
        _, j = self.get(f"/api/bank/{qid}/categories")
        self.assertEqual(j["categories"], [b, a])

    def test_put_unknown_is_404_and_malformed_is_400(self):
        import bank_taxonomy as tx
        qid = self.mkquestion("Q1")
        r = self.c.put(f"/api/bank/{qid}/categories",
                       json={"categories": [tx.new_cat_id()]})
        self.assertEqual(r.status_code, 404)
        r = self.c.put(f"/api/bank/{qid}/categories", json={"categories": ["*"]})
        self.assertEqual(r.status_code, 400)

    def test_classifying_does_not_bump_version(self):
        a = self.mkcat("A")
        qid = self.mkquestion("Q1")
        before = bank.load(qid)["version"]
        self.c.put(f"/api/bank/{qid}/categories", json={"categories": [a]})
        self.assertEqual(bank.load(qid)["version"], before)


class TestAssignRoute(RouteCase):
    def test_bulk_by_ids_is_idempotent(self):
        a = self.mkcat("A")
        ids = [self.mkquestion(f"Q{i}") for i in range(3)]
        self.assertEqual(self.post(f"/api/bank/categories/{a}/assign",
                                   {"bank_ids": ids})[1]["n"], 3)
        self.assertEqual(self.post(f"/api/bank/categories/{a}/assign",
                                   {"bank_ids": ids})[1]["n"], 0)

    def test_promote_tag_opt_in(self):
        a = self.mkcat("Proba")
        self.mkquestion("Q1", tags=["proba"])
        self.mkquestion("Q2", tags=["proba"])
        self.mkquestion("Q3", tags=["stats"])
        code, j = self.post(f"/api/bank/categories/{a}/assign", {"tag": "proba"})
        self.assertEqual((code, j["n"]), (200, 2))
        # Les tags ne sont PAS consommés par la promotion.
        self.assertEqual(bank.load(bank.list_questions({"q": "Q1"})[0]["bank_id"])["tags"],
                         ["proba"])

    def test_remove(self):
        a = self.mkcat("A")
        qid = self.mkquestion("Q1")
        self.post(f"/api/bank/categories/{a}/assign", {"bank_ids": [qid]})
        code, j = self.post(f"/api/bank/categories/{a}/assign",
                            {"bank_ids": [qid], "remove": True})
        self.assertEqual((code, j["n"]), (200, 1))
        self.assertEqual(self.get(f"/api/bank/{qid}/categories")[1]["categories"], [])


class TestListFilters(RouteCase):
    def setUp(self):
        super().setUp()
        self.inf = self.mkcat("Inférence")
        self.tests = self.mkcat("Tests", self.inf)
        self.q_tests = self.mkquestion("Sur les tests")
        self.q_inf = self.mkquestion("Sur l'inférence")
        self.q_none = self.mkquestion("Non classée")
        self.c.put(f"/api/bank/{self.q_tests}/categories",
                   json={"categories": [self.tests]})
        self.c.put(f"/api/bank/{self.q_inf}/categories",
                   json={"categories": [self.inf]})

    def titles(self, qs):
        code, j = self.get("/api/bank?" + qs)
        self.assertEqual(code, 200, j)
        return sorted(q["title"] for q in j["items"])

    def test_descendants_default_on(self):
        self.assertEqual(self.titles(f"category={self.inf}"),
                         ["Sur l'inférence", "Sur les tests"])

    def test_descendants_off(self):
        self.assertEqual(self.titles(f"category={self.inf}&descendants=0"),
                         ["Sur l'inférence"])

    def test_uncategorized(self):
        self.assertEqual(self.titles("uncategorized=1"), ["Non classée"])

    def test_malformed_category_is_400(self):
        code, _ = self.get("/api/bank?category=*")
        self.assertEqual(code, 400)

    def test_items_carry_categories(self):
        _, j = self.get(f"/api/bank?category={self.tests}")
        self.assertEqual(j["items"][0]["categories"], [self.tests])

    def test_facets_route(self):
        self.mkquestion("Avec tag", tags=["proba"])
        code, j = self.get("/api/bank/facets")
        self.assertEqual(code, 200)
        self.assertEqual(j["all_tags"], ["proba"])
        self.assertEqual([n["name"] for n in j["nodes"]], ["Inférence", "Tests"])

    def test_list_no_longer_carries_all_tags(self):
        # Le calculer imposait un second parcours complet de la banque à chaque
        # frappe. Les facettes sont servies une fois par /api/bank/facets.
        code, j = self.get("/api/bank")
        self.assertEqual(code, 200)
        self.assertNotIn("all_tags", j)
        self.assertIn("items", j)



if __name__ == "__main__":
    unittest.main()


class TestOnlineDispatch(RouteCase):
    """Les mêmes routes doivent servir une banque en ligne — les catégories ne
    sont PAS réservées au backend online comme les ratings, ni au local."""

    def setUp(self):
        super().setUp()
        self.db = install(bank_online, FakePostgrest())
        self._orig_cfg = config.active_bank_cfg
        config.active_bank_cfg = lambda: {
            "name": "en ligne", "type": "online",
            "supabase_url": "https://x.supabase.co", "supabase_anon_key": "k",
            "user_token": "jwt", "user_id": "me"}
        self.addCleanup(lambda: setattr(config, "active_bank_cfg", self._orig_cfg))

    def test_backend_is_the_online_one(self):
        self.assertIs(server._bank(), bank_online)
        self.assertIs(server._cat_backend(), bank_online)

    def test_crud_over_http(self):
        code, j = self.post("/api/bank/categories", {"name": "Inférence"})
        self.assertEqual(code, 200, j)
        inf = j["node"]["id"]
        code, j = self.post("/api/bank/categories",
                            {"name": "Tests", "parent_id": inf})
        self.assertEqual(code, 200, j)
        code, j = self.get("/api/bank/categories")
        self.assertEqual([n["name"] for n in j["nodes"]], ["Inférence", "Tests"])
        self.assertTrue(j["can_edit"])

    def test_conflicts_keep_their_http_codes(self):
        inf = self.mkcat("Inférence")
        self.assertEqual(self.post("/api/bank/categories",
                                   {"name": "inférence"})[0], 409)
        self.assertEqual(self.patch(f"/api/bank/categories/{inf}",
                                    {"parent_id": inf})[0], 409)
        self.assertEqual(self.patch(
            "/api/bank/categories/11111111-1111-1111-1111-111111111111",
            {"name": "X"})[0], 404)
        self.assertEqual(self.post("/api/bank/categories",
                                   {"name": "X", "parent_id": "*"})[0], 400)

    def test_question_assignment_over_http(self):
        tests = self.mkcat("Tests")
        qid = self.db.add_question("Q")
        code, j = self.put(f"/api/bank/{qid}/categories", {"categories": [tests]})
        self.assertEqual(code, 200, j)
        code, j = self.get(f"/api/bank/{qid}/categories")
        self.assertEqual((code, j["categories"]), (200, [tests]))
        # classer n'écrit jamais la question
        self.assertEqual(self.db.wrote("bank_questions"), [])
