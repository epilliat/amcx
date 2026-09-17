"""Sujet à plusieurs versions : groupes AMC, un `\\exemplaire` par version.

C'est la construction AMC qui donne des questions **toutes différentes** à deux
populations (matin / après-midi) : chaque question est déclarée dans un
`\\element{groupe}{…}` au niveau document, et chaque version ne restitue que son
groupe. La disjonction est garantie par construction, contrairement à un tirage
dans un pool commun (`\\restituegroupe[k]{pool}`) où deux copies partagent des
questions au hasard du tirage.

Ces tests fixent ce qui a été mesuré en important un vrai sujet de ce type :

- l'import ne voyait AUCUNE question (elles vivent hors du `\\exemplaire`) et
  produisait un sujet « canonique » vide, au barème nul ;
- la seconde version disparaissait du store, donc du `.tex` régénéré ;
- le contenu placé après `\\end{reponses}` (un `\\end{multicols}`) était jeté,
  ce qui rendait le sujet non compilable ;
- les numéros AMC d'une copie ne sont pas ses indices d'ordre du document.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import sujet_store as ss   # noqa: E402


# --- Fabriques ------------------------------------------------------------

def qcm(bid, tag, group="", qtype="single"):
    return ss.Block(bid=bid, kind="question_qcm", group=group, data={
        "tag": tag, "qtype": qtype, "env": "reponses",
        "statement": f"Énoncé {tag}",
        "answers": [{"text": "a", "correct": True, "bareme": "1"},
                    {"text": "b", "correct": False, "bareme": "0"}],
        "value": "1"})


def subject(versions, blocks):
    return {"config": ss.SubjectConfig(versions=versions),
            "blocks": blocks, "mode": "canonical"}


TWO_VERSIONS = [
    ss.SubjectVersion(vid="v-1", name="Session du matin", group="morning",
                      num_copies=2, header=ss.HeaderBlock(raw_tex="EN-TÊTE MATIN")),
    ss.SubjectVersion(vid="v-2", name="Après-midi", group="afternoon",
                      num_copies=3, header=ss.HeaderBlock(raw_tex="EN-TÊTE APREM")),
]


# --- Rendu / relecture ----------------------------------------------------

class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.subject = subject(
            [ss.SubjectVersion(**{**v.__dict__}) for v in TWO_VERSIONS],
            [qcm("q1", "tmatin", "morning"), qcm("q2", "taprem", "afternoon")])
        self.tex = ss.render_subject(self.subject)

    def test_one_exemplaire_per_version(self):
        self.assertEqual(self.tex.count("\\exemplaire{"), 2)
        self.assertIn("\\exemplaire{2}{", self.tex)
        self.assertIn("\\exemplaire{3}{", self.tex)

    def test_each_version_restores_only_its_group(self):
        self.assertEqual(self.tex.count("\\restituegroupe{morning}"), 1)
        self.assertEqual(self.tex.count("\\restituegroupe{afternoon}"), 1)

    def test_questions_are_declared_outside_exemplaire(self):
        """Sinon AMC les imprimerait dans les deux versions — ou nulle part."""
        first = self.tex.find("\\exemplaire{")
        self.assertLess(self.tex.find("\\element{morning}{"), first)
        self.assertLess(self.tex.find("\\element{afternoon}{"), first)

    def test_parse_recovers_versions_and_groups(self):
        back = ss.parse_subject(self.tex)
        vs = back["config"].versions
        self.assertEqual([(v.group, v.num_copies) for v in vs],
                         [("morning", 2), ("afternoon", 3)])
        self.assertEqual([b.group for b in back["blocks"]],
                         ["morning", "afternoon"])

    def test_version_name_may_contain_spaces(self):
        """`_parse_attrs` découpe sur les blancs : le nom, libre, est écrit en
        dernier et tout ce qui le suit lui appartient."""
        back = ss.parse_subject(self.tex)
        self.assertEqual(back["config"].versions[0].name, "Session du matin")

    def test_each_version_keeps_its_own_header(self):
        back = ss.parse_subject(self.tex)
        self.assertEqual([v.header.raw_tex for v in back["config"].versions],
                         ["EN-TÊTE MATIN", "EN-TÊTE APREM"])

    def test_render_is_stable_after_one_pass(self):
        t2 = ss.render_subject(ss.parse_subject(self.tex))
        self.assertEqual(t2, ss.render_subject(ss.parse_subject(t2)))

    def test_total_num_copies_is_the_sum(self):
        self.assertEqual(ss.parse_subject(self.tex)["config"].num_copies, 5)


class TestCommonGroup(unittest.TestCase):
    def test_ungrouped_block_is_restored_by_every_version(self):
        """Un bloc sans groupe déclaré au niveau document ne serait imprimé
        nulle part, sans la moindre erreur LaTeX."""
        tex = ss.render_subject(subject(
            TWO_VERSIONS,
            [ss.Block(bid="t1", kind="text", data={"tex": "\\section*{Q}"})]))
        self.assertEqual(tex.count("\\element{%s}{" % ss.COMMON_GROUP), 1)
        self.assertEqual(tex.count("\\restituegroupe{%s}" % ss.COMMON_GROUP), 2)

    def test_text_block_survives_the_element_wrap(self):
        """Le wrap est une décoration de rendu : il ne doit pas s'empiler dans
        `data.tex` au fil des round-trips."""
        subj = subject(TWO_VERSIONS,
                       [ss.Block(bid="t1", kind="text", data={"tex": "\\section*{Q}"})])
        back = ss.parse_subject(ss.render_subject(subj))
        self.assertEqual(back["blocks"][0].data["tex"], "\\section*{Q}")
        again = ss.parse_subject(ss.render_subject(back))
        self.assertEqual(again["blocks"][0].data["tex"], "\\section*{Q}")

    def test_orphan_group_falls_back_to_common(self):
        """Un bloc dont la version a été supprimée reste imprimé."""
        tex = ss.render_subject(subject(TWO_VERSIONS,
                                        [qcm("q1", "t", "version_disparue")]))
        self.assertIn("\\element{%s}{" % ss.COMMON_GROUP, tex)
        self.assertNotIn("version_disparue", tex.replace("group=", ""))


# --- Numérotation des copies ---------------------------------------------

class TestCopyRanges(unittest.TestCase):
    """AMC numérote les copies en continu d'un `\\exemplaire` au suivant — c'est
    ce qui permet de scanner les deux demi-journées dans le même lot."""

    def setUp(self):
        self.cfg = ss.SubjectConfig(versions=TWO_VERSIONS)

    def test_ranges_are_contiguous(self):
        self.assertEqual(ss.version_copy_ranges(self.cfg), [(1, 2), (3, 5)])

    def test_version_of_copy(self):
        got = [ss.version_of_copy(self.cfg, c) for c in (1, 2, 3, 4, 5)]
        self.assertEqual([v.group for v in got],
                         ["morning", "morning", "afternoon", "afternoon", "afternoon"])

    def test_copy_outside_any_version(self):
        self.assertIsNone(ss.version_of_copy(self.cfg, 6))

    def test_no_versions_means_no_ranges(self):
        self.assertEqual(ss.version_copy_ranges(ss.SubjectConfig()), [])


# --- Détection du format à groupes ---------------------------------------

GROUPED_TEX = r"""
\documentclass{article}
\usepackage[bloc,ensemble]{automultiplechoice}
\begin{document}
\AMCrandomseed{42}
\element{morning}{
\begin{question}{qm}
Question du matin ?
\begin{reponses}
\bonne{oui}
\mauvaise{non}
\end{reponses}
\end{question}
}
\element{afternoon}{
\begin{questionmult}{qa}
Question de l'après-midi ?
\begin{reponses}
\bonne{a}\bareme{b=1,m=0}
\mauvaise{b}\bareme{b=0,m=-1}
\end{reponses}
\end{questionmult}
}
\exemplaire{1}{
En-tête du matin.
\restituegroupe{morning}
\AMCdebutFormulaire
\formulaire
}
\exemplaire{1}{
En-tête de l'après-midi.
\restituegroupe{afternoon}
\AMCdebutFormulaire
\formulaire
}
\end{document}
"""


class TestGroupedDetection(unittest.TestCase):
    def test_split_finds_both_versions(self):
        split = ss._split_grouped_tex(GROUPED_TEX)
        self.assertIsNotNone(split)
        self.assertEqual([v["group"] for v in split["versions"]],
                         ["morning", "afternoon"])
        self.assertEqual([e["group"] for e in split["elements"]],
                         ["morning", "afternoon"])

    def test_header_excludes_the_restore_command(self):
        """`render_subject` la réémet : la garder la dupliquerait."""
        split = ss._split_grouped_tex(GROUPED_TEX)
        self.assertNotIn("restituegroupe", split["versions"][0]["header"])
        self.assertIn("En-tête du matin", split["versions"][0]["header"])

    def test_parse_exposes_the_questions(self):
        subj = ss.parse_subject(GROUPED_TEX)
        tags = [(b.data.get("tag"), b.group) for b in subj["blocks"]
                if b.kind == "question_qcm"]
        self.assertEqual(tags, [("qm", "morning"), ("qa", "afternoon")])

    def test_a_plain_amcx_subject_is_not_read_as_versions(self):
        """Avec `shuffle_questions`, AMCx pose des `\\element{questions}{…}` —
        mais À L'INTÉRIEUR du `\\exemplaire`, et sur un groupe réservé."""
        cfg = ss.SubjectConfig(shuffle_questions=True)
        tex = ss.render_subject({"config": cfg, "blocks": [qcm("q1", "t")],
                                 "mode": "canonical"})
        self.assertIn("\\element{questions}{", tex)
        self.assertIsNone(ss._split_grouped_tex(tex))

    def test_elements_without_any_exemplaire_restoring_them(self):
        tex = GROUPED_TEX.replace("\\restituegroupe{morning}", "") \
                         .replace("\\restituegroupe{afternoon}", "")
        self.assertIsNone(ss._split_grouped_tex(tex))


