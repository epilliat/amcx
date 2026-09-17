"""Envoi des notes par courriel — ce qui protège d'un envoi de masse raté.

Un message part chez 40 personnes et ne se rappelle pas. Ces tests fixent les
garde-fous : rien ne part sans `--send`, rien n'est filtré en silence, une
adresse déjà servie n'est pas re-servie, et le barème annoncé est celui de
l'échelle réelle — pas le plafond dur, qui vaut souvent 20 alors que les notes
sont sur 5.

    .venv/bin/python -m unittest discover -s tests -v
"""

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "auto_grading"))

import mail_results as mr             # noqa: E402


HEADER = ["batch", "page", "id_canonique", "nom_prenom", "courriel",
          "QCM_brut_sur_32", "note_finale", "validee"]
ROWS = [
    ["b", "1", "3017", "BADETS Robin", "r.b@x.fr", "5.0", "5.0", "non"],
    ["b", "2", "3058", "BARBEAU Faustine", "f.b@x.fr", "1.33", "1.33", "non"],
    ["b", "3", "", "?", "", "2.0", "2.0", "non"],              # non reliée
    ["b", "4", "3000", "ZARPAS Alexandre", "a.z@x.fr", "", "", "non"],  # pas de note
    ["b", "5", "2964", "PETITJEAN Enguerrand", "p.e@x.fr", "-1.5", "-1.5", "non"],
]


class MailCase(unittest.TestCase):
    """⚠ Chaque cas se donne un projet JETABLE. Sans ça, `load_recipients`
    construisait un `StudentMatcher` sur le projet actif de la machine : le
    test passait seul (le prénom venait de la vraie liste) et échouait dans la
    suite complète, selon l'ordre d'import. Le même piège que `AMCX_BANK_DIR`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old = os.environ.get("AMCX_PROJECT_DIR")
        os.environ["AMCX_PROJECT_DIR"] = str(self.dir)
        self.csv = self.dir / "notes.csv"
        with open(self.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(ROWS)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AMCX_PROJECT_DIR", None)
        else:
            os.environ["AMCX_PROJECT_DIR"] = self._old
        self._tmp.cleanup()

    def with_roster(self, *students):
        """Remplace le roster le temps d'un test (le prénom en vient)."""
        import student_list
        real = mr.StudentMatcher

        class Stub:
            def __init__(self):
                self.students = list(students)

        mr.StudentMatcher = Stub
        self.addCleanup(lambda: setattr(mr, "StudentMatcher", real))
        return student_list.Student


class TestDestinataires(MailCase):
    def test_le_prenom_vient_de_la_liste_etudiants(self):
        """Le csv ne porte que « NOM Prénom » collés ; découper la chaîne serait
        faux dès qu'un nom de famille est composé (« ADJEBA MBA Christian »)."""
        Student = self.with_roster()
        self.with_roster(Student(id="3017", nom="BADETS", prenom="Robin"))
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertEqual(d["recipients"][0]["first_name"], "Robin")

    def test_sans_liste_on_retombe_sur_le_nom_complet(self):
        """⚠ Jamais « Dear , » : un étudiant y lirait un envoi bâclé."""
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertEqual(d["recipients"][0]["first_name"], "BADETS Robin")

    def test_les_ecartes_sont_listes_jamais_avales(self):
        """⚠ Sur un envoi de masse, une ligne filtrée en silence est un
        étudiant qui ne recevra rien sans que personne ne le sache."""
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertEqual([r["full_name"] for r in d["recipients"]],
                         ["BADETS Robin", "BARBEAU Faustine",
                          "PETITJEAN Enguerrand"])
        self.assertEqual([w for _, w in d["skipped"]],
                         ["aucune adresse", "aucune note dans « note_finale »"])

    def test_une_note_negative_est_ramenee_a_zero(self):
        """Comme le script d'origine : AMCx autorise les notes négatives, les
        annoncer telles quelles dans une boîte mail n'apporte rien."""
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertEqual(d["recipients"][-1]["score"], 0.0)
        d = mr.load_recipients(self.csv, "note_finale", floor_at_zero=False)
        self.assertEqual(d["recipients"][-1]["score"], -1.5)

    def test_un_csv_sans_colonne_courriel_est_refuse_avec_le_remede(self):
        p = self.dir / "vieux.csv"
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([c for c in HEADER if c != "courriel"])
            w.writerow(["b", "1", "3017", "BADETS Robin", "5.0", "5.0", "non"])
        with self.assertRaises(mr.MailError) as cm:
            mr.load_recipients(p, "note_finale")
        self.assertIn("courriel", str(cm.exception))

    def test_une_colonne_de_note_inconnue_dit_lesquelles_existent(self):
        with self.assertRaises(mr.MailError) as cm:
            mr.load_recipients(self.csv, "note_sur_12")
        self.assertIn("note_finale", str(cm.exception))

    def test_un_csv_absent_renvoie_vers_le_bouton_qui_le_produit(self):
        with self.assertRaises(mr.MailError) as cm:
            mr.load_recipients(self.dir / "nulle-part.csv", "note_finale")
        self.assertIn("compte rendu", str(cm.exception))


