"""Backend local des catégories : arbre sur disque + affectations + filtres.

Isolé par `AMCX_PROJECT_DIR` (config vide → aucune banque configurée) et
`AMCX_BANK_DIR` (racine de banque jetable). Aucune banque réelle n'est touchée.

    .venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))

# ⚠ AVANT d'importer bank/config : la racine de banque est résolue à l'appel,
# mais la config du projet est lue au premier import.
_TMP = Path(tempfile.mkdtemp(prefix="amcx-bank-test-"))
os.environ["AMCX_PROJECT_DIR"] = str(_TMP / "projet")
os.environ["AMCX_BANK_DIR"] = str(_TMP / "banque")

import bank            # noqa: E402
import bank_taxonomy as tx  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class BankCase(unittest.TestCase):
    """Repart d'une banque vide à chaque test."""

    def setUp(self):
        # ⚠ Repointer la banque à CHAQUE test, pas seulement à l'import du
        # module : `unittest discover` importe tous les modules de test avant
        # d'en exécuter un seul, donc le dernier qui pose `AMCX_BANK_DIR` au
        # niveau module gagne pour tout le monde.
        os.environ["AMCX_BANK_DIR"] = str(_TMP / "banque")
        shutil.rmtree(_TMP / "banque", ignore_errors=True)
        bank.ensure_root()
        self.assertEqual(bank.bank_root(), (_TMP / "banque").resolve())

    def add_question(self, title, tags=None, categories=None):
        q = bank.from_block(
            {"kind": "question_qcm",
             "data": {"tag": "q", "qtype": "single", "statement": title,
                      "answers": [{"text": "a", "correct": True}]}},
            title=title, tags=tags or [], categories=categories)
        bank.save(q)
        return q["bank_id"]

    def chapter(self):
        """Inférence[Tests, Intervalles] · Validation."""
        inf = bank.create_category("Inférence")
        tests = bank.create_category("Tests", inf["id"])
        ic = bank.create_category("Intervalles", inf["id"])
        val = bank.create_category("Validation")
        return inf, tests, ic, val


class TestTreeStorage(BankCase):
    def test_absent_file_means_empty_tree_and_is_not_created(self):
        self.assertEqual(bank.load_categories(), [])
        self.assertFalse(bank.categories_path().exists())

    def test_tree_lives_at_bank_root_not_in_questions(self):
        bank.create_category("Inférence")
        self.assertTrue(bank.categories_path().exists())
        self.assertEqual(bank.categories_path().parent, bank.bank_root())
        # Un intrus dans questions/ ferait diverger le compte de
        # `_read_or_rebuild_index` → rebuild à chaque lecture.
        self.assertEqual(list(bank.question_dir().glob("*.json")), [])

    def test_persisted_shape(self):
        inf = bank.create_category("Inférence")
        raw = json.loads(bank.categories_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], bank.CATEGORIES_VERSION)
        self.assertEqual(len(raw["nodes"]), 1)
        self.assertEqual(raw["nodes"][0]["id"], inf["id"])
        self.assertIsNone(raw["nodes"][0]["parent_id"])

    def test_corrupt_file_is_set_aside_not_fatal(self):
        bank.create_category("Inférence")
        bank.categories_path().write_text("{ pas du json", encoding="utf-8")
        self.assertEqual(bank.load_categories(), [])
        aside = list(bank.bank_root().glob("categories.json.corrupt-*"))
        self.assertEqual(len(aside), 1)


