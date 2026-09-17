"""Sujet vierge et corrigé à publier (onglet Sujet → 📤 Publier).

Ces tests fixent quatre décisions, chacune venue d'un piège mesuré sur le
sujet réel à deux versions :

1. la borne gauche du bloc « feuille de réponses » est un PRÉFIXE de sa borne
   droite — sans garde, le découpage saute d'une version à l'autre et emporte
   les questions du milieu ;
2. le `\\newpage` qui précède la feuille part avec elle, sinon le document
   publié se termine par une page blanche ;
3. un sujet legacy n'a pas de marqueurs : on garde sa feuille de réponses et
   on le DIT, plutôt que de couper au jugé ;
4. le sujet vierge et le corrigé sortent du même tex à une ligne près, pour
   qu'ils se lisent côte à côte page pour page.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auto_grading"))


def _fresh_sujet_store():
    """Recharge sujet_store : SUJET_DIR est figé à l'import du module."""
    import config
    config._config_cache = None
    import sujet_store
    return importlib.reload(sujet_store)


class PublicationCase(unittest.TestCase):
    """⚠ `unittest discover` importe tous les modules avant d'en exécuter un :
    poser AMCX_PROJECT_DIR au niveau module le poserait pour tout le monde."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("AMCX_PROJECT_DIR")
        os.environ["AMCX_PROJECT_DIR"] = str(self.dir)
        (self.dir / "sujet").mkdir(parents=True, exist_ok=True)
        (self.dir / "config.json").write_text("{}", encoding="utf-8")
        self.S = _fresh_sujet_store()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AMCX_PROJECT_DIR", None)
        else:
            os.environ["AMCX_PROJECT_DIR"] = self._old
        self.tmp.cleanup()


def _two_version_tex():
    """Squelette du sujet réel : deux `\\exemplaire`, chacun avec sa feuille."""
    body = []
    body.append("\\documentclass{article}")
    body.append("\\usepackage[separateanswersheet]{automultiplechoice}")
    body.append("\\begin{document}")
    for v, q in (("morning", "QM"), ("afternoon", "QA")):
        body.append("\\exemplaire{22}{")
        body.append("%%QCM-HEADER")
        body.append("EN-TETE " + v)
        body.append("%%QCM-HEADER-END")
        body.append("\\restituegroupe{%s}" % v)
        body.append("QUESTION-%s" % q)
        body.append("\\newpage")
        body.append("%%QCM-ANSWER-SHEET")
        body.append("\\AMCdebutFormulaire")
        body.append("GRILLE-%s" % v)
        body.append("%%QCM-ANSWER-SHEET-END")
        body.append("}")
    body.append("\\end{document}")
    return "\n".join(body) + "\n"


class TestDecoupage(PublicationCase):
    def test_les_deux_feuilles_partent_les_questions_restent(self):
        """Le piège : `%%QCM-ANSWER-SHEET` est un préfixe de
        `%%QCM-ANSWER-SHEET-END`. Sans le garde, la 1re borne gauche
        s'accroche à la FERMETURE du 1er bloc et tout le second exemplaire
        — en-tête et questions compris — disparaît du document publié."""
        tex, notes = self.S.publication_tex(_two_version_tex(), "corrige")
        self.assertNotIn("GRILLE-morning", tex)
        self.assertNotIn("GRILLE-afternoon", tex)
        self.assertNotIn("AMCdebutFormulaire", tex)
        self.assertIn("QUESTION-QM", tex)
        self.assertIn("QUESTION-QA", tex)          # ← ce qui sautait
        self.assertIn("EN-TETE afternoon", tex)
        self.assertEqual(tex.count("\\exemplaire"), 2)
        self.assertTrue(any("2 version" in n for n in notes), notes)

    def test_le_saut_de_page_part_avec_la_feuille(self):
        """Le `\\newpage` qui l'introduisait laisserait une page blanche."""
        tex, _ = self.S.publication_tex(_two_version_tex(), "corrige")
        self.assertNotIn("\\newpage", tex)

    def test_sujet_legacy_on_garde_et_on_le_dit(self):
        """Pas de marqueurs : couper au jugé publierait un sujet tronqué."""
        legacy = ("\\documentclass{article}\n"
                  "\\usepackage{automultiplechoice}\n"
                  "\\begin{document}\n\\exemplaire{10}{\nTOUT\n}\n"
                  "\\end{document}\n")
        tex, notes = self.S.publication_tex(legacy, "corrige")
        self.assertIn("TOUT", tex)
        self.assertTrue(any("conservée" in n for n in notes), notes)