# --- Contenu conservé -----------------------------------------------------

class TestEpilogue(unittest.TestCase):
    """Un sujet qui met ses réponses en colonnes ouvre `\\begin{multicols}` dans
    l'énoncé et le referme APRÈS `\\end{reponses}`. Jeter cette fin produisait un
    environnement jamais fermé, donc un sujet qui ne compile plus."""

    BODY = ("Quelle formule ?\n\\begin{multicols}{2}\n"
            "\\begin{reponses}\n\\bonne{A}\n\\mauvaise{B}\n\\end{reponses}\n"
            "\\end{multicols}\n")

    def test_parse_keeps_what_follows_the_answers(self):
        info = ss._parse_block(self.BODY, "question", "t")
        self.assertEqual(info["epilogue"], "\\end{multicols}")

    def test_render_emits_it_after_the_answers(self):
        info = ss._parse_block(self.BODY, "question", "t")
        out = ss._render_qcm_body({
            "tag": "t", "qtype": "single", "env": "reponses",
            "statement": info["statement"], "value": "1",
            "epilogue": info["epilogue"],
            "answers": [{"text": a["text"], "correct": a["correct"], "bareme": "0"}
                        for a in info["answers"]]})
        self.assertLess(out.index("\\end{reponses}"), out.index("\\end{multicols}"))
        self.assertEqual(out.count("\\begin{multicols}"), 1)
        self.assertEqual(out.count("\\end{multicols}"), 1)

    def test_question_level_bareme_is_not_left_in_the_statement(self):
        """Il est déjà lu dans `value` et réémis : le laisser produisait deux
        `\\bareme` dans la même question dès qu'on changeait la valeur."""
        info = ss._parse_block(
            "\\bareme{b=2,m=0}\nÉnoncé.\n\\begin{reponses}\n\\bonne{A}\n"
            "\\end{reponses}\n", "question", "t")
        self.assertEqual(info["bareme"]["value"], "2")
        self.assertNotIn("bareme", info["statement"])
        self.assertIn("Énoncé.", info["statement"])