class TestRendu(MailCase):
    def rec(self, **kw):
        base = {"email": "r.b@x.fr", "full_name": "BADETS Robin",
                "first_name": "Robin", "score": 4.33}
        return {**base, **kw}

    def test_le_message_porte_le_prenom_et_le_score(self):
        body = mr.render(mr.DEFAULT_TEMPLATE.read_text(encoding="utf-8"),
                         self.rec(), date="3 September 2025", max_score=5,
                         sender_name="Emmanuel Pilliat")
        self.assertIn("Dear Robin,", body)
        self.assertIn("Score: 4.33 / 5", body)
        self.assertIn("3 September 2025", body)

    def test_un_score_entier_ne_traine_pas_de_zeros(self):
        """« 5.00 » dans un courriel fait tableur, pas note."""
        body = mr.render("$score/$max_score", self.rec(score=5.0),
                         date="", max_score=5, sender_name="")
        self.assertEqual(body, "5/5")

    def test_l_entete_du_message_est_complet(self):
        msg = mr.build_message(self.rec(), "corps", subject="MCQ results",
                               sender="prof@x.fr", sender_name="E. Pilliat")
        self.assertEqual(msg["To"], "r.b@x.fr")
        self.assertEqual(msg["Subject"], "MCQ results")
        self.assertIn("prof@x.fr", msg["From"])
        self.assertTrue(msg["Date"])
        self.assertEqual(msg.get_content().strip(), "corps")


class TestEchelleAnnoncee(unittest.TestCase):
    """⚠ `final_threshold` est un PLAFOND, pas l'échelle. Le confondre annonce
    « 5 / 20 » à un étudiant qui a tout juste."""

    def test_l_echelle_vient_des_colonnes_pas_du_plafond(self):
        cfg = {"qcm_max": 5.0, "qcm_agg_weight": 1.0, "final_threshold": 20.0}
        self.assertEqual(mr.default_max_score(cfg), 5.0)

    def test_le_plafond_s_applique_quand_il_mord(self):
        cfg = {"qcm_max": 30.0, "qcm_agg_weight": 1.0, "final_threshold": 20.0}
        self.assertEqual(mr.default_max_score(cfg), 20.0)

    def test_moyenne_ponderee_de_plusieurs_colonnes(self):
        cfg = {"qcm_max": 10.0, "qcm_agg_weight": 1.0, "final_threshold": 20.0,
               "grade_files": [{"grade_cols": [{"max": 20.0, "agg_weight": 3.0}]}]}
        self.assertEqual(mr.default_max_score(cfg), 17.5)   # (10 + 3×20) / 4


class TestJournal(MailCase):
    """⚠ Relancer la commande après trois échecs ne doit pas re-notifier toute
    la promo."""

    def test_les_adresses_deja_servies_sont_memorisees(self):
        log = self.dir / "mail_log.csv"
        self.assertEqual(mr.already_sent(log), set())
        rec = {"email": "r.b@x.fr", "full_name": "BADETS Robin", "score": 5}
        mr.log_send(log, rec, "ok")
        mr.log_send(log, {**rec, "email": "f.b@x.fr"}, "echec", "boîte pleine")
        self.assertEqual(mr.already_sent(log), {"r.b@x.fr"})

    def test_un_echec_reste_a_renvoyer(self):
        log = self.dir / "mail_log.csv"
        mr.log_send(log, {"email": "x@x.fr", "full_name": "X", "score": 1},
                    "echec", "refusé")
        self.assertNotIn("x@x.fr", mr.already_sent(log))


