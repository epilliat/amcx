"""Backend en ligne : parité de l'API catégories avec bank.py local.

Le vrai PostgREST est remplacé par `fake_postgrest` : on vérifie les invariants
côté client, les requêtes émises et les formes de retour. RLS et trigger
`bank_categories_check_tree` ne sont vérifiables que sur une vraie base.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bank_taxonomy as tx        # noqa: E402
import bank_online as bo          # noqa: E402
from fake_postgrest import FakePostgrest, install  # noqa: E402


class OnlineCase(unittest.TestCase):
    def setUp(self):
        self._orig = (bo._request, bo.current_user_id)
        self.db = install(bo, FakePostgrest())

    def tearDown(self):
        bo._request, bo.current_user_id = self._orig

    def chapter(self):
        inf = bo.create_category("Inférence")
        tests = bo.create_category("Tests", inf["id"])
        ic = bo.create_category("Intervalles", inf["id"])
        val = bo.create_category("Validation")
        return inf, tests, ic, val


class TestTree(OnlineCase):
    def test_create_and_annotate(self):
        self.chapter()
        got = [(n["name"], n["depth"], n["path"]) for n in bo.list_categories()]
        self.assertEqual(got, [
            ("Inférence", 1, ["Inférence"]),
            ("Tests", 2, ["Inférence", "Tests"]),
            ("Intervalles", 2, ["Inférence", "Intervalles"]),
            ("Validation", 1, ["Validation"])])

    def test_created_by_is_set(self):
        inf = bo.create_category("Inférence")
        self.assertEqual(inf["created_by"], "me")

    def test_sibling_conflict(self):
        inf, *_ = self.chapter()
        with self.assertRaises(tx.TaxonomyConflict):
            bo.create_category("TESTS", inf["id"])

    def test_depth_limit(self):
        parent = None
        for i in range(tx.MAX_DEPTH):
            parent = bo.create_category(f"N{i}", parent)["id"]
        with self.assertRaises(tx.TaxonomyConflict):
            bo.create_category("trop", parent)

    def test_unknown_parent_is_keyerror(self):
        # BankNotFoundError hérite de KeyError → 404 côté route, comme en local.
        with self.assertRaises(KeyError):
            bo.create_category("X", tx.new_cat_id())

    def test_malformed_parent(self):
        with self.assertRaises(tx.TaxonomyError):
            bo.create_category("X", "*")

    def test_empty_name(self):
        with self.assertRaises(tx.TaxonomyError):
            bo.create_category("  ")


class TestMove(OnlineCase):
    def test_rename_keeps_assignments(self):
        inf, tests, _ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"]])
        bo.update_category(tests["id"], name="Tests d'hypothèses")
        self.assertEqual(bo.get_question_categories(qid), [tests["id"]])

    def test_move_and_back_to_root(self):
        _inf, tests, _ic, val = self.chapter()
        bo.update_category(tests["id"], parent_id=val["id"])
        self.assertEqual(tx.path(bo._raw_categories(), tests["id"]),
                         ["Validation", "Tests"])
        bo.update_category(tests["id"], parent_id=None)
        self.assertEqual(tx.path(bo._raw_categories(), tests["id"]), ["Tests"])

    def test_parent_untouched_when_omitted(self):
        inf, tests, _ic, _val = self.chapter()
        bo.update_category(tests["id"], name="Renommée")
        self.assertEqual(tx.index_by_id(bo._raw_categories())[tests["id"]]["parent_id"],
                         inf["id"])

    def test_cycle_refused(self):
        inf, tests, _ic, _val = self.chapter()
        with self.assertRaises(tx.TaxonomyConflict):
            bo.update_category(inf["id"], parent_id=tests["id"])

    def test_subtree_depth_refused(self):
        a = bo.create_category("a")
        b = bo.create_category("b", a["id"])
        bo.create_category("c", b["id"])
        x = bo.create_category("x")
        y = bo.create_category("y", x["id"])
        with self.assertRaises(tx.TaxonomyConflict):
            bo.update_category(a["id"], parent_id=y["id"])
        bo.update_category(a["id"], parent_id=x["id"])   # un niveau de moins : OK

    def test_noop_update_emits_no_write(self):
        _inf, tests, *_ = self.chapter()
        before = len(self.db.wrote("bank_categories"))
        bo.update_category(tests["id"])
        self.assertEqual(len(self.db.wrote("bank_categories")), before)


class TestDelete(OnlineCase):
    def test_refuses_non_empty(self):
        inf, _tests, _ic, _val = self.chapter()
        with self.assertRaises(tx.TaxonomyConflict):
            bo.delete_category(inf["id"])

    def test_deletes_empty(self):
        _inf, _tests, _ic, val = self.chapter()
        bo.delete_category(val["id"])
        self.assertNotIn("Validation", [n["name"] for n in bo.list_categories()])

    def test_reparent_moves_children_and_questions(self):
        inf, tests, _ic, _val = self.chapter()
        deep = bo.create_category("Student", tests["id"])
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"]])
        res = bo.delete_category(tests["id"], mode="reparent")
        self.assertEqual((res["reparented_children"], res["reassigned_questions"]), (1, 1))
        self.assertEqual(tx.path(bo._raw_categories(), deep["id"]),
                         ["Inférence", "Student"])
        self.assertEqual(bo.get_question_categories(qid), [inf["id"]])

    def test_reparent_from_root_drops_category(self):
        inf, tests, _ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [inf["id"]])
        bo.delete_category(inf["id"], mode="reparent")
        self.assertEqual(bo.get_question_categories(qid), [])

    def test_never_touches_bank_questions(self):
        _inf, tests, _ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"]])
        bo.delete_category(tests["id"], mode="reparent")
        self.assertEqual(self.db.wrote("bank_questions"), [])

    def test_bad_mode(self):
        _inf, _tests, _ic, val = self.chapter()
        with self.assertRaises(tx.TaxonomyError):
            bo.delete_category(val["id"], mode="purge")


class TestAssignments(OnlineCase):
    def test_set_get_roundtrip_and_dedup(self):
        _inf, tests, ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        got = bo.set_question_categories(qid, [tests["id"], ic["id"], tests["id"]])
        self.assertEqual(got, [tests["id"], ic["id"]])
        self.assertEqual(sorted(bo.get_question_categories(qid)), sorted(got))

    def test_set_replaces(self):
        _inf, tests, ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"]])
        bo.set_question_categories(qid, [ic["id"]])
        self.assertEqual(bo.get_question_categories(qid), [ic["id"]])

    def test_classing_never_writes_the_question(self):
        # « Classer n'est pas éditer » : aucune écriture sur bank_questions,
        # donc ni `modified_at` ni `version` ne bougent.
        _inf, tests, _ic, _val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"]])
        self.assertEqual(self.db.wrote("bank_questions"), [])

    def test_unknown_and_malformed(self):
        qid = self.db.add_question("Q")
        with self.assertRaises(KeyError):
            bo.set_question_categories(qid, [tx.new_cat_id()])
        with self.assertRaises(tx.TaxonomyError):
            bo.set_question_categories(qid, ["*"])

    def test_bulk_assign_idempotent(self):
        _inf, tests, _ic, _val = self.chapter()
        ids = [self.db.add_question(f"Q{i}") for i in range(3)]
        self.assertEqual(bo.assign_category(tests["id"], ids), 3)
        self.assertEqual(bo.assign_category(tests["id"], ids), 0)
        self.assertEqual(bo.assign_category(tests["id"], ids[:2], remove=True), 2)
        self.assertEqual(bo.assign_category(tests["id"], ids[:2], remove=True), 0)

    def test_bulk_rejects_malformed_question_id(self):
        _inf, tests, _ic, _val = self.chapter()
        with self.assertRaises(ValueError):
            bo.assign_category(tests["id"], ["*"])

    def test_multi_membership_counts_as_a_set(self):
        inf, tests, _ic, val = self.chapter()
        qid = self.db.add_question("Q")
        bo.set_question_categories(qid, [tests["id"], val["id"]])
        cats = {n["name"]: n for n in bo.list_categories()}
        self.assertEqual(cats["Inférence"]["n_total"], 1)
        self.assertEqual(cats["Tests"]["n_direct"], 1)
        self.assertEqual(cats["Validation"]["n_direct"], 1)


class TestListQuestions(OnlineCase):
    def setUp(self):
        super().setUp()
        self.inf, self.tests, self.ic, self.val = self.chapter()
        self.q1 = self.db.add_question("Dans Tests", tags=["proba"])
        self.q2 = self.db.add_question("Dans Inférence")
        self.q3 = self.db.add_question("Sans catégorie")
        bo.set_question_categories(self.q1, [self.tests["id"]])
        bo.set_question_categories(self.q2, [self.inf["id"]])

    def titles(self, **f):
        return sorted(q["title"] for q in bo.list_questions(f))

    def test_descendants_by_default(self):
        self.assertEqual(self.titles(category=self.inf["id"]),
                         ["Dans Inférence", "Dans Tests"])

    def test_descendants_off(self):
        self.assertEqual(self.titles(category=self.inf["id"], descendants=False),
                         ["Dans Inférence"])

    def test_empty_branch(self):
        self.assertEqual(self.titles(category=self.ic["id"]), [])

    def test_uncategorized(self):
        self.assertEqual(self.titles(uncategorized=True), ["Sans catégorie"])

    def test_malformed_category(self):
        with self.assertRaises(tx.TaxonomyError):
            bo.list_questions({"category": "*"})

    def test_items_carry_categories(self):
        item = next(q for q in bo.list_questions(None) if q["title"] == "Dans Tests")
        self.assertEqual(item["categories"], [self.tests["id"]])

    def test_combines_with_tag_filter(self):
        self.assertEqual(self.titles(category=self.inf["id"], tags=["proba"]),
                         ["Dans Tests"])


class TestRowMapping(OnlineCase):
    def test_categories_never_sent_as_a_column(self):
        # `categories` / `question_categories` sont une table de jonction : les
        # laisser dans le payload ferait échouer l'insert en PGRST204.
        row = bo._question_to_row({
            "bank_id": "x", "kind": "question_qcm", "data": {}, "title": "T",
            "categories": ["a"], "question_categories": [{"category_id": "a"}],
            "stats": {}, "author": "moi"})
        self.assertNotIn("categories", row)
        self.assertNotIn("question_categories", row)
        self.assertEqual(sorted(row), ["data", "kind", "title"])

    def test_embedding_becomes_a_flat_list(self):
        out = bo._normalize_question(
            {"id": "q1", "title": "T",
             "question_categories": [{"category_id": "c1"}, {"category_id": "c2"}]})
        self.assertEqual(out["categories"], ["c1", "c2"])
        self.assertEqual(out["bank_id"], "q1")

    def test_missing_embedding_defaults_to_empty(self):
        # Les réponses d'insert/update ne portent pas le `select` embarqué.
        self.assertEqual(bo._normalize_question({"id": "q1"})["categories"], [])


if __name__ == "__main__":
    unittest.main()