class TestStripElementWrap(unittest.TestCase):
    def test_strips_a_full_wrap(self):
        self.assertEqual(ss._strip_element_wrap("\\element{g}{\nX\n}", "g"), "X")

    def test_leaves_a_partial_wrap_alone(self):
        """Si le wrap ne couvre pas tout le corps, on ne comprend pas la
        structure : mieux vaut ne rien toucher que couper au mauvais endroit."""
        body = "\\element{g}{A} et B"
        self.assertEqual(ss._strip_element_wrap(body, "g"), body)

    def test_other_group_untouched(self):
        body = "\\element{autre}{X}"
        self.assertEqual(ss._strip_element_wrap(body, "g"), body)

    def test_unbalanced_braces_are_not_a_crash(self):
        body = "\\element{g}{X"
        self.assertEqual(ss._strip_element_wrap(body, "g"), body)


# --- Garde-fou de migration ----------------------------------------------

class TestMigrationGuard(unittest.TestCase):
    """Un sujet dont les questions n'ont pas été comprises ne doit PAS être
    enregistré comme canonique : l'éditeur serait vide, le barème nul, donc
    toutes les copies notées zéro — en silence."""

    def test_refuses_when_all_questions_are_lost(self):
        tex = "\\begin{question}{q}\n\\begin{reponses}\n\\bonne{a}\n" \
              "\\end{reponses}\n\\end{question}"
        msg = ss._migration_lost_questions(tex, [])
        self.assertIsNotNone(msg)
        self.assertIn("1 question", msg)

    def test_accepts_when_questions_were_parsed(self):
        tex = "\\begin{question}{q}\\end{question}"
        self.assertIsNone(ss._migration_lost_questions(tex, [qcm("q1", "q")]))

    def test_a_subject_without_questions_is_not_a_failure(self):
        self.assertIsNone(ss._migration_lost_questions("du texte", []))


class TestGroupSlug(unittest.TestCase):
    """AMC construit un nom de macro depuis le nom de groupe : tout ce qui n'est
    pas une lettre ASCII casse la compilation sans message utilisable."""

    def test_accents_and_punctuation_are_removed(self):
        self.assertEqual(ss._slug_group("Après-midi"), "apresmidi")

    def test_digits_and_spaces_are_removed(self):
        self.assertEqual(ss._slug_group("Groupe 2 (bis)"), "groupebis")

    def test_never_empty(self):
        self.assertEqual(ss._slug_group("123"), "grp")
        self.assertEqual(ss._slug_group(""), "grp")


# --- CRUD des versions (ajout / suppression / annulation) -----------------

class _StoreCase(unittest.TestCase):
    """Base : un `subject.json` jetable, pour les fonctions qui écrivent.

    ⚠ `sujet_store` fige ses chemins à l'import (SUJET_DIR vient du projet
    actif). On les repointe donc par instance, et on les restaure : un test qui
    écrirait dans le projet de l'utilisateur détruirait son sujet.
    """

    def setUp(self):
        import json
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._saved = (ss.SUJET_DIR, ss.SUBJECT_JSON, ss.EXAM_TEX)
        ss.SUJET_DIR = d
        ss.SUBJECT_JSON = d / "subject.json"
        ss.EXAM_TEX = d / "exam.tex"
        ss._invalidate_caches()
        self._json = json

    def tearDown(self):
        ss.SUJET_DIR, ss.SUBJECT_JSON, ss.EXAM_TEX = self._saved
        ss._invalidate_caches()
        self._tmp.cleanup()

    def write(self, versions, blocks, **cfg_kw):
        sub = {"config": ss.SubjectConfig(versions=versions, **cfg_kw),
               "blocks": blocks, "mode": "canonical"}
        ss.save_subject_store(sub)
        return sub

    def load(self):
        return ss.parse_subject()


class TestAddVersion(_StoreCase):
    def test_bootstrap_materialise_la_version_courante(self):
        """Un sujet SANS versions en gagne DEUX au premier ajout.

        Sans ça, la nouvelle version serait la seule déclarée et restituerait
        les questions existantes (rangées en « commun ») : le sujet entier
        s'imprimerait des deux côtés.
        """
        self.write([], [qcm("q1", "t1")], num_copies=7,
                   header=ss.HeaderBlock(title="Examen"))
        out = ss.add_version(name="Après-midi", num_copies=3)
        self.assertTrue(out["bootstrapped"])
        cfg = self.load()["config"]
        self.assertEqual(len(cfg.versions), 2)
        self.assertEqual(cfg.versions[0].num_copies, 7)       # copies héritées
        self.assertEqual(cfg.versions[0].header.title, "Examen")
        self.assertEqual(cfg.versions[1].name, "Après-midi")
        self.assertEqual(cfg.num_copies, 10)                  # 7 + 3

    def test_les_questions_existantes_restent_communes(self):
        self.write([], [qcm("q1", "t1")])
        ss.add_version(name="Aprem")
        self.assertEqual(self.load()["blocks"][0].group, "")

    def test_second_ajout_ne_rebootstrappe_pas(self):
        self.write([], [qcm("q1", "t1")])
        ss.add_version(name="B")
        out = ss.add_version(name="C")
        self.assertFalse(out["bootstrapped"])
        self.assertEqual(len(self.load()["config"].versions), 3)

    def test_groupe_unique_meme_avec_des_noms_qui_se_ressemblent(self):
        """Deux versions au même groupe restitueraient les MÊMES questions."""
        self.write([], [])
        ss.add_version(name="Matin")
        ss.add_version(name="Matin 2")     # slug identique : « matin »
        groups = [v.group for v in self.load()["config"].versions]
        self.assertEqual(len(set(groups)), len(groups))

    def test_groupe_reserve_refuse(self):
        """`questions` / `open` / `bareme` servent au rendu AMCx lui-même."""
        self.write([], [])
        ss.add_version(name="questions")
        self.assertNotIn(self.load()["config"].versions[-1].group,
                         ss.RESERVED_GROUPS)