class TestCliNeEnvoieRien(MailCase):
    """Le garde-fou principal : sans `--send`, la commande ne se connecte à
    aucun serveur — elle imprime et s'arrête."""

    def test_la_simulation_est_le_defaut(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mr.main(["--notes", str(self.csv), "--out-of", "5",
                          "--date", "3 September 2025"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("RIEN n'a été envoyé", out)
        self.assertIn("Dear BADETS Robin,", out)   # l'aperçu est bien rendu
        self.assertIn("aucune adresse", out)       # les écartés sont dits

    def test_une_colonne_sans_echelle_connue_exige_out_of(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mr.main(["--notes", str(self.csv),
                          "--score-col", "QCM_brut_sur_32"])
        self.assertEqual(rc, 1)
        self.assertIn("--out-of est requis", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestGabaritDollar(MailCase):
    """`$name` plutôt que `{name}` : un courriel contient des accolades bien
    plus souvent qu'un `$`, et il faudrait les doubler."""

    REC = {"email": "r.b@x.fr", "full_name": "BADETS Robin",
           "first_name": "Robin", "id": "3017", "score": 4.33}

    def r(self, tpl):
        return mr.render(tpl, self.REC, date="3 September 2025", max_score=5,
                         sender_name="E. Pilliat")

    def test_les_champs_sont_substitues(self):
        self.assertEqual(self.r("$name / $score / $max_score"), "Robin / 4.33 / 5")
        self.assertEqual(self.r("$full_name <$email> #$id"),
                         "BADETS Robin <r.b@x.fr> #3017")
        self.assertEqual(self.r("$date — $sender"), "3 September 2025 — E. Pilliat")

    def test_les_accolades_passent_telles_quelles(self):
        """Ce que `str.format` aurait fait échouer."""
        self.assertEqual(self.r("Voir {chapitre 3} et {4}"), "Voir {chapitre 3} et {4}")

    def test_un_champ_inconnu_dit_lesquels_existent(self):
        with self.assertRaises(mr.MailError) as cm:
            self.r("Dear $prenom,")
        self.assertIn("$prenom", str(cm.exception))
        self.assertIn("$name", str(cm.exception))

    def test_un_dollar_isole_explique_comment_l_ecrire(self):
        """⚠ « 5$ » ne doit pas faire échouer un envoi avec un message obscur."""
        with self.assertRaises(mr.MailError) as cm:
            self.r("Prix : 5$ ")
        self.assertIn("$$", str(cm.exception))
        self.assertEqual(self.r("Prix : 5$$"), "Prix : 5$")

    def test_un_objet_sans_date_ne_traine_pas_de_tiret(self):
        """« MCQ results — » partirait tel quel dans 38 boîtes."""
        self.assertEqual(mr.default_subject(""), "MCQ results")
        self.assertEqual(mr.default_subject("  "), "MCQ results")
        self.assertEqual(mr.default_subject("3 September 2025"),
                         "MCQ results — 3 September 2025")


class TestSecret(MailCase):
    """Le mot de passe : hors du projet, 0600, jamais rendu."""

    def setUp(self):
        super().setUp()
        self._saved = mr.PASSWORD_FILE
        mr.PASSWORD_FILE = self.dir / "smtp_password"
        self.addCleanup(lambda: setattr(mr, "PASSWORD_FILE", self._saved))
        self._env = os.environ.pop("AMCX_SMTP_PASSWORD", None)
        if self._env is not None:
            self.addCleanup(os.environ.__setitem__, "AMCX_SMTP_PASSWORD", self._env)

    def test_ecriture_lecture_effacement(self):
        self.assertFalse(mr.has_password())
        mr.save_password("abcd efgh ijkl mnop")
        self.assertTrue(mr.has_password())
        self.assertEqual(mr.smtp_password(interactive=False), "abcd efgh ijkl mnop")
        mr.save_password("")
        self.assertFalse(mr.has_password())
        self.assertFalse(mr.PASSWORD_FILE.exists())

    def test_le_fichier_est_en_0600(self):
        """⚠ Créé en 0600 AVANT l'écriture : poser les droits après laisserait
        une fenêtre où le secret est lisible par tout le monde."""
        import stat as _stat
        mr.save_password("secret")
        mode = _stat.S_IMODE(mr.PASSWORD_FILE.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_l_environnement_prime_sur_le_fichier(self):
        mr.save_password("du-fichier")
        os.environ["AMCX_SMTP_PASSWORD"] = "de-l-env"
        self.addCleanup(os.environ.pop, "AMCX_SMTP_PASSWORD", None)
        self.assertEqual(mr.smtp_password(interactive=False), "de-l-env")

    def test_sans_secret_le_message_dit_ou_le_poser(self):
        with self.assertRaises(mr.MailError) as cm:
            mr.smtp_password(interactive=False)
        self.assertIn("Courriels", str(cm.exception))

    def test_le_secret_vit_hors_du_projet(self):
        """Un dossier de projet se partage — il emporterait le mot de passe."""
        import project_state
        self.assertEqual(self._saved.parent, project_state.STATE_DIR)
        self.assertNotIn(str(self.dir), str(self._saved))


class TestGabaritDuProjet(MailCase):
    def setUp(self):
        super().setUp()
        self._saved = mr.PROJECT_TEMPLATE
        mr.PROJECT_TEMPLATE = self.dir / "mail_template.txt"
        self.addCleanup(lambda: setattr(mr, "PROJECT_TEMPLATE", self._saved))

    def test_le_premier_acces_installe_le_gabarit_fourni(self):
        self.assertFalse(mr.PROJECT_TEMPLATE.exists())
        text = mr.load_template()
        self.assertIn("$name", text)
        self.assertTrue(mr.PROJECT_TEMPLATE.exists())

    def test_les_editions_survivent(self):
        mr.save_template("Hi $name, you got $score.")
        self.assertEqual(mr.load_template(), "Hi $name, you got $score.")

    def test_un_projet_en_lecture_seule_reste_consultable(self):
        """Un projet reçu sur une clé USB ne doit pas casser l'onglet."""
        mr.PROJECT_TEMPLATE = Path("/proc/amcx-inexistant/mail_template.txt")
        self.assertIn("$name", mr.load_template())


class TestAbsents(MailCase):
    """Un absent est DANS le csv (marqué ABS) et HORS des courriels.

    Les deux moitiés comptent : sans la ligne, un étudiant sans copie disparaît
    du fichier remis à la scolarité et « absent » devient indiscernable de
    « oublié dans l'export » ; sans l'exclusion, il reçoit un message annonçant
    une note qu'il n'a pas.
    """

    def setUp(self):
        super().setUp()
        with open(self.csv, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(
                ["", "", "2991", "ZARG-LAYOUN Adam", "a.z2@x.fr", "ABS", "ABS", ""])

    def test_l_absent_n_est_pas_un_destinataire(self):
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertNotIn("a.z2@x.fr", [r["email"] for r in d["recipients"]])

    def test_il_est_ecarte_SOUS_SON_NOM_et_pour_la_bonne_raison(self):
        """« il était absent » et « l'export est cassé » ne se ressemblent pas."""
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertIn(("ZARG-LAYOUN Adam", "absent (ABS)"), d["skipped"])

    def test_ABS_n_est_jamais_lu_comme_zero(self):
        """⚠ Un absent n'a pas eu zéro : le confondre fausserait toute moyenne
        calculée en aval sur le fichier exporté."""
        self.assertIsNone(mr._num("ABS"))
        self.assertIsNone(mr._num("abs"))
        d = mr.load_recipients(self.csv, "note_finale")
        self.assertNotIn(0.0, [r["score"] for r in d["recipients"]
                               if r["full_name"].startswith("ZARG")])
