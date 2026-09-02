"""Invariants de l'arbre de catégories (module pur, aucune I/O).

    .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import bank_taxonomy as tx  # noqa: E402


def node(name, parent=None, position=0, cid=None):
    return {"id": cid or tx.new_cat_id(), "name": name,
            "parent_id": parent, "position": position}


def tree():
    """Inférence[Tests[Student], Intervalles] · Validation — 5 nœuds."""
    inf = node("Inférence")
    tests = node("Tests", inf["id"])
    student = node("Student", tests["id"])
    ic = node("Intervalles", inf["id"], position=1)
    val = node("Validation", position=1)
    return [inf, tests, student, ic, val], inf, tests, student, ic, val


class TestIds(unittest.TestCase):
    def test_new_id_is_valid(self):
        self.assertTrue(tx.is_valid_cat_id(tx.new_cat_id()))

    def test_rejects_loose_hex(self):
        # `bank.is_valid_bank_id` accepterait cette chaîne (36 chars de
        # [0-9a-fA-F-]) ; pas question de l'interpoler dans une URL PostgREST.
        self.assertFalse(tx.is_valid_cat_id("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaaa"))
        self.assertFalse(tx.is_valid_cat_id("----------------------------------aa"))
        self.assertFalse(tx.is_valid_cat_id("*"))
        self.assertFalse(tx.is_valid_cat_id(""))
        self.assertFalse(tx.is_valid_cat_id(None))

    def test_uppercase_uuid_accepted(self):
        self.assertTrue(tx.is_valid_cat_id(tx.new_cat_id().upper()))


class TestNames(unittest.TestCase):
    def test_whitespace_collapsed(self):
        self.assertEqual(tx.clean_name("  Régression   linéaire \n"),
                         "Régression linéaire")

    def test_empty_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(tx.TaxonomyError):
                tx.clean_name(bad)

    def test_too_long_rejected(self):
        with self.assertRaises(tx.TaxonomyError):
            tx.clean_name("x" * (tx.NAME_MAX + 1))
        self.assertEqual(len(tx.clean_name("x" * tx.NAME_MAX)), tx.NAME_MAX)


class TestStructure(unittest.TestCase):
    def test_depth_and_path(self):
        nodes, inf, tests, student, _ic, _val = tree()
        self.assertEqual(tx.depth(nodes, inf["id"]), 1)
        self.assertEqual(tx.depth(nodes, student["id"]), 3)
        self.assertEqual(tx.path(nodes, student["id"]),
                         ["Inférence", "Tests", "Student"])
        self.assertEqual(tx.path(nodes, "inconnu"), [])

    def test_descendants(self):
        nodes, inf, tests, student, ic, val = tree()
        self.assertEqual(tx.descendants(nodes, inf["id"]),
                         {inf["id"], tests["id"], student["id"], ic["id"]})
        self.assertEqual(tx.descendants(nodes, inf["id"], include_self=False),
                         {tests["id"], student["id"], ic["id"]})
        self.assertEqual(tx.descendants(nodes, val["id"]), {val["id"]})
        self.assertEqual(tx.descendants(nodes, "inconnu"), set())

    def test_children_sorted_by_position_then_name(self):
        a = node("Zeta", position=0)
        b = node("Alpha", position=0)
        c = node("Beta", position=-1)
        names = [n["name"] for n in tx.children_of([a, b, c], None)]
        self.assertEqual(names, ["Beta", "Alpha", "Zeta"])

    def test_subtree_height(self):
        nodes, inf, tests, _student, _ic, val = tree()
        self.assertEqual(tx.subtree_height(nodes, inf["id"]), 3)
        self.assertEqual(tx.subtree_height(nodes, tests["id"]), 2)
        self.assertEqual(tx.subtree_height(nodes, val["id"]), 1)


class TestCycles(unittest.TestCase):
    def test_self_parent_is_cycle(self):
        nodes, inf, *_ = tree()
        self.assertTrue(tx.would_create_cycle(nodes, inf["id"], inf["id"]))

    def test_descendant_as_parent_is_cycle(self):
        nodes, inf, _tests, student, *_ = tree()
        self.assertTrue(tx.would_create_cycle(nodes, inf["id"], student["id"]))

    def test_unrelated_parent_is_fine(self):
        nodes, inf, *_rest, val = tree()
        self.assertFalse(tx.would_create_cycle(nodes, inf["id"], val["id"]))
        self.assertFalse(tx.would_create_cycle(nodes, inf["id"], None))

    def test_validate_detects_existing_cycle(self):
        a = node("A")
        b = node("B", a["id"])
        a["parent_id"] = b["id"]          # boucle A→B→A
        with self.assertRaises(tx.TaxonomyConflict):
            tx.validate_nodes([a, b])

    def test_descendants_terminates_on_corrupt_tree(self):
        a = node("A")
        b = node("B", a["id"])
        a["parent_id"] = b["id"]
        self.assertEqual(tx.descendants([a, b], a["id"]), {a["id"], b["id"]})


class TestValidation(unittest.TestCase):
    def test_valid_tree_normalized(self):
        nodes, *_ = tree()
        nodes[0]["name"] = "  Inférence  "
        nodes[0]["parent_id"] = ""          # "" doit devenir None
        nodes[0]["position"] = "3"          # str doit devenir int
        out = tx.validate_nodes(nodes)
        self.assertEqual(out[0]["name"], "Inférence")
        self.assertIsNone(out[0]["parent_id"])
        self.assertEqual(out[0]["position"], 3)

    def test_unknown_parent_rejected(self):
        with self.assertRaises(tx.TaxonomyError):
            tx.validate_nodes([node("Orphelin", tx.new_cat_id())])

    def test_duplicate_id_rejected(self):
        cid = tx.new_cat_id()
        with self.assertRaises(tx.TaxonomyError):
            tx.validate_nodes([node("A", cid=cid), node("B", cid=cid)])

    def test_bad_id_rejected(self):
        with self.assertRaises(tx.TaxonomyError):
            tx.validate_nodes([{"id": "pas-un-uuid", "name": "A"}])

    def test_sibling_name_conflict_rejected(self):
        inf = node("Inférence")
        with self.assertRaises(tx.TaxonomyConflict):
            tx.validate_nodes([inf, node("Tests", inf["id"]),
                               node("tests", inf["id"])])

    def test_same_name_under_different_parents_ok(self):
        a, b = node("A"), node("B", position=1)
        tx.validate_nodes([a, b, node("Tests", a["id"]), node("Tests", b["id"])])

    def test_root_level_conflict_rejected(self):
        with self.assertRaises(tx.TaxonomyConflict):
            tx.validate_nodes([node("Inférence"), node("INFÉRENCE", position=1)])

    def test_max_depth_enforced(self):
        chain, parent = [], None
        for i in range(tx.MAX_DEPTH):
            n = node(f"N{i}", parent)
            chain.append(n)
            parent = n["id"]
        tx.validate_nodes(chain)                    # exactement MAX_DEPTH : OK
        chain.append(node("trop-profond", parent))  # un de plus
        with self.assertRaises(tx.TaxonomyConflict):
            tx.validate_nodes(chain)

    def test_sibling_conflict_helper(self):
        nodes, inf, tests, *_ = tree()
        self.assertTrue(tx.sibling_conflict(nodes, inf["id"], "TESTS"))
        self.assertFalse(tx.sibling_conflict(nodes, inf["id"], "TESTS",
                                             exclude_id=tests["id"]))
        self.assertFalse(tx.sibling_conflict(nodes, inf["id"], "Nouveau"))


class TestAnnotate(unittest.TestCase):
    def test_prefix_order_and_depth(self):
        nodes, *_ = tree()
        out = tx.annotate(tx.validate_nodes(nodes))
        self.assertEqual([(n["name"], n["depth"]) for n in out],
                         [("Inférence", 1), ("Tests", 2), ("Student", 3),
                          ("Intervalles", 2), ("Validation", 1)])

    def test_counts_are_sets_not_sums(self):
        # q1 est classée dans DEUX sous-catégories d'Inférence : elle ne doit
        # compter qu'une fois dans le total du chapitre.
        nodes, inf, tests, _student, ic, _val = tree()
        nodes = tx.validate_nodes(nodes)
        out = {n["name"]: n for n in tx.annotate(
            nodes, {tests["id"]: ["q1", "q2"], ic["id"]: ["q1"]})}
        self.assertEqual((out["Inférence"]["n_direct"], out["Inférence"]["n_total"]), (0, 2))
        self.assertEqual((out["Tests"]["n_direct"], out["Tests"]["n_total"]), (2, 2))
        self.assertEqual((out["Intervalles"]["n_direct"], out["Intervalles"]["n_total"]), (1, 1))
        self.assertEqual(out["Validation"]["n_total"], 0)

    def test_no_node_lost_when_orphan(self):
        a = node("A")
        orphan = node("Orphelin", tx.new_cat_id())   # parent absent
        out = tx.annotate([a, orphan])
        self.assertEqual(len(out), 2)
        self.assertTrue(out[-1].get("orphan"))


class TestSanitizeAssignment(unittest.TestCase):
    def test_keeps_order_drops_unknown_and_dups(self):
        nodes, inf, tests, *_ = tree()
        got = tx.sanitize_assignment(
            [tests["id"], "pas-un-uuid", tx.new_cat_id(), inf["id"], tests["id"], ""],
            nodes)
        self.assertEqual(got, [tests["id"], inf["id"]])

    def test_empty(self):
        nodes, *_ = tree()
        self.assertEqual(tx.sanitize_assignment(None, nodes), [])
        self.assertEqual(tx.sanitize_assignment([], nodes), [])


if __name__ == "__main__":
    unittest.main()