class TestDeleteVersion(_StoreCase):
    def setUp(self):
        super().setUp()
        self.write([ss.SubjectVersion(vid="v-1", name="Matin", group="morning"),
                    ss.SubjectVersion(vid="v-2", name="Aprem", group="afternoon")],
                   [qcm("q1", "t1", "morning"), qcm("q2", "t2", "afternoon"),
                    qcm("q3", "t3", "")])

    def test_reparent_ne_supprime_aucune_question(self):
        undo = ss.delete_version("v-1", mode="reparent")
        sub = self.load()
        self.assertEqual(len(sub["blocks"]), 3)
        self.assertEqual(sub["blocks"][0].group, "")     # devenue commune
        self.assertEqual(undo["regrouped"], ["q1"])

    def test_delete_blocks_emporte_les_questions_de_la_version(self):
        undo = ss.delete_version("v-2", mode="delete_blocks")
        bids = [b.bid for b in self.load()["blocks"]]
        self.assertEqual(bids, ["q1", "q3"])
        self.assertEqual([e["block"]["bid"] for e in undo["removed"]], ["q2"])

    def test_derniere_version_rend_un_sujet_a_une_version(self):
        ss.delete_version("v-1")
        ss.delete_version("v-2")
        cfg = self.load()["config"]
        self.assertEqual(cfg.versions, [])
        self.assertEqual(cfg.num_copies, 1)

    def test_mode_inconnu_refuse(self):
        with self.assertRaises(ValueError):
            ss.delete_version("v-1", mode="nuke")

    def test_vid_inconnu(self):
        with self.assertRaises(KeyError):
            ss.delete_version("v-inexistante")


class TestRestoreVersion(_StoreCase):
    def setUp(self):
        super().setUp()
        self.blocks = [qcm("q1", "t1", "morning"), qcm("q2", "t2", "afternoon"),
                       qcm("q3", "t3", "morning")]
        self.write([ss.SubjectVersion(vid="v-1", name="Matin", group="morning",
                                      num_copies=4),
                    ss.SubjectVersion(vid="v-2", name="Aprem", group="afternoon")],
                   [ss.Block(bid=b.bid, kind=b.kind, data=b.data, group=b.group)
                    for b in self.blocks])

    def _snapshot(self):
        sub = self.load()
        return ([(v.vid, v.name, v.group, v.num_copies) for v in sub["config"].versions],
                [(b.bid, b.group) for b in sub["blocks"]])

    def test_reparent_est_reversible_au_detail_pres(self):
        before = self._snapshot()
        undo = ss.delete_version("v-1", mode="reparent")
        ss.restore_version(undo)
        self.assertEqual(self._snapshot(), before)

    def test_delete_blocks_est_reversible_position_comprise(self):
        before = self._snapshot()
        undo = ss.delete_version("v-1", mode="delete_blocks")
        self.assertEqual([b.bid for b in self.load()["blocks"]], ["q2"])
        ss.restore_version(undo)
        self.assertEqual(self._snapshot(), before)

    def test_restauration_de_la_derniere_version_rend_len_tete(self):
        ss.delete_version("v-2")
        undo = ss.delete_version("v-1")
        self.assertEqual(self.load()["config"].versions, [])
        ss.restore_version(undo)
        cfg = self.load()["config"]
        self.assertEqual([v.vid for v in cfg.versions], ["v-1"])
        self.assertEqual(cfg.versions[0].num_copies, 4)

    def test_restaurer_deux_fois_est_refuse(self):
        undo = ss.delete_version("v-1")
        ss.restore_version(undo)
        with self.assertRaises(ValueError):
            ss.restore_version(undo)

    def test_payload_invalide_refuse(self):
        with self.assertRaises(ValueError):
            ss.restore_version({})


class TestSetBlockGroup(_StoreCase):
    def setUp(self):
        super().setUp()
        self.write([ss.SubjectVersion(vid="v-1", name="Matin", group="morning"),
                    ss.SubjectVersion(vid="v-2", name="Aprem", group="afternoon")],
                   [qcm("q1", "t1", "morning")])

    def test_affectation_et_groupe_precedent(self):
        prev = ss.set_block_group("q1", "afternoon")
        self.assertEqual(prev, "morning")
        self.assertEqual(self.load()["blocks"][0].group, "afternoon")

    def test_commun_est_stocke_vide(self):
        """`commun` est le nom d'affichage ; le store, lui, note l'absence."""
        ss.set_block_group("q1", ss.COMMON_GROUP)
        self.assertEqual(self.load()["blocks"][0].group, "")

    def test_groupe_sans_version_refuse(self):
        """Le rendu le traiterait en commun : imprimé partout, en silence."""
        with self.assertRaises(ValueError):
            ss.set_block_group("q1", "evening")

    def test_bloc_inconnu(self):
        with self.assertRaises(KeyError):
            ss.set_block_group("q-nope", "morning")


# --- En-tête : champs structurés ------------------------------------------