class TestCreate(BankCase):
    def test_create_root_and_child(self):
        inf, tests, ic, val = self.chapter()
        names = [(n["name"], n["depth"]) for n in bank.list_categories()]
        self.assertEqual(names, [("Inférence", 1), ("Tests", 2),
                                 ("Intervalles", 2), ("Validation", 1)])

    def test_position_appends_at_end(self):
        inf, tests, ic, _val = self.chapter()
        self.assertLess(tests["position"], ic["position"])

    def test_sibling_name_conflict(self):
        inf = bank.create_category("Inférence")
        bank.create_category("Tests", inf["id"])
        with self.assertRaises(tx.TaxonomyConflict):
            bank.create_category("  tests ", inf["id"])
        # même nom sous un autre parent : autorisé
        bank.create_category("Tests", bank.create_category("Validation")["id"])

    def test_unknown_parent(self):
        with self.assertRaises(KeyError):
            bank.create_category("Orpheline", tx.new_cat_id())

    def test_invalid_parent_id(self):
        with self.assertRaises(tx.TaxonomyError):
            bank.create_category("X", "*")

    def test_max_depth(self):
        parent = None
        for i in range(tx.MAX_DEPTH):
            parent = bank.create_category(f"N{i}", parent)["id"]
        with self.assertRaises(tx.TaxonomyConflict):
            bank.create_category("trop-profond", parent)

    def test_empty_name(self):
        with self.assertRaises(tx.TaxonomyError):
            bank.create_category("   ")


class TestRenameMove(BankCase):
    def test_rename_keeps_assignments(self):
        inf, tests, _ic, _val = self.chapter()
        qid = self.add_question("Q", categories=[tests["id"]])
        bank.update_category(tests["id"], name="Tests d'hypothèses")
        self.assertEqual(bank.get_question_categories(qid), [tests["id"]])
        self.assertEqual(tx.path(bank.load_categories(), tests["id"]),
                         ["Inférence", "Tests d'hypothèses"])

    def test_move_to_other_parent_and_to_root(self):
        inf, tests, _ic, val = self.chapter()
        bank.update_category(tests["id"], parent_id=val["id"])
        self.assertEqual(tx.path(bank.load_categories(), tests["id"]),
                         ["Validation", "Tests"])
        bank.update_category(tests["id"], parent_id=None)
        self.assertEqual(tx.path(bank.load_categories(), tests["id"]), ["Tests"])

    def test_parent_untouched_when_omitted(self):
        inf, tests, _ic, _val = self.chapter()
        bank.update_category(tests["id"], name="Renommée")
        self.assertEqual(bank.load_categories()[1]["parent_id"], inf["id"])

    def test_cycle_refused(self):
        inf, tests, _ic, _val = self.chapter()
        with self.assertRaises(tx.TaxonomyConflict):
            bank.update_category(inf["id"], parent_id=tests["id"])
        with self.assertRaises(tx.TaxonomyConflict):
            bank.update_category(inf["id"], parent_id=inf["id"])

    def test_move_refused_when_subtree_would_exceed_depth(self):
        # a[b[c]] et une chaîne x[y] : déplacer `a` sous `y` ferait 4+1 niveaux.
        a = bank.create_category("a")
        b = bank.create_category("b", a["id"])
        bank.create_category("c", b["id"])
        x = bank.create_category("x")
        y = bank.create_category("y", x["id"])
        with self.assertRaises(tx.TaxonomyConflict):
            bank.update_category(a["id"], parent_id=y["id"])
        # sous `x` (1 niveau de moins) : passe tout juste
        bank.update_category(a["id"], parent_id=x["id"])
        self.assertEqual(tx.depth(bank.load_categories(), a["id"]), 2)

    def test_move_into_name_conflict_refused(self):
        inf, tests, _ic, val = self.chapter()
        bank.create_category("Tests", val["id"])
        with self.assertRaises(tx.TaxonomyConflict):
            bank.update_category(tests["id"], parent_id=val["id"])

    def test_unknown_node(self):
        with self.assertRaises(KeyError):
            bank.update_category(tx.new_cat_id(), name="X")


