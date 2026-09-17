"""Variantes d'une question de banque : module pur + backend local.

Isolé par `AMCX_PROJECT_DIR` (config vide → aucune banque configurée) et
`AMCX_BANK_DIR` (racine jetable). Aucune banque réelle n'est touchée.

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

_TMP = Path(tempfile.mkdtemp(prefix="amcx-var-test-"))
os.environ["AMCX_PROJECT_DIR"] = str(_TMP / "projet")
os.environ["AMCX_BANK_DIR"] = str(_TMP / "banque")

import bank                    # noqa: E402
import bank_variants as vr     # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def R(bid, ptr="", created="2026-01-01"):
    return {"bank_id": bid, "variant_of": ptr, "created_at": created}


class PureTest(unittest.TestCase):
    """`bank_variants` seul : aucune I/O, aucune banque."""

    def test_chaine_aplatie(self):
        rows = [R("a"), R("b", "a", "2"), R("c", "b", "3")]
        self.assertEqual(vr.normalize(rows), {"a": "", "b": "a", "c": "a"})
        self.assertEqual(vr.members(rows, "c"), ["a", "b", "c"])

    def test_pointeur_mort_rend_chef(self):
        self.assertEqual(vr.normalize([R("a", "disparu")]), {"a": ""})

    def test_auto_reference_rend_chef(self):
        self.assertEqual(vr.normalize([R("a", "a")]), {"a": ""})

    def test_cycle_coupe_sur_le_plus_ancien(self):
        rows = [R("a", "b", "1"), R("b", "c", "2"), R("c", "a", "3")]
        self.assertEqual(vr.normalize(rows), {"a": "", "b": "a", "c": "a"})

    def test_attach_fusionne_les_deux_groupes(self):
        rows = [R("a", "", "1"), R("b", "a", "2"), R("c", "", "3"), R("d", "c", "4")]
        # c a déjà une variante : elle DOIT suivre, sinon c reste un second
        # chef au même énoncé — le doublon qu'on veut éviter.
        self.assertEqual(vr.attach(rows, "c", "a"), {"c": "a", "d": "a"})

    def test_attach_ne_cree_jamais_de_chaine(self):
        rows = [R("a", "", "1"), R("b", "a", "2"), R("c", "", "3")]
        self.assertEqual(vr.attach(rows, "c", "b"), {"c": "a"})

    def test_attach_dans_son_propre_groupe_refuse(self):
        rows = [R("a", "", "1"), R("b", "a", "2")]
        with self.assertRaises(ValueError):
            vr.attach(rows, "b", "a")
        with self.assertRaises(ValueError):
            vr.attach(rows, "a", "a")

    def test_detach_du_chef_refuse(self):
        rows = [R("a", "", "1"), R("b", "a", "2")]
        self.assertEqual(vr.detach(rows, "b"), {"b": ""})
        with self.assertRaises(ValueError):
            vr.detach(rows, "a")

    def test_promotion_a_la_suppression_du_chef(self):
        rows = [R("a", "", "1"), R("b", "a", "2"), R("c", "a", "3")]
        self.assertEqual(vr.promote_on_delete(rows, "a"), {"b": "", "c": "b"})
        self.assertEqual(vr.promote_on_delete(rows, "b"), {})

    def test_fold_ne_garde_que_les_chefs(self):
        rows = [R("a", "", "1"), R("b", "a", "2"), R("c", "", "3")]
        got = {g["bank_id"]: g["n_variants"] for g in vr.fold(rows)}
        self.assertEqual(got, {"a": 1, "c": 0})

    def test_fold_rend_une_variante_dont_le_chef_est_absent(self):
        # Cas d'un filtre qui ne retient que la variante : la replier sous un
        # chef absent de la liste la rendrait introuvable.
        self.assertEqual([g["bank_id"] for g in vr.fold([R("b", "a", "2")])], ["b"])


class BankVariantsTest(unittest.TestCase):
    """Le backend local : persistance, listing, suppression."""

    def setUp(self):
        os.environ["AMCX_BANK_DIR"] = str(_TMP / "banque")
        shutil.rmtree(_TMP / "banque", ignore_errors=True)
        bank.ensure_root()

    def add(self, title, tags=None, created=""):
        """⚠ `created` explicite là où l'ordre compte : `_now()` est à la
        seconde, donc trois questions créées dans le même test ont la même
        date et ne se départagent plus que par un identifiant tiré au sort."""
        q = bank.from_block(
            {"kind": "question_qcm",
             "data": {"tag": "q", "qtype": "single", "statement": title,
                      "answers": [{"text": "a", "correct": True}]}},
            title=title, tags=tags or [])
        if created:
            q["created_at"] = created
        bank.save(q)
        return q["bank_id"]

    def test_une_question_nait_chef(self):
        a = self.add("Pente")
        self.assertEqual(bank.load(a)["variant_of"], "")
        self.assertEqual(bank.variant_head(a), a)

    def test_liste_repliee_par_defaut(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        bank.set_variant_of(b, a)
        items = bank.list_questions()
        self.assertEqual([i["bank_id"] for i in items], [a])
        self.assertEqual(items[0]["n_variants"], 1)
        self.assertEqual([v["bank_id"] for v in items[0]["variants"]], [b])
        self.assertEqual(len(bank.list_questions({"variants": "all"})), 2)

    def test_une_recherche_qui_touche_la_variante_ramene_le_groupe(self):
        a, b = self.add("Pente"), self.add("Ordonnée à l'origine")
        bank.set_variant_of(b, a)
        items = bank.list_questions({"q": "ordonnée"})
        self.assertEqual([i["bank_id"] for i in items], [a])
        self.assertEqual(items[0]["n_variants"], 1)

    def test_lier_ne_touche_pas_modified_at(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        before = bank.load(b)["modified_at"], bank.load(b)["version"]
        bank.set_variant_of(b, a)
        self.assertEqual((bank.load(b)["modified_at"], bank.load(b)["version"]), before)

    def test_detacher_rend_la_question_autonome(self):
        a, b = self.add("Matin"), self.add("Après-midi")
        bank.set_variant_of(b, a)
        bank.set_variant_of(b, None)
        self.assertEqual(bank.load(b)["variant_of"], "")
        self.assertEqual(len(bank.list_questions()), 2)

    def test_supprimer_le_chef_promeut_sans_rien_perdre(self):
        a = self.add("A", created="2026-01-01T09:00:00")
        b = self.add("B", created="2026-01-01T09:00:01")
        c = self.add("C", created="2026-01-01T09:00:02")
        bank.set_variant_of(b, a)
        bank.set_variant_of(c, a)
        bank.delete(a)
        self.assertEqual(bank.load(b)["variant_of"], "")
        self.assertEqual(bank.load(c)["variant_of"], b)
        self.assertEqual([i["bank_id"] for i in bank.list_questions()], [b])

    def test_repair_persiste_la_reparation(self):
        a, b = self.add("A"), self.add("B")
        q = bank.load(b)
        q["variant_of"] = "8badf00d"          # pointeur mort écrit à la main
        bank.save(q)
        self.assertEqual(bank.repair_variants(), {b: ""})
        self.assertEqual(bank.load(b)["variant_of"], "")
        self.assertEqual(bank.repair_variants(), {})
        self.assertEqual(len(bank.list_questions()), 2)

    def test_groupe_complet_rendu_par_list_variants(self):
        a, b = self.add("A"), self.add("B")
        grp = bank.set_variant_of(b, a)
        self.assertEqual(grp["head"], a)
        self.assertEqual([m["bank_id"] for m in grp["members"]], [a, b])
        self.assertEqual(bank.list_variants(a), grp)


class OnlinePayloadTest(unittest.TestCase):
    """La migration vers une banque en ligne ne doit pas buter sur le champ."""

    def test_variant_of_n_est_pas_envoye_a_postgrest(self):
        # ⚠ `variant_of` n'est pas une colonne du schéma en ligne. Comme
        # `from_block` le pose sur TOUTE question locale, l'oublier dans le
        # `skip` ferait échouer `bank_migrate` en PGRST204 dès la première.
        import bank_online
        q = bank.from_block(
            {"kind": "question_qcm",
             "data": {"tag": "q", "qtype": "single", "statement": "s",
                      "answers": [{"text": "a", "correct": True}]}},
            title="T")
        self.assertIn("variant_of", q)
        self.assertNotIn("variant_of", bank_online._question_to_row(q))


if __name__ == "__main__":
    unittest.main()