class TestHeaderFields(unittest.TestCase):
    """`date` et `rules` ont été ajoutés pour ne plus avoir à passer au LaTeX
    brut sur les en-têtes réels observés (date de l'épreuve, filets autour des
    consignes)."""

    def test_date_et_filets_font_laller_retour(self):
        h = ss.HeaderBlock(establishment="ENSAI", date="8/9/2026",
                           duration="10 min", subtitle="Morning", rules=True,
                           instructions="Aucun document.")
        back = ss._parse_header_tex(ss.render_header(h))
        for k in ("establishment", "date", "duration", "subtitle",
                  "instructions", "rules"):
            self.assertEqual(getattr(back, k), getattr(h, k), k)

    def test_les_filets_encadrent_les_instructions(self):
        tex = ss.render_header(ss.HeaderBlock(instructions="Consignes.", rules=True))
        self.assertEqual(tex.count("\\hrule"), 2)
        self.assertLess(tex.index("\\hrule"), tex.index("Consignes."))
        self.assertGreater(tex.rindex("\\hrule"), tex.index("Consignes."))

    def test_pas_de_filets_par_defaut(self):
        self.assertNotIn("\\hrule",
                         ss.render_header(ss.HeaderBlock(instructions="X")))

    def test_pas_de_tableau_vide(self):
        """Un en-tête sans identité laissait une bande blanche en haut de page."""
        tex = ss.render_header(ss.HeaderBlock(title="Examen", instructions="X"))
        self.assertNotIn("tabular", tex)

    def test_duree_seule_est_rendue(self):
        """Durée et sous-titre étaient subordonnés au titre : sans titre, rien
        ne sortait — en silence."""
        tex = ss.render_header(ss.HeaderBlock(duration="2 h", subtitle="Calc. OK"))
        self.assertIn("2 h", tex)
        self.assertIn("Calc. OK", tex)

    def test_pas_de_saut_de_ligne_orphelin(self):
        tex = ss.render_header(ss.HeaderBlock(title="Examen"))
        self.assertNotIn("\\\\\n\\end{center}", tex)

    def test_en_tete_sans_date_reste_inchange(self):
        """Le rendu d'un en-tête déjà en service ne doit pas bouger d'un octet :
        son `.xy` — donc le calage des copies déjà imprimées — en dépend."""
        h = ss.HeaderBlock(establishment="ENSAI - 1A", year="2025-2026",
                           author="E. Pilliat", title="Examen", duration="2 h",
                           subtitle="Calculatrice autorisée",
                           instructions="Consignes.")
        self.assertIn("{\\sc ENSAI - 1A} & \\hfill 2025-2026 \\\\\n & \\hfill E. Pilliat",
                      ss.render_header(h))


if __name__ == "__main__":
    unittest.main()


class TestUpdateBlockCarriesHiddenKeys(unittest.TestCase):
    """L'éditeur ne renvoie que ce qu'il édite. Les clés qu'il ignore doivent
    survivre à un enregistrement, sinon ouvrir une question suffit à casser le
    sujet (`\\end{multicols}` perdu) ou à couper une question de sa banque."""

    def test_carried_keys_are_declared(self):
        self.assertIn("epilogue", ss._CARRIED_DATA_KEYS)
        self.assertIn("_bank_id", ss._CARRIED_DATA_KEYS)


class TestProjectNameValidation(unittest.TestCase):
    """Le contrôle du nom de projet n'acceptait que l'ASCII : « Régression »
    était refusé, avec un message annonçant « lettres » qui ne disait pas
    pourquoi. On n'interdit donc que ce qui casse vraiment un chemin."""

    @staticmethod
    def _err(name):
        import project_state
        return project_state.project_name_error(name)

    def test_accents_are_accepted(self):
        for n in ("Régression linéaire", "Épreuve août", "Rattrapage n°2"):
            self.assertIsNone(self._err(n), n)

    def test_path_separators_are_refused(self):
        self.assertIn("interdit", self._err("a/b") or "")
        self.assertIn("interdit", self._err("a\\b") or "")

    def test_dot_names_are_refused(self):
        self.assertIsNotNone(self._err(".."))
        self.assertIsNotNone(self._err("."))

    def test_trailing_dot_or_space_is_refused(self):
        """Windows les retire en silence : le dossier ne porterait pas le nom
        affiché à l'utilisateur."""
        self.assertIsNotNone(self._err("examen."))
        self.assertIsNotNone(self._err("examen "))

    def test_windows_reserved_names(self):
        self.assertIsNotNone(self._err("CON"))
        self.assertIsNotNone(self._err("nul.txt"))
        self.assertIsNone(self._err("console"))

    def test_empty_and_too_long(self):
        self.assertIsNotNone(self._err(""))
        self.assertIsNotNone(self._err("a" * 101))


class TestFolderBrowsing(unittest.TestCase):
    """Le sélecteur de dossier est borné au dossier personnel : le serveur n'a
    aucune authentification et `--host` permet de l'exposer, donc une route qui
    énumère n'importe quel dossier serait une primitive de reconnaissance."""

    def setUp(self):
        import project_state
        self.ps = project_state

    def test_home_is_the_root_and_has_no_parent(self):
        from pathlib import Path
        self.assertEqual(self.ps.browse_root(), Path.home())
        self.assertEqual(self.ps.display_dir(Path.home()), "~")

    def test_escaping_home_is_refused(self):
        from pathlib import Path
        for bad in (Path("/etc"), Path("/"), Path.home() / ".." / ".."):
            with self.assertRaises(ValueError):
                self.ps.list_subdirs(bad)

    def test_missing_directory_raises_not_a_directory(self):
        from pathlib import Path
        with self.assertRaises(NotADirectoryError):
            self.ps.list_subdirs(Path.home() / "n-existe-vraiment-pas-42")

    def test_resolve_dir_expands_tilde(self):
        from pathlib import Path
        self.assertEqual(self.ps.resolve_dir("~/Documents"), Path.home() / "Documents")

    def test_resolve_dir_empty_falls_back_to_default_root(self):
        self.assertEqual(self.ps.resolve_dir(""), self.ps.DEFAULT_PROJECTS_ROOT)
        self.assertEqual(self.ps.resolve_dir(None), self.ps.DEFAULT_PROJECTS_ROOT)

    def test_hidden_directories_are_skipped(self):
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=Path.home()) as td:
            root = Path(td)
            (root / ".cache").mkdir()
            (root / "visible").mkdir()
            (root / "un fichier.txt").write_text("x")
            names = [d["name"] for d in self.ps.list_subdirs(root)]
            self.assertEqual(names, ["visible"])
            del os


