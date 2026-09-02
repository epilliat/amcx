"""Migration locale → en ligne : l'arbre et les affectations suivent.

Le point vérifié ici est le pari central du plan : les identifiants de
catégorie sont des UUID v4 des DEUX côtés, donc ils se transposent tels quels
et aucune table de correspondance n'est nécessaire pour les catégories (elle
n'existe que pour les questions, dont l'id local fait 8 hex).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = Path(tempfile.mkdtemp(prefix="amcx-migr-test-"))
os.environ.setdefault("AMCX_PROJECT_DIR", str(_TMP / "projet"))
os.environ["AMCX_BANK_DIR"] = str(_TMP / "banque")

import bank                  # noqa: E402
import bank_online as bo     # noqa: E402
import bank_migrate as mig   # noqa: E402
import bank_taxonomy as tx   # noqa: E402
from fake_postgrest import FakePostgrest, install  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class MigrateCase(unittest.TestCase):
    def setUp(self):
        # ⚠ repointé à CHAQUE test : `unittest discover` importe tous les
        # modules avant d'en exécuter un, et le dernier import gagne.
        self._bankdir = tempfile.TemporaryDirectory()
        os.environ["AMCX_BANK_DIR"] = self._bankdir.name
        self.addCleanup(self._bankdir.cleanup)
        bank.ensure_root()

        self._orig = (bo._request, bo.current_user_id, bo.is_logged_in, mig.MAPPING_FILE)
        self.db = install(bo, FakePostgrest())
        bo.is_logged_in = lambda: True
        mig.MAPPING_FILE = Path(self._bankdir.name) / "mapping.json"
        mig.print = lambda *a, **k: None

        def restore():
            bo._request, bo.current_user_id, bo.is_logged_in, mig.MAPPING_FILE = self._orig
            mig.print = print
        self.addCleanup(restore)

        # Arbre local : Inférence[Tests, Intervalles] · Validation
        self.inf = bank.create_category("Inférence")
        self.tst = bank.create_category("Tests", self.inf["id"])
        self.ic = bank.create_category("Intervalles", self.inf["id"])
        self.val = bank.create_category("Validation")

    def add_question(self, title, categories=None, stats=None):
        q = bank.from_block({"kind": "question_qcm",
                             "data": {"tag": "q", "qtype": "single",
                                      "statement": title, "answers": []}},
                            title=title, categories=categories)
        if stats:
            q["stats"] = {"by_project": stats}
        bank.save(q)
        return q["bank_id"]

    def online_nodes(self):
        return self.db.tables["bank_categories"]

    def junction(self):
        return self.db.tables["question_categories"]


class TestTreeMigration(MigrateCase):
    def test_ids_are_preserved(self):
        mig.migrate_categories()
        got = {n["id"]: n["name"] for n in self.online_nodes()}
        self.assertEqual(got, {self.inf["id"]: "Inférence",
                               self.tst["id"]: "Tests",
                               self.ic["id"]: "Intervalles",
                               self.val["id"]: "Validation"})

    def test_parents_are_inserted_before_children(self):
        mig.migrate_categories()
        order = [n["id"] for n in self.online_nodes()]
        self.assertLess(order.index(self.inf["id"]), order.index(self.tst["id"]))
        self.assertLess(order.index(self.inf["id"]), order.index(self.ic["id"]))

    def test_hierarchy_survives(self):
        mig.migrate_categories()
        nodes = tx.validate_nodes(self.online_nodes())
        self.assertEqual(tx.path(nodes, self.tst["id"]), ["Inférence", "Tests"])

    def test_idempotent(self):
        r1 = mig.migrate_categories()
        n = len(self.online_nodes())
        r2 = mig.migrate_categories()
        self.assertEqual(len(self.online_nodes()), n)
        self.assertEqual((r1["errors"], r2["errors"]), ([], []))

    def test_dry_run_writes_nothing(self):
        r = mig.migrate_categories(dry_run=True)
        self.assertEqual(self.online_nodes(), [])
        self.assertEqual(r["created"], 0)

    def test_name_conflict_is_reported_not_merged(self):
        # Un chapitre « Validation » créé indépendamment en ligne, autre id.
        bo.import_category({"id": tx.new_cat_id(), "parent_id": None,
                            "name": "Validation", "position": 0})
        r = mig.migrate_categories()
        self.assertEqual(len(r["errors"]), 1, r)
        self.assertIn("Validation", r["errors"][0])
        # Les autres passent quand même.
        names = sorted(n["name"] for n in self.online_nodes())
        self.assertEqual(names, sorted(["Inférence", "Tests", "Intervalles", "Validation"]))

    def test_empty_tree(self):
        shutil.rmtree(bank.bank_root(), ignore_errors=True)
        bank.ensure_root()
        self.assertEqual(mig.migrate_categories(), {"created": 0, "errors": []})


class TestQuestionMigration(MigrateCase):
    def test_assignments_follow_with_the_same_ids(self):
        self.add_question("Deux catégories", categories=[self.tst["id"], self.val["id"]])
        self.add_question("Aucune")
        mig.migrate_categories()
        r = mig.migrate_questions()
        self.assertEqual((r["uploaded"], r["cats"], r["errors"]), (2, 2, []))
        rows = self.junction()
        self.assertEqual(sorted(x["category_id"] for x in rows),
                         sorted([self.tst["id"], self.val["id"]]))
        # …et pointent bien sur la question migrée, pas sur son id local.
        new_ids = {q["id"] for q in self.db.tables["bank_questions"]}
        self.assertTrue({x["question_id"] for x in rows} <= new_ids)

    def test_online_question_reads_back_its_categories(self):
        self.add_question("Deux catégories", categories=[self.tst["id"], self.val["id"]])
        mig.migrate_categories()
        mig.migrate_questions()
        q = next(x for x in bo.list_questions(None) if x["title"] == "Deux catégories")
        self.assertEqual(sorted(q["categories"]),
                         sorted([self.tst["id"], self.val["id"]]))

    def test_filtering_works_after_migration(self):
        self.add_question("Dans Tests", categories=[self.tst["id"]])
        self.add_question("Ailleurs", categories=[self.val["id"]])
        mig.migrate_categories()
        mig.migrate_questions()
        titles = [q["title"] for q in bo.list_questions({"category": self.inf["id"]})]
        self.assertEqual(titles, ["Dans Tests"])   # descendants inclus

    def test_idempotent_no_duplicate_assignments(self):
        self.add_question("Deux catégories", categories=[self.tst["id"], self.val["id"]])
        mig.migrate_categories()
        mig.migrate_questions()
        n = len(self.junction())
        r2 = mig.migrate_questions()
        self.assertEqual(r2["uploaded"], 0)
        self.assertEqual(r2["skipped"], 1)
        self.assertEqual(len(self.junction()), n)

    def test_evals_still_migrate(self):
        self.add_question("Avec stats", categories=[self.tst["id"]],
                          stats={"exam2026": {"n_eval": 10, "sum_normalized": 7.5,
                                              "n_perfect": 3, "max_score_at_sync": 1}})
        mig.migrate_categories()
        r = mig.migrate_questions()
        self.assertEqual(r["evals"], 1)
        self.assertEqual(len(self.db.tables["question_evals"]), 1)

    def test_mapping_file_written(self):
        self.add_question("Q")
        mig.migrate_categories()
        mig.migrate_questions()
        m = json.loads(mig.MAPPING_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(m), 1)
        old_id = next(iter(m))
        self.assertEqual(len(old_id), 8)          # local : 8 hex
        self.assertTrue(tx.is_valid_cat_id(m[old_id]))   # en ligne : UUID


if __name__ == "__main__":
    unittest.main()
