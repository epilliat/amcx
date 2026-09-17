"""Vue d'ensemble : plusieurs examens d'un même dossier.

Ce que fixent ces tests, et pourquoi :

- **la jointure des identités.** Le même étudiant ne porte pas le même
  identifiant d'un examen à l'autre — mesuré sur deux vrais examens, `3017`
  d'un côté et `13017` de l'autre, **36 étudiants sur 39** apparaissaient en
  double avec la moitié de leurs notes chacun ;
- **le plafond par colonne avant la moyenne** : « seuiller à 30 » sur un examen
  qui en vaut 31 donne 20/20, et cet excédent ne compense pas une autre note ;
- **rien n'entre dans la moyenne sans qu'on l'ait demandé** : un projet trouvé
  dans le dossier mais absent de `cohorte.json` est un candidat, pas un membre.

    .venv/bin/python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "auto_grading"))

import cohort   # noqa: E402


def member(path, label, bareme, students, ok=True, error=""):
    return {"path": path, "label": label, "ok": ok, "error": error,
            "bareme": bareme, "students": students, "summary": {}}


def st(sid, name, brut=None, absent=False, email=""):
    return {"batch": "b", "page": 1, "id": sid, "nom_prenom": name,
            "courriel": email, "id_lu": sid, "brut": brut, "note": brut,
            "validee": False, "flags": [], "absent": absent}


def cols(*specs):
    """`(path, label, seuil, max, poids)` → descripteurs de colonne d'examen."""
    return [{"key": f"exam::{p}", "kind": "exam", "path": p, "label": lab,
             "color": "#000", "seuil": s, "max": m, "auto_seuil": False,
             "auto_max": False, "agg_weight": w, "bareme": s, "ok": True,
             "error": ""} for (p, lab, s, m, w) in specs]


class TestIdentites(unittest.TestCase):

    def people(self, *pairs):
        return {i: {"id": i, "nom_prenom": n, "courriel": "", "absent_in": []}
                for i, n in pairs}

    def test_un_suffixe_rejoint_son_identifiant_complet(self):
        m, refused = cohort.identity_map(
            self.people(("3017", "BADETS Robin"), ("13017", "BADETS Robin")))
        self.assertEqual(m["3017"], "13017")
        self.assertEqual(m["13017"], "13017")
        self.assertEqual(refused, [])

    def test_un_suffixe_ambigu_ne_designe_personne(self):
        """⚠ Fondre deux étudiants est pire que d'en afficher un en double."""
        m, refused = cohort.identity_map(
            self.people(("017", "X"), ("13017", "A"), ("23017", "B")))
        self.assertEqual(m["017"], "017")
        self.assertEqual(len(refused), 1)
        self.assertIn("017", refused[0])

    def test_des_noms_differents_ne_sont_pas_fusionnes(self):
        m, refused = cohort.identity_map(
            self.people(("3017", "BADETS Robin"), ("13017", "MOREL Jean")))
        self.assertEqual(m["3017"], "3017")
        self.assertEqual(len(refused), 1)

    def test_un_nom_inconnu_n_empeche_pas_le_rapprochement(self):
        """Un examen sans liste d'étudiants rend « ? » : il ne doit pas
        bloquer un rapprochement que le numéro établit."""
        m, _ = cohort.identity_map(
            self.people(("3017", "?"), ("13017", "BADETS Robin")))
        self.assertEqual(m["3017"], "13017")

    def test_un_identifiant_non_numerique_est_laisse_tel_quel(self):
        m, refused = cohort.identity_map(
            self.people(("E3017", "X"), ("13017", "X")))
        self.assertEqual(m["E3017"], "E3017")
        self.assertEqual(refused, [])