class TestMakeSubdir(unittest.TestCase):
    """Créer un dossier depuis le sélecteur : même borne que la lecture, et un
    nom qui obéit aux mêmes règles qu'un nom de projet (il peut en devenir un)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import project_state
        self.ps = project_state
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_and_returns_the_path(self):
        got = self.ps.make_subdir(self.root, "Session 2026")
        self.assertTrue(got.is_dir())
        self.assertEqual(got, self.root / "Session 2026")

    def test_existing_name_is_refused(self):
        self.ps.make_subdir(self.root, "a")
        with self.assertRaises(FileExistsError):
            self.ps.make_subdir(self.root, "a")

    def test_separator_in_name_cannot_escape(self):
        for bad in ("a/b", "..", "../evil", "a\\b"):
            with self.assertRaises(ValueError):
                self.ps.make_subdir(self.root, bad)

    def test_parent_outside_home_is_refused(self):
        from pathlib import Path
        with self.assertRaises(ValueError):
            self.ps.make_subdir(Path("/tmp"), "x")

    def test_missing_parent(self):
        with self.assertRaises(NotADirectoryError):
            self.ps.make_subdir(self.root / "absent", "x")

    def test_accented_name_is_accepted(self):
        self.assertTrue(self.ps.make_subdir(self.root, "Épreuves été").is_dir())


class TestPdfPageMapping(unittest.TestCase):
    """Le calage numérote les pages **par copie** : `\\page{2/1/58}` est la 1re
    page de la copie 2, soit la 3e page du PDF. Confondre les deux plaçait les
    régions d'aperçu de la seconde version sur les pages de la première."""

    def _layout(self, copy, page_ids):
        import layout_store
        return layout_store.Layout(dpi=300, pages={}, boxes=[], zones=[],
                                   answer_sheet_page=1, copy=copy,
                                   page_ids=tuple(page_ids))

    def test_second_copy_starts_after_the_first(self):
        ids = [(1, 1, 60), (1, 2, 59), (2, 1, 58), (2, 2, 57)]
        self.assertEqual(self._layout(1, ids).pdf_page(1), 1)
        self.assertEqual(self._layout(1, ids).pdf_page(2), 2)
        self.assertEqual(self._layout(2, ids).pdf_page(1), 3)
        self.assertEqual(self._layout(2, ids).pdf_page(2), 4)

    def test_versions_with_different_page_counts(self):
        """La position dans la liste des triplets est la seule source : aucune
        hypothèse sur un nombre de pages constant d'une version à l'autre."""
        ids = [(1, 1, 9), (1, 2, 8), (1, 3, 7), (2, 1, 6), (2, 2, 5)]
        self.assertEqual(self._layout(2, ids).pdf_page(1), 4)
        self.assertEqual(self._layout(2, ids).pdf_page(2), 5)

    def test_without_triplets_the_page_is_unchanged(self):
        """Calage sans code imprimé : exact pour un sujet à une seule copie."""
        self.assertEqual(self._layout(1, []).pdf_page(2), 2)


class TestVersionHeader(_StoreCase):
    """⚠ Avec des versions, `config.header` n'est imprimé nulle part : chaque
    `\\exemplaire` rend le sien. Le formulaire d'en-tête doit donc viser une
    version, sans quoi il édite un en-tête qu'aucune copie ne porte."""

    def setUp(self):
        super().setUp()
        self.write([ss.SubjectVersion(vid="v-1", name="Matin", group="morning",
                                      header=ss.HeaderBlock(raw_tex="BRUT MATIN")),
                    ss.SubjectVersion(vid="v-2", name="Aprem", group="afternoon",
                                      header=ss.HeaderBlock(raw_tex="BRUT APREM"))],
                   [qcm("q1", "t1", "morning")])

    def test_le_patch_ne_touche_que_la_version_visee(self):
        ss.update_version("v-2", {"header": {"raw_tex": "", "title": "Examen",
                                             "date": "8/9/2026", "rules": True}})
        cfg = self.load()["config"]
        self.assertEqual(cfg.versions[0].header.raw_tex, "BRUT MATIN")
        self.assertEqual(cfg.versions[1].header.title, "Examen")
        self.assertEqual(cfg.versions[1].header.date, "8/9/2026")
        self.assertTrue(cfg.versions[1].header.rules)

    def test_chaque_version_rend_son_propre_en_tete(self):
        ss.update_version("v-2", {"header": {"raw_tex": "", "title": "Examen"}})
        tex = ss.render_subject(self.load())
        self.assertIn("BRUT MATIN", tex)
        self.assertIn("\\textbf{Examen}", tex)

    def test_une_cle_inconnue_est_ignoree(self):
        ss.update_version("v-1", {"header": {"couleur": "rouge"}})
        self.assertFalse(hasattr(self.load()["config"].versions[0].header, "couleur"))


# --- Décomposition d'un en-tête brut en champs ----------------------------

# Les deux en-têtes RÉELS du dépôt, verbatim. Ce sont eux qui ont dicté le
# découpage ; les figer ici, c'est empêcher qu'une « amélioration » du parseur
# les casse en silence.
HDR_ENSAI = (
    "%%% beginning of the exam header:\n\n\\noindent\n"
    "\\textsc{ENSAI - 2A - MCQ on Linear and Generalized Regression}"
    " \\hfill Duration: 10 min\\\\\n8/9/2026 - Morning\n\n\n"
    "\\vspace{1cm}\n\\hrule\n\\vspace{0.4cm}\n\n"
    "  No documents are allowed.\n  The use of a calculator is forbidden.\n\n"
    "  Questions displaying the symbol \\multiSymbole{} may have\n"
    "  one or several correct answers.\n\n\\smallskip\n\n"
    "{ \\bf Answers must be given exclusively at the end of the paper.}\n\n"
    "\\vspace{0.2cm}\n\\hrule\n\\vspace{0.5cm}\n\n%%% end of the header\n"
    "%\\AMCcleardoublepage\n\n% \\AMCaddpagesto{3}"
)

HDR_EXAM2026 = (
    "%%% debut de l'en-tete des copies :\n\n\\noindent\n"
    "\\textsc{ENSAI - 1A} \\hfill \\textsc{Ann\\'ee 2025-2026} \n\\par\n"
    "\\noindent\nEmmanuel Pilliat \n\\vspace{0.4cm}\n%\\hrule\n\\vspace{0.4cm}\n\n"
    "\\begin{center}\n"
    "{\\bf Examen : Introduction aux Tests d'Hypoth\\`ese (dur\\'ee : 2 heures) \\\\}\n"
    "\\vspace{0.2cm}\n{\\it Calculatrice autoris\\'ee.}\n\\end{center}\n\n"
    "\\vspace{0.5cm}\n\\hrule\n\\vspace{0.4cm}\n\n"
    "Les premiers exercices sont des QCM.\n\n"
    "\\vspace{0.2cm}\n\\hrule\n\\vspace{0.5cm}\n"
)