class TestVierge(PublicationCase):
    def test_le_vierge_eteint_les_reponses_et_le_bandeau(self):
        """Il n'existe aucun crochet AMC « comme le corrigé, mais vierge » :
        on repart de `\\CorrigeExterne` et on éteint dans le préambule."""
        tex, _ = self.S.publication_tex(_two_version_tex(), "sujet")
        i_inj = tex.index("\\AMC@correcfalse")
        self.assertLess(i_inj, tex.index("\\begin{document}"))
        self.assertIn("\\def\\AMC@intituleHead{}", tex)
        self.assertIn("\\makeatletter", tex)
        self.assertIn("\\makeatother", tex)

    def test_le_corrige_ne_touche_pas_au_preambule(self):
        """Les deux documents doivent se superposer page pour page : ils ne
        diffèrent que par ces trois lignes."""
        src = _two_version_tex()
        vierge, _ = self.S.publication_tex(src, "sujet")
        corrige, _ = self.S.publication_tex(src, "corrige")
        self.assertNotIn("AMC@correcfalse", corrige)
        self.assertEqual(vierge.replace(self.S._PUB_BLANK_PREAMBLE, ""), corrige)

    def test_type_inconnu_et_preambule_illisible(self):
        with self.assertRaises(ValueError):
            self.S.publication_tex(_two_version_tex(), "catalogue")
        with self.assertRaises(ValueError):
            self.S.publication_tex("\\documentclass{article}\n", "sujet")


@unittest.skipUnless(shutil.which("pdflatex"), "pdflatex absent")
class TestCompilation(PublicationCase):
    """Bout en bout sur un projet vierge : c'est la seule preuve que le
    document se compile sans sa feuille de réponses (le `\\AMCform` qui la
    porte n'est alors jamais appelé)."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "auto_grading"))
        import new_project
        importlib.reload(new_project)
        # `create_project` rend le sous-dossier `auto_grading/`, qui EST le
        # project_root() : le dossier parent porte aussi `projet/`.
        self.proj = new_project.create_project(Path(self.tmp.name) / "p")
        os.environ["AMCX_PROJECT_DIR"] = str(self.proj)
        self.S = _fresh_sujet_store()

    def test_les_deux_documents_sortent_et_se_superposent(self):
        import fitz
        pages = {}
        for kind in self.S.PUBLICATION_KINDS:
            r = self.S.compile_publication(kind)
            self.assertTrue(r["ok"], r["log"])
            p = self.S.PUBLICATION_PDF[kind]
            self.assertTrue(p.exists())
            with fitz.open(p) as d:
                pages[kind] = d.page_count
        self.assertEqual(pages["sujet"], pages["corrige"])
        self.assertGreater(pages["sujet"], 0)

    def test_ne_touche_ni_au_tex_ni_au_calage(self):
        """Publier ne doit rien changer à la correction des copies."""
        tex = self.S.EXAM_TEX
        before = (tex.read_bytes(), tex.stat().st_mtime_ns)
        self.assertTrue(self.S.compile_publication("corrige")["ok"])
        self.assertEqual((tex.read_bytes(), tex.stat().st_mtime_ns), before)
        self.assertFalse(self.S.SUJET_PDF.exists())
        self.assertFalse(self.S.EXAM_XY.exists())


if __name__ == "__main__":
    unittest.main()