class TestTable(unittest.TestCase):

    def test_le_plafond_par_colonne_s_applique_avant_la_moyenne(self):
        """31 points seuillés à 30, ramenés sur 20 → 20/20, et l'excédent ne
        compense pas la seconde note : 17,0 et non 17,33."""
        c = cols(("qcm", "QCM", 30.0, 20.0, 1.0),
                 ("proj", "Projet", 20.0, 20.0, 1.0))
        rows, _ = cohort.build_table(
            [member("qcm", "QCM", 31.0, [st("1", "ADAM Ève", 31.0)]),
             member("proj", "Projet", 20.0, [st("1", "ADAM Ève", 14.0)])],
            c, [], 20.0)
        r = rows[0]
        self.assertEqual(r["notes"]["exam::qcm"], 20.0)
        self.assertEqual(r["final"], 17.0)

    def test_une_colonne_absente_ne_compte_pas_dans_la_moyenne(self):
        c = cols(("a", "A", 20.0, 20.0, 1.0), ("b", "B", 20.0, 20.0, 1.0))
        rows, _ = cohort.build_table(
            [member("a", "A", 20.0, [st("1", "ADAM Ève", 12.0)]),
             member("b", "B", 20.0, [])],
            c, [], 20.0)
        self.assertEqual(rows[0]["final"], 12.0)
        self.assertEqual(rows[0]["n_columns"], 1)

    def test_un_absent_est_nomme_pas_compte_zero(self):
        c = cols(("a", "A", 20.0, 20.0, 1.0), ("b", "B", 20.0, 20.0, 1.0))
        rows, _ = cohort.build_table(
            [member("a", "A", 20.0, [st("1", "ADAM Ève", 12.0)]),
             member("b", "B", 20.0, [st("1", "ADAM Ève", None, absent=True)])],
            c, [], 20.0)
        self.assertEqual(rows[0]["absent_in"], ["B"])
        self.assertEqual(rows[0]["final"], 12.0)

    def test_les_notes_d_un_meme_etudiant_se_rejoignent(self):
        """Sans le repliement des identités, il a deux lignes et deux notes
        finales, fausses toutes les deux."""
        c = cols(("a", "A", 20.0, 20.0, 1.0), ("b", "B", 20.0, 20.0, 1.0))
        rows, _ = cohort.build_table(
            [member("a", "A", 20.0, [st("3017", "BADETS Robin", 10.0)]),
             member("b", "B", 20.0, [st("13017", "BADETS Robin", 16.0)])],
            c, [], 20.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "13017")
        self.assertEqual(rows[0]["also_id"], ["3017"])
        self.assertEqual(rows[0]["final"], 13.0)

    def test_une_copie_non_reliee_est_comptee_a_part(self):
        """Elle ne peut être recollée à rien d'un examen à l'autre : la fondre
        dans une ligne au hasard donnerait la note d'un autre."""
        c = cols(("a", "A", 20.0, 20.0, 1.0))
        rows, _ = cohort.build_table(
            [member("a", "A", 20.0, [st("", "?", 9.0)])], c, [], 20.0)
        self.assertEqual(rows[-1]["unlinked"], 1)
        self.assertIsNone(rows[-1]["final"])

    def test_un_examen_illisible_n_efface_pas_les_autres(self):
        c = cols(("a", "A", 20.0, 20.0, 1.0), ("b", "B", 20.0, 20.0, 1.0))
        c[1]["ok"] = False
        c[1]["error"] = "pas un projet AMCx"
        rows, _ = cohort.build_table(
            [member("a", "A", 20.0, [st("1", "ADAM Ève", 12.0)]),
             member("b", "B", 0.0, [], ok=False, error="pas un projet AMCx")],
            c, [], 20.0)
        self.assertEqual(rows[0]["final"], 12.0)
        self.assertIn("pas un projet AMCx", " ".join(cohort.scale_warnings(c)))


class TestEchelles(unittest.TestCase):

    def test_deux_echelles_differentes_sont_signalees(self):
        """Un QCM sur 33 et un projet sur 20, à poids égal, donnent un
        « /26,5 » que personne n'a demandé."""
        c = cols(("a", "QCM", 33.0, 33.0, 1.0), ("b", "Projet", 20.0, 20.0, 1.0))
        w = cohort.scale_warnings(c)
        self.assertEqual(len(w), 1)
        self.assertIn("échelle", w[0])

    def test_une_echelle_commune_ne_dit_rien(self):
        c = cols(("a", "QCM", 33.0, 20.0, 1.0), ("b", "Projet", 20.0, 20.0, 1.0))
        self.assertEqual(cohort.scale_warnings(c), [])


class TestDossier(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _project(self, name):
        (self.d / name / "sujet").mkdir(parents=True)
        (self.d / name / "sujet" / "exam.tex").write_text("%", encoding="utf-8")

    def test_un_projet_non_liste_est_candidat_pas_membre(self):
        """⚠ Un dossier d'essai n'entre pas tout seul dans la moyenne."""
        self._project("QCM1")
        cfg = cohort.load(self.d)
        self.assertEqual(cfg["exams"], [])
        self.assertEqual([c["label"] for c in cohort.candidates(self.d, cfg)],
                         ["QCM1"])

    def test_un_projet_liste_n_est_plus_candidat(self):
        self._project("QCM1")
        cfg = dict(cohort.DEFAULTS, exams=[{"path": "QCM1"}])
        self.assertEqual(cohort.candidates(self.d, cfg), [])

    def test_un_fichier_illisible_leve_au_lieu_de_repartir_de_zero(self):
        """Repartir des défauts effacerait la composition de l'ensemble et les
        réglages de note à la première écriture."""
        cohort.cohort_file(self.d).write_text("{pas du json", encoding="utf-8")
        with self.assertRaises(cohort.CohortError):
            cohort.load(self.d)

    def test_enregistrer_ne_garde_que_les_cles_connues(self):
        cohort.save(self.d, {"name": "L3", "exams": [{"path": "a"}],
                             "inconnue": 1})
        out = json.loads(cohort.cohort_file(self.d).read_text(encoding="utf-8"))
        self.assertNotIn("inconnue", out)
        self.assertEqual(out["name"], "L3")
        self.assertEqual(out["final_threshold"], 20.0)


if __name__ == "__main__":
    unittest.main()


class TestEnsembleVide(unittest.TestCase):
    """⚠ Un ensemble qu'on vient de créer n'a AUCUNE colonne — et c'est le
    premier écran qu'on voit après « créer ». Les bornes de curseurs levaient
    sur la liste vide (`max()` d'une séquence vide) : la page répondait 400.
    """

    def test_les_bornes_tiennent_sans_colonne(self):
        import grades_view as gv
        r = gv.slider_ranges(dict(cohort.DEFAULTS), [], 20.0)
        self.assertGreater(r["weight"]["max"], 0)
        self.assertEqual(r["cols"], {})

    def test_la_table_d_un_ensemble_vide_est_vide(self):
        rows, refused = cohort.build_table([], [], [], 20.0)
        self.assertEqual((rows, refused), ([], []))
        self.assertEqual(cohort.summary([], [])["n_students"], 0)