class TestDelete(BankCase):
    def test_refuses_non_empty_by_default(self):
        inf, tests, _ic, _val = self.chapter()
        with self.assertRaises(tx.TaxonomyConflict):
            bank.delete_category(inf["id"])
        qid = self.add_question("Q", categories=[tests["id"]])
        with self.assertRaises(tx.TaxonomyConflict):
            bank.delete_category(tests["id"])
        self.assertEqual(bank.get_question_categories(qid), [tests["id"]])

    def test_deletes_empty_node(self):
        _inf, _tests, _ic, val = self.chapter()
        bank.delete_category(val["id"])
        self.assertNotIn("Validation",
                         [n["name"] for n in bank.list_categories()])

    def test_reparent_moves_children_and_questions(self):
        inf, tests, _ic, _val = self.chapter()
        deep = bank.create_category("Student", tests["id"])
        qid = self.add_question("Q", categories=[tests["id"]])
        res = bank.delete_category(tests["id"], mode="reparent")
        self.assertEqual(res["reparented_children"], 1)
        self.assertEqual(res["reassigned_questions"], 1)
        self.assertEqual(tx.path(bank.load_categories(), deep["id"]),
                         ["Inférence", "Student"])
        self.assertEqual(bank.get_question_categories(qid), [inf["id"]])

    def test_reparent_from_root_drops_the_category(self):
        inf, tests, _ic, _val = self.chapter()
        qid = self.add_question("Q", categories=[tests["id"], inf["id"]])
        bank.delete_category(inf["id"], mode="reparent")
        # `tests` remonte à la racine ; la question perd `inf` mais garde `tests`
        self.assertEqual(bank.get_question_categories(qid), [tests["id"]])

    def test_never_deletes_a_question(self):
        _inf, tests, _ic, _val = self.chapter()
        qid = self.add_question("Q", categories=[tests["id"]])
        bank.delete_category(tests["id"], mode="reparent")
        self.assertEqual(bank.load(qid)["title"], "Q")

    def test_bad_mode(self):
        _inf, _tests, _ic, val = self.chapter()
        with self.assertRaises(tx.TaxonomyError):
            bank.delete_category(val["id"], mode="purge")


class TestAssignments(BankCase):
    def test_set_and_get(self):
        _inf, tests, ic, _val = self.chapter()
        qid = self.add_question("Q")
        self.assertEqual(bank.get_question_categories(qid), [])
        got = bank.set_question_categories(qid, [tests["id"], ic["id"], tests["id"]])
        self.assertEqual(got, [tests["id"], ic["id"]])          # dédoublonné
        self.assertEqual(bank.get_question_categories(qid), got)

    def test_multi_membership_across_branches(self):
        _inf, tests, _ic, val = self.chapter()
        qid = self.add_question("Q")
        bank.set_question_categories(qid, [tests["id"], val["id"]])
        cats = {n["name"]: n for n in bank.list_categories()}
        self.assertEqual(cats["Tests"]["n_direct"], 1)
        self.assertEqual(cats["Validation"]["n_direct"], 1)
        # comptée une seule fois dans le chapitre malgré 2 appartenances
        self.assertEqual(cats["Inférence"]["n_total"], 1)

    def test_unknown_category_refused(self):
        qid = self.add_question("Q")
        with self.assertRaises(KeyError):
            bank.set_question_categories(qid, [tx.new_cat_id()])
        with self.assertRaises(tx.TaxonomyError):
            bank.set_question_categories(qid, ["*"])

    def test_classing_does_not_bump_modified_at(self):
        _inf, tests, _ic, _val = self.chapter()
        qid = self.add_question("Q")
        before = bank.load(qid)["modified_at"]
        bank.set_question_categories(qid, [tests["id"]])
        self.assertEqual(bank.load(qid)["modified_at"], before)

    def test_dead_id_ignored_on_read(self):
        qid = self.add_question("Q")
        bank.save({**bank.load(qid), "categories": [tx.new_cat_id()]})
        self.assertEqual(bank.get_question_categories(qid), [])

    def test_assign_bulk_add_and_remove(self):
        _inf, tests, _ic, _val = self.chapter()
        ids = [self.add_question(f"Q{i}") for i in range(3)]
        self.assertEqual(bank.assign_category(tests["id"], ids), 3)
        self.assertEqual(bank.assign_category(tests["id"], ids), 0)  # idempotent
        self.assertEqual(bank.assign_category(tests["id"], ids[:2], remove=True), 2)
        self.assertEqual(bank.get_question_categories(ids[0]), [])
        self.assertEqual(bank.get_question_categories(ids[2]), [tests["id"]])

    def test_assign_rejects_malformed_question_id(self):
        _inf, tests, _ic, _val = self.chapter()
        with self.assertRaises(ValueError):
            bank.assign_category(tests["id"], ["*"])

    def test_assign_unknown_category(self):
        with self.assertRaises(KeyError):
            bank.assign_category(tx.new_cat_id(), [])

    def test_from_block_carries_categories(self):
        _inf, tests, _ic, _val = self.chapter()
        qid = self.add_question("Q", categories=[tests["id"], "pas-un-uuid"])
        self.assertEqual(bank.load(qid)["categories"], [tests["id"]])