class TestHeaderAnalysis(unittest.TestCase):
    def test_en_tete_ensai_entierement_decompose(self):
        r = ss.analyze_header_tex(HDR_ENSAI)
        f = r["fields"]
        self.assertTrue(r["ok"], r["leftovers"])
        self.assertEqual(f["establishment"],
                         "ENSAI - 2A - MCQ on Linear and Generalized Regression")
        self.assertEqual(f["duration"], "Duration: 10 min")
        self.assertEqual(f["date"], "8/9/2026")
        self.assertEqual(f["subtitle"], "Morning")   # « 8/9/2026 - Morning »
        self.assertTrue(f["rules"])
        self.assertIn("No documents are allowed.", f["instructions"])
        self.assertIn("\\multiSymbole{}", f["instructions"])
        # ⚠ Les lignes vides sont des fins de paragraphe LaTeX : les jeter avec
        # la présentation collait les trois consignes en un seul bloc.
        self.assertIn("\n\n", f["instructions"])
        self.assertEqual(f["instructions"].count("\n\n"), 2)

    def test_en_tete_exam2026_entierement_decompose(self):
        r = ss.analyze_header_tex(HDR_EXAM2026)
        f = r["fields"]
        self.assertTrue(r["ok"], r["leftovers"])
        self.assertEqual(f["establishment"], "ENSAI - 1A")
        self.assertEqual(f["year"], "Ann\\'ee 2025-2026")
        self.assertEqual(f["author"], "Emmanuel Pilliat")
        self.assertEqual(f["title"], "Examen : Introduction aux Tests d'Hypoth\\`ese")
        self.assertEqual(f["duration"], "dur\\'ee : 2 heures")
        self.assertEqual(f["subtitle"], "Calculatrice autoris\\'ee.")

    def test_accents_latex_ne_masquent_pas_la_duree(self):
        """`dur\\'ee` s'écrit ainsi dans les vrais sujets : chercher « durée »
        n'y trouvait rien, et la durée restait collée au titre."""
        r = ss.analyze_header_tex(
            "\\begin{center}{\\bf Examen (dur\\'ee : 2 heures)}\\end{center}")
        self.assertEqual(r["fields"]["duration"], "dur\\'ee : 2 heures")
        self.assertEqual(r["fields"]["title"], "Examen")

    def test_le_commentaire_ne_compte_pas_comme_filet(self):
        r = ss.analyze_header_tex("\\noindent X\n%\\hrule\n")
        self.assertFalse(r["fields"]["rules"])

    def test_une_structure_nest_pas_coupee_en_deux_champs(self):
        """Un `tabular` réparti sur deux champs donne un sujet qui ne compile
        plus. Il ressort en « non reconnu »."""
        r = ss.analyze_header_tex(
            "\\begin{tabular}{ll} A & B \\\\ C & D \\end{tabular}\n\\hrule\nX")
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["leftovers"]), 2)
        self.assertFalse(r["fields"]["establishment"])

    def test_une_commande_qui_nest_pas_du_texte_est_refusee(self):
        """Un logo à la place du nom de l'établissement serait plausible et
        faux ; et l'accolade finale y était mangée au passage."""
        r = ss.analyze_header_tex(
            "\\includegraphics[width=3cm]{logo.png} \\hfill 2025-2026\n\\hrule\nX")
        self.assertFalse(r["ok"])
        self.assertIn("\\includegraphics[width=3cm]{logo.png}", r["leftovers"])
        self.assertEqual(r["fields"]["year"], "2025-2026")

    def test_cellules_en_trop_signalees_et_non_avalees(self):
        r = ss.analyze_header_tex(
            "\\textsc{A} \\hfill B \\hfill C \\hfill D \\hfill E\\\\\n\\hrule\nX")
        self.assertFalse(r["ok"])
        self.assertEqual(r["leftovers"], ["D", "E"])

    def test_en_tete_vide_ou_tout_commente(self):
        for raw in ("", "   \n\n", "% rien\n%% rien non plus"):
            r = ss.analyze_header_tex(raw)
            self.assertFalse(r["ok"])
            self.assertEqual(r["leftovers"], [])

    def test_annee_seule(self):
        r = ss.analyze_header_tex("\\textsc{IUT} \\hfill 2026")
        self.assertEqual(r["fields"]["year"], "2026")
        self.assertEqual(r["fields"]["establishment"], "IUT")

    def test_le_resultat_repasse_par_le_rendu(self):
        """Ce qui est proposé doit être rendable : accolades équilibrées et
        aucune structure — sinon on écrit un sujet qui ne compile plus."""
        for raw in (HDR_ENSAI, HDR_EXAM2026):
            f = ss.analyze_header_tex(raw)["fields"]
            tex = ss.render_header(ss.HeaderBlock(**f))
            self.assertTrue(ss._hdr_braces_balanced(tex), raw[:30])
            self.assertNotIn("\\begin{tabular}{ll}", tex)

    def test_analyse_pure_naucun_effet_de_bord(self):
        """`analyze_header_tex` ne doit rien écrire : c'est une proposition."""
        before = ss.analyze_header_tex(HDR_ENSAI)
        after = ss.analyze_header_tex(HDR_ENSAI)
        self.assertEqual(before, after)