class TestFilters(BankCase):
    def setUp(self):
        super().setUp()
        self.inf, self.tests, self.ic, self.val = self.chapter()
        self.q_tests = self.add_question("Dans Tests", categories=[self.tests["id"]])
        self.q_inf = self.add_question("Dans Inférence", categories=[self.inf["id"]])
        self.q_none = self.add_question("Sans catégorie")

    def titles(self, **filters):
        return sorted(q["title"] for q in bank.list_questions(filters))

    def test_descendants_included_by_default(self):
        self.assertEqual(self.titles(category=self.inf["id"]),
                         ["Dans Inférence", "Dans Tests"])

    def test_descendants_can_be_excluded(self):
        self.assertEqual(self.titles(category=self.inf["id"], descendants=False),
                         ["Dans Inférence"])

    def test_leaf_filter(self):
        self.assertEqual(self.titles(category=self.tests["id"]), ["Dans Tests"])

    def test_empty_branch(self):
        self.assertEqual(self.titles(category=self.val["id"]), [])

    def test_uncategorized(self):
        self.assertEqual(self.titles(uncategorized=True), ["Sans catégorie"])

    def test_uncategorized_counts_dead_ids_as_uncategorized(self):
        bank.save({**bank.load(self.q_none), "categories": [tx.new_cat_id()]})
        self.assertIn("Sans catégorie", self.titles(uncategorized=True))

    def test_unknown_but_wellformed_category_returns_nothing(self):
        self.assertEqual(self.titles(category=tx.new_cat_id()), [])

    def test_malformed_category_is_an_error(self):
        # Un id non validé finirait interpolé dans une URL PostgREST côté online.
        with self.assertRaises(tx.TaxonomyError):
            bank.list_questions({"category": "*"})

    def test_combines_with_text_search(self):
        self.assertEqual(self.titles(category=self.inf["id"], q="tests"),
                         ["Dans Tests"])


class TestIndex(BankCase):
    def test_index_carries_categories(self):
        _inf, tests, _ic, _val = self.chapter()
        self.add_question("Q", categories=[tests["id"]])
        entry = json.loads(bank.index_path().read_text(encoding="utf-8"))["questions"][0]
        self.assertEqual(entry["categories"], [tests["id"]])

    def test_stale_index_version_triggers_rebuild(self):
        _inf, tests, _ic, _val = self.chapter()
        self.add_question("Q", categories=[tests["id"]])
        idx = json.loads(bank.index_path().read_text(encoding="utf-8"))
        for e in idx["questions"]:
            e.pop("categories", None)
        idx["index_version"] = 1
        bank.index_path().write_text(json.dumps(idx), encoding="utf-8")
        rebuilt = bank._read_or_rebuild_index()
        self.assertEqual(rebuilt["index_version"], bank.INDEX_VERSION)
        self.assertEqual(rebuilt["questions"][0]["categories"], [tests["id"]])

    def test_save_without_reindex_leaves_index_stale(self):
        qid = self.add_question("Q")
        n_before = len(bank._read_or_rebuild_index()["questions"])
        q = bank.from_block({"kind": "text", "data": {"tex": "x"}}, title="T2")
        bank.save(q, reindex=False)
        raw = json.loads(bank.index_path().read_text(encoding="utf-8"))
        self.assertEqual(len(raw["questions"]), n_before)   # pas encore réindexé
        # …mais la lecture détecte la désynchro et reconstruit
        self.assertEqual(len(bank._read_or_rebuild_index()["questions"]), n_before + 1)
        self.assertTrue(qid)


if __name__ == "__main__":
    unittest.main()