class TestVersionTotalMax(_StoreCase):
    """⚠ Le barème d'une version se calcule depuis SES questions, pas depuis le
    numéro de sa première copie. En le lisant par `total_max(first_copy)`, il
    disparaissait de l'après-midi dès qu'on changeait le nombre de copies du
    matin : le décalage sortait sa première copie du calage compilé."""

    def setUp(self):
        super().setUp()
        self.write([ss.SubjectVersion(vid="v-1", name="Matin", group="morning"),
                    ss.SubjectVersion(vid="v-2", name="Aprem", group="afternoon")],
                   [qcm("q1", "t1", "morning"), qcm("q2", "t2", "morning"),
                    qcm("q3", "t3", "afternoon"),
                    qcm("q4", "commune", "")])

    def test_chaque_version_compte_ses_questions_et_les_communes(self):
        sub = self.load()
        # 1 pt par question : matin = 2 propres + 1 commune, aprem = 1 + 1.
        self.assertEqual(ss.version_total_max(sub, "morning"), 3.0)
        self.assertEqual(ss.version_total_max(sub, "afternoon"), 2.0)

    def test_le_nombre_de_copies_ne_change_pas_le_bareme(self):
        before = ss.version_total_max(self.load(), "afternoon")
        ss.update_version("v-1", {"num_copies": 22})
        self.assertEqual(ss.version_total_max(self.load(), "afternoon"), before)

    def test_un_groupe_sans_version_ne_vaut_que_les_communes(self):
        """Une version tout juste créée affiche son barème, sans compilation."""
        self.assertEqual(ss.version_total_max(self.load(), "evening"), 1.0)


# --- Bascule champs ⇄ LaTeX brut ------------------------------------------

class TestToRaw(unittest.TestCase):
    """« Passer au LaTeX brut » fige EXACTEMENT ce que les champs produisaient :
    le PDF, donc le calage, ne doit pas bouger. C'est le retour aux champs qui
    change la mise en page — d'où la confirmation d'un seul côté dans l'UI."""

    def test_le_brut_est_le_rendu_des_champs_sans_les_marqueurs(self):
        h = ss.HeaderBlock(establishment="ENSAI", date="8/9/2026",
                           subtitle="Matin", rules=True, instructions="Consignes.")
        raw = ss.header_to_raw(h)
        self.assertNotIn("%%H:", raw)
        # Ce qui compte : le LaTeX visible est le même des deux côtés.
        rendu = ss.render_header(h)
        for line in raw.split("\n"):
            if line.strip():
                self.assertIn(line, rendu)
        self.assertIn("Consignes.", raw)
        self.assertEqual(raw.count("\\hrule"), 2)

    def test_un_en_tete_deja_brut_est_rendu_tel_quel(self):
        """Le repasser en brut écraserait son contenu par celui, vide, des champs."""
        h = ss.HeaderBlock(raw_tex="EN-TÊTE IMPORTÉ")
        self.assertEqual(ss.header_to_raw(h), "EN-TÊTE IMPORTÉ")

    def test_le_brut_repasse_dans_le_rendu_a_l_identique(self):
        """`raw_tex` prime : réinjecté, il doit ressortir mot pour mot."""
        h = ss.HeaderBlock(establishment="ENSAI", title="Examen",
                           instructions="Consignes.")
        raw = ss.header_to_raw(h)
        again = ss.render_header(ss.HeaderBlock(raw_tex=raw))
        self.assertIn(raw, again)

    def test_feuille_de_reponses_sans_marqueurs(self):
        a = ss.AnswerSheetConfig(id_grid_digits=4, name_field=True, columns=2)
        tex = ss.answer_sheet_to_raw(a)
        self.assertNotIn("%%A:", tex)
        self.assertIn("\\AMCdebutFormulaire", tex)
        self.assertIn("\\AMCcodeGridInt{etu}{4}", tex)
        self.assertIn("\\champnom", tex)

    def test_la_largeur_de_grille_suit_les_champs(self):
        tex = ss.answer_sheet_to_raw(ss.AnswerSheetConfig(id_grid_digits=7))
        self.assertIn("\\AMCcodeGridInt{etu}{7}", tex)

    def test_sans_champ_nom_pas_de_champnom(self):
        tex = ss.answer_sheet_to_raw(ss.AnswerSheetConfig(name_field=False))
        self.assertNotIn("\\champnom", tex)

    def test_le_brut_de_la_feuille_est_repris_verbatim(self):
        """Figé puis relu, il doit produire la même feuille — sinon « passer au
        brut » déplacerait les cases, donc invaliderait le calage."""
        a = ss.AnswerSheetConfig()
        tex = ss.answer_sheet_to_raw(a)
        again = ss.render_answer_sheet(a, custom_tex=tex)
        self.assertEqual(again, tex)


class TestSwitchKeepsRendering(unittest.TestCase):
    """⚠ La promesse affichée dans l'UI — « le PDF est inchangé » — est
    vérifiable : le tex rendu doit être le même, aux commentaires près.

    Elle a été prise en défaut à la première version : `render_subject` émet le
    `\\newpage` À CÔTÉ de la feuille canonique et cesse de l'émettre dès que
    `answer_sheet_tex` est rempli. Figer la feuille supprimait donc un saut de
    page — et le `.xy` compilé changeait, donc toutes les positions de cases.
    """

    def _subject(self):
        return {"config": ss.SubjectConfig(
                    num_copies=3,
                    header=ss.HeaderBlock(establishment="ENSAI", title="Examen",
                                          date="8/9/2026", rules=True,
                                          instructions="Aucun document."),
                    answer_sheet=ss.AnswerSheetConfig()),
                "blocks": [qcm("q1", "t1")], "mode": "canonical"}

    @staticmethod
    def _visible(tex):
        """Le tex sans ses lignes de marqueurs : ce que LaTeX voit vraiment."""
        return [l for l in tex.split("\n") if not l.lstrip().startswith("%%")]

    def test_figer_len_tete_et_la_feuille_ne_change_pas_le_rendu(self):
        sub = self._subject()
        before = ss.render_subject(sub)
        cfg = sub["config"]
        cfg.header = ss.HeaderBlock(raw_tex=ss.header_to_raw(cfg.header))
        cfg.answer_sheet_tex = ss.answer_sheet_to_raw(cfg.answer_sheet,
                                                      num_copies=cfg.num_copies)
        after = ss.render_subject(sub)
        self.assertEqual(self._visible(before), self._visible(after))

    def test_le_saut_de_page_fait_partie_du_texte_fige(self):
        tex = ss.answer_sheet_to_raw(ss.AnswerSheetConfig())
        self.assertTrue(tex.startswith("\\newpage"))
        # …et il n'est pas doublé par le rendu.
        sub = self._subject()
        sub["config"].answer_sheet_tex = tex
        self.assertEqual(ss.render_subject(sub).count("\\newpage"), 1)
