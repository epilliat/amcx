"""Envoi de leur note aux étudiants, par courriel — portage du script Julia.

Lit `compte_rendu/notes.csv` (bouton « Sauvegarder le compte rendu » du
tableau de bord), recolle le prénom depuis la liste étudiants, et envoie à
chacun un message sobre avec son score.

    # 1. voir ce qui partirait, sans rien envoyer (comportement PAR DÉFAUT)
    python auto_grading/mail_results.py --date "3 September 2025"

    # 2. un essai sur sa propre adresse
    python auto_grading/mail_results.py --date "…" --only moi@ensai.fr --send

    # 3. l'envoi réel
    python auto_grading/mail_results.py --date "…" --send

⚠ **Rien ne part sans `--send`.** Un envoi à toute une promo est irréversible
et sort du poste : le mode par défaut imprime les destinataires, le score de
chacun et le message rendu, et s'arrête là. C'est le seul garde-fou qui vaille
pour une action qu'on ne peut pas rappeler.

⚠ **Le mot de passe n'est écrit NULLE PART** — ni ici, ni dans `config.json`
(qui suit le projet quand on le partage, et que `public_config()` ne masquerait
pas). Il est lu dans `AMCX_SMTP_PASSWORD`, sinon demandé au clavier. Pour
Gmail c'est un « mot de passe d'application » (compte Google → Sécurité →
Mots de passe des applications), jamais le mot de passe du compte.
"""

from __future__ import annotations

import argparse
import csv
import os
import smtplib
import json
import ssl
import stat
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from getpass import getpass
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent          # installation : pour les imports
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import project_state  # noqa: E402
from student_list import StudentMatcher  # noqa: E402

_DATA = config.project_root()                    # projet actif : pour les données
NOTES_CSV = _DATA / "compte_rendu" / "notes.csv"
SENT_LOG = _DATA / "compte_rendu" / "mail_log.csv"
DEFAULT_TEMPLATE = ROOT / "mail_results.txt"     # gabarit fourni (installation)
PROJECT_TEMPLATE = _DATA / "mail_template.txt"   # gabarit du projet, éditable
# ⚠ Le secret vit HORS du projet : un dossier de projet se partage (sujet,
# scans, config) et emporterait le mot de passe avec lui.
PASSWORD_FILE = project_state.STATE_DIR / "smtp_password"

# Champs offerts au gabarit. `string.Template` (`$name`) plutôt que `str.format`
# (`{name}`) : un texte de courriel contient des accolades bien plus souvent
# qu'un `$`, et il faudrait les doubler.
FIELDS = ("name", "full_name", "email", "id", "score", "max_score", "date",
          "sender")

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465


# --------------------------------------------------------------------------
# Destinataires
# --------------------------------------------------------------------------
class MailError(Exception):
    """Problème que l'utilisateur doit voir avant tout envoi."""


# Marqueur posé par l'export pour un étudiant qui n'a pas composé. Il est dans
# le csv **exprès** — la scolarité doit voir la ligne — mais il n'y a rien à
# annoncer à son porteur. Déclaré une seule fois, dans `exam_results`.
from exam_results import ABSENT_MARK   # noqa: E402


def _num(raw: str) -> float | None:
    """Note lue depuis le csv ; None si vide ou illisible (ligne écartée)."""
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_recipients(csv_path: Path, score_col: str, floor_at_zero: bool = True) -> dict:
    """Destinataires prêts à l'envoi + tout ce qui a été écarté, et pourquoi.

    ⚠ Rien n'est filtré en silence : une copie sans adresse, sans note ou non
    reliée à un étudiant ressort dans `skipped`. Sur un envoi de masse, ce sont
    exactement les cas qu'il faut voir AVANT de partir — après, il est trop tard
    pour la copie qu'on a oubliée.
    """
    if not csv_path.exists():
        raise MailError(
            f"{csv_path} est absent — clique « 💾 Sauvegarder le compte rendu » "
            "sur le tableau de bord pour le produire.")
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise MailError(f"{csv_path} ne contient aucune ligne.")
    if score_col not in rows[0]:
        raise MailError(
            f"colonne « {score_col} » absente de {csv_path.name} "
            f"(colonnes : {', '.join(rows[0])})")
    if "courriel" not in rows[0]:
        raise MailError(
            f"{csv_path.name} n'a pas de colonne « courriel » — recharge la "
            "liste étudiants en désignant sa colonne d'adresses, puis "
            "ré-enregistre le compte rendu.")

    # Le csv ne porte que « NOM Prénom » collés : le prénom vient du roster,
    # où les deux champs sont distincts. Découper la chaîne serait faux dès
    # qu'un nom de famille est composé (« ADJEBA MBA Christian »).
    by_id = {s.id: s for s in StudentMatcher().students}

    out, skipped = [], []
    for r in rows:
        who = (r.get("nom_prenom") or "?").strip()
        email = (r.get("courriel") or "").strip()
        note = _num(r.get(score_col, ""))
        if not email:
            skipped.append((who, "aucune adresse"))
            continue
        if note is None:
            # ⚠ Un absent est écarté SOUS SON NOM, pas silencieusement : « il
            # n'a rien reçu parce qu'il était absent » et « il n'a rien reçu
            # parce que l'export est cassé » ne se ressemblent pas.
            raw = (r.get(score_col) or "").strip().upper()
            skipped.append((who, "absent (ABS)" if raw == ABSENT_MARK
                            else f"aucune note dans « {score_col} »"))
            continue
        if floor_at_zero:
            note = max(note, 0.0)
        st = by_id.get((r.get("id_canonique") or "").strip())
        out.append({
            "email": email,
            "id": (r.get("id_canonique") or "").strip(),
            "full_name": who,
            # Repli sur le nom complet : mieux vaut « Dear BADETS Robin » qu'un
            # « Dear , » qui signalerait à l'étudiant que l'envoi est bâclé.
            "first_name": (st.prenom.strip() if st and st.prenom.strip() else who),
            "score": note,
        })
    return {"recipients": out, "skipped": skipped}


def default_max_score(cfg: dict) -> float:
    """Le plus haut score que `note_finale` puisse atteindre.

    ⚠ Depuis que l'évaluation d'un examen n'a plus de réglage, `note_finale`
    EST le score brut : son échelle est le **barème du sujet**, qu'on lit donc
    en premier. Mettre la note à une autre échelle appartient au niveau qui
    compare plusieurs examens, et ce niveau annoncera la sienne.

    ⚠ Ce n'est PAS `final_threshold` : celui-ci est un plafond dur, souvent
    laissé à 20 alors que les colonnes sont ramenées sur 5. Annoncer « 5 / 20 »
    à un étudiant qui a tout juste serait faux, et faux dans le sens qui
    inquiète. Le repli ci-dessous — moyenne pondérée des `max` — ne sert plus
    qu'aux projets dont le sujet n'est pas lisible.
    """
    try:
        import sujet_store
        bareme = sujet_store.subject_total_max()
        if bareme > 0:
            return bareme
    except Exception:                                   # noqa: BLE001
        pass
    num = float(cfg.get("qcm_max", 20.0)) * float(cfg.get("qcm_agg_weight", 1.0))
    den = float(cfg.get("qcm_agg_weight", 1.0))
    for fc in cfg.get("grade_files", []):
        for gc in fc.get("grade_cols", []):
            w = float(gc.get("agg_weight", 1.0))
            num += float(gc.get("max", 20.0)) * w
            den += w
    scale = num / den if den else float(cfg.get("qcm_max", 20.0))
    return min(scale, float(cfg.get("final_threshold", 20.0)))


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
def fields_for(rec: dict, *, date: str, max_score: float, sender: str,
               decimals: int = 2) -> dict:
    """Valeurs des `$champs` pour un destinataire."""
    return {
        "name": rec["first_name"],
        "full_name": rec["full_name"],
        "email": rec["email"],
        "id": rec.get("id", ""),
        # 4.33 → « 4.33 » ; 5.0 → « 5 ». Un « 5.00 » dans un courriel fait
        # tableur, pas note.
        "score": f"{rec['score']:.{decimals}f}".rstrip("0").rstrip("."),
        "max_score": f"{max_score:g}",
        "date": date,
        "sender": sender,
    }


def render(template: str, rec: dict, *, date: str, max_score: float,
           sender_name: str, decimals: int = 2) -> str:
    """Corps du message pour un destinataire.

    ⚠ Le gabarit est édité par l'utilisateur : un `$champ` inconnu doit dire
    lequel et lister les champs valides, pas lever un `KeyError` nu au milieu
    d'un envoi. Un `$` isolé (« 5$ ») ne doit pas non plus faire échouer
    l'envoi — d'où le message dédié.
    """
    f = fields_for(rec, date=date, max_score=max_score, sender=sender_name,
                   decimals=decimals)
    try:
        return Template(template).substitute(f)
    except KeyError as e:
        raise MailError(
            f"le gabarit utilise un champ inconnu : ${e.args[0]}. "
            f"Disponibles : {', '.join('$' + k for k in FIELDS)}") from e
    except ValueError as e:
        raise MailError(
            f"gabarit invalide ({e}) — pour écrire un « $ » littéral, "
            "doublez-le : « $$ ».") from e


def default_subject(date: str) -> str:
    """Objet par défaut. ⚠ Sans date, pas de tiret cadratin orphelin — « MCQ
    results — » part tel quel dans 38 boîtes si on oublie de la renseigner."""
    date = (date or "").strip()
    return f"MCQ results — {date}" if date else "MCQ results"


def build_message(rec: dict, body: str, *, subject: str, sender: str,
                  sender_name: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = rec["email"]
    msg["Date"] = datetime.now(timezone.utc).astimezone().strftime(
        "%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(body)
    return msg


# --------------------------------------------------------------------------
# Journal des envois
# --------------------------------------------------------------------------
def already_sent(log_path: Path) -> set[str]:
    """Adresses déjà servies. ⚠ Un second passage ne doit pas re-notifier
    toute la promo parce qu'on relance la commande après trois échecs."""
    if not log_path.exists():
        return set()
    with open(log_path, encoding="utf-8", newline="") as f:
        return {r["email"] for r in csv.DictReader(f) if r.get("status") == "ok"}


def log_send(log_path: Path, rec: dict, status: str, detail: str = "") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["horodatage", "email", "nom", "score", "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), rec["email"],
                    rec["full_name"], rec["score"], status, detail])


# --------------------------------------------------------------------------
# Envoi
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Le secret — hors du projet, jamais rendu à qui le demande
# --------------------------------------------------------------------------
# ⚠ Ce fichier contient le mot de passe EN CLAIR, protégé par les seules
# permissions du système (0600). Le chiffrer sans un secret d'utilisateur à
# saisir à chaque envoi ne serait que de l'obscurcissement : la clé vivrait à
# côté. L'interface le dit, plutôt que de laisser croire à un coffre-fort.
#
# Ce qui est réellement garanti :
#   - il est HORS du dossier de projet (qui se partage, sujet et scans compris) ;
#   - aucune route ne le renvoie — le front ne voit que « configuré : oui/non » ;
#   - `save_password("")` l'efface.
def has_password() -> bool:
    return PASSWORD_FILE.exists() and bool(PASSWORD_FILE.read_text("utf-8").strip())


def save_password(password: str) -> None:
    """Écrit le secret en 0600, ou l'efface si `password` est vide."""
    pw = (password or "").strip()
    if not pw:
        PASSWORD_FILE.unlink(missing_ok=True)
        return
    project_state.ensure_state_dir()
    # Créé en 0600 AVANT d'écrire : poser les droits après laisserait une
    # fenêtre où le fichier est lisible par tout le monde.
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pw)
    os.chmod(PASSWORD_FILE, stat.S_IRUSR | stat.S_IWUSR)


def smtp_password(interactive: bool = True) -> str:
    """Précédence : environnement > fichier local > saisie au clavier."""
    pw = os.environ.get("AMCX_SMTP_PASSWORD", "")
    if pw:
        return pw
    if has_password():
        return PASSWORD_FILE.read_text("utf-8").strip()
    if not interactive or not sys.stdin.isatty():
        raise MailError(
            "aucun mot de passe enregistré : pose-le dans l'onglet Courriels, "
            "ou dans AMCX_SMTP_PASSWORD (mot de passe d'application, pas celui "
            "du compte).")
    return getpass("Mot de passe d'application SMTP : ")


# --------------------------------------------------------------------------
# Gabarit du projet
# --------------------------------------------------------------------------
def load_template() -> str:
    """Gabarit du projet, initialisé depuis celui fourni s'il n'existe pas.

    ⚠ Lecture seule si le projet n'est pas inscriptible : on rend le gabarit
    fourni plutôt que d'échouer — un projet reçu sur une clé USB doit rester
    consultable.
    """
    if PROJECT_TEMPLATE.exists():
        return PROJECT_TEMPLATE.read_text(encoding="utf-8")
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    try:
        PROJECT_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
        PROJECT_TEMPLATE.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def save_template(text: str) -> None:
    PROJECT_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_TEMPLATE.write_text(text, encoding="utf-8")


def send_all(recipients: list[dict], *, host: str, port: int, user: str,
             password: str, sender: str, sender_name: str, subject: str,
             template: str, date: str, max_score: float, delay: float,
             log_path: Path) -> tuple[int, list[tuple[str, str]]]:
    """Envoie un message par destinataire. Rend (n_envoyés, échecs).

    ⚠ Un échec n'interrompt pas la boucle : sur 40 destinataires, s'arrêter à la
    première adresse morte laisserait 39 personnes sans leur note. Chaque envoi
    est journalisé au fil de l'eau — une interruption ne perd pas la trace de ce
    qui est déjà parti.
    """
    failures: list[tuple[str, str]] = []
    sent = 0
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx) as smtp:
        smtp.login(user, password)
        for rec in recipients:
            body = render(template, rec, date=date, max_score=max_score,
                          sender_name=sender_name)
            msg = build_message(rec, body,
                                subject=render(subject, rec, date=date,
                                               max_score=max_score,
                                               sender_name=sender_name),
                                sender=sender, sender_name=sender_name)
            try:
                smtp.send_message(msg)
            except Exception as e:                       # noqa: BLE001
                failures.append((rec["email"], str(e)))
                log_send(log_path, rec, "echec", str(e)[:200])
                print(f"  ✘ {rec['full_name']} <{rec['email']}> : {e}")
            else:
                sent += 1
                log_send(log_path, rec, "ok")
                print(f"  ✓ {rec['full_name']} <{rec['email']}> — "
                      f"{rec['score']:g}/{max_score:g}")
            if delay:
                time.sleep(delay)
    return sent, failures


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    cfg = config.load_config()
    ap = argparse.ArgumentParser(
        description="Envoie à chaque étudiant sa note par courriel.",
        epilog="Sans --send, rien ne part : la commande montre ce qu'elle ferait.")
    ap.add_argument("--send", action="store_true",
                    help="envoie réellement (sinon : simulation)")
    ap.add_argument("--date", default="",
                    help="date de l'épreuve, telle qu'elle apparaîtra dans le message")
    ap.add_argument("--subject", default="",
                    help="objet (défaut : « MCQ results — <date> »)")
    ap.add_argument("--from", dest="sender", default=os.environ.get("AMCX_SMTP_USER", ""),
                    help="adresse d'expédition (défaut : $AMCX_SMTP_USER)")
    ap.add_argument("--from-name", default="", help="nom affiché de l'expéditeur")
    ap.add_argument("--user", default="", help="identifiant SMTP (défaut : --from)")
    ap.add_argument("--host", default="", help=f"défaut : config, sinon {DEFAULT_HOST}")
    ap.add_argument("--port", type=int, default=0, help=f"défaut : config, sinon {DEFAULT_PORT}")
    # ⚠ Le journal suit le fichier de notes, il ne le devine pas : envoyer les
    # notes d'un ENSEMBLE en journalisant dans le projet actif ferait sauter
    # les étudiants déjà servis pour l'examen — même adresse, autre note.
    ap.add_argument("--log", type=Path, default=SENT_LOG,
                    help=f"journal des envois (défaut : {SENT_LOG})")
    ap.add_argument("--notes", type=Path, default=NOTES_CSV,
                    help=f"csv des notes (défaut : {NOTES_CSV})")
    ap.add_argument("--score-col",
                    default=cfg.get("mail_score_col") or "note_finale",
                    help="colonne de note à annoncer (défaut : celle de la config)")
    ap.add_argument("--out-of", type=float, default=None,
                    help="barème affiché (défaut : l'échelle de la note finale)")
    ap.add_argument("--template", type=Path, default=None,
                    help="gabarit (défaut : celui du projet, éditable dans l'onglet Courriels)")
    ap.add_argument("--only", action="append", default=[],
                    help="n'envoyer qu'à cette adresse (répétable) — pour un essai")
    ap.add_argument("--limit", type=int, default=0, help="s'arrêter après N envois")
    ap.add_argument("--force", action="store_true",
                    help="ré-envoyer même aux adresses déjà servies")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="pause entre deux envois, en secondes")
    ap.add_argument("--no-floor", action="store_true",
                    help="annoncer les notes négatives telles quelles")
    a = ap.parse_args(argv)

    try:
        data = load_recipients(a.notes, a.score_col, floor_at_zero=not a.no_floor)
        template = (a.template.read_text(encoding="utf-8") if a.template
                    else load_template())
    except (MailError, OSError) as e:
        print(f"✘ {e}", file=sys.stderr)
        return 1

    recipients, skipped = data["recipients"], data["skipped"]
    # Précédence : --out-of > barème réglé dans l'onglet > échelle déduite.
    # ⚠ Le barème réglé vaut pour TOUTE colonne : l'interface le laisse choisir
    # avec « QCM brut », et la ligne de commande refusait alors de partir en
    # réclamant --out-of — deux chemins, deux comportements.
    max_score = a.out_of if a.out_of is not None else float(
        cfg.get("mail_max_score") or 0)
    if not max_score:
        if a.score_col == "note_finale":
            max_score = default_max_score(cfg)
        else:
            # Pour une note brute ou importée, l'échelle n'est déductible de
            # rien : la deviner mettrait un barème faux dans chaque message.
            print(f"✘ --out-of est requis avec --score-col {a.score_col} "
                  "(aucun barème réglé dans l'onglet Courriels).",
                  file=sys.stderr)
            return 1
    # ⚠ Les réglages de l'onglet Courriels font foi : la ligne de commande et
    # l'interface doivent envoyer le MÊME message. Les options les surchargent
    # ponctuellement, elles ne repartent pas de zéro.
    date = a.date or cfg.get("mail_date") or datetime.now().strftime("%d/%m/%Y")
    subject = a.subject or cfg.get("mail_subject") or default_subject(date)
    sender = (a.sender or cfg.get("mail_sender") or "").strip()
    sender_name = a.from_name or cfg.get("mail_sender_name") or sender
    user = (a.user or cfg.get("mail_smtp_user") or sender).strip()
    host = a.host or cfg.get("mail_smtp_host") or DEFAULT_HOST
    port = a.port or int(cfg.get("mail_smtp_port") or DEFAULT_PORT)

    if a.only:
        keep = {e.lower() for e in a.only}
        recipients = [r for r in recipients if r["email"].lower() in keep]
    done = set() if a.force else already_sent(a.log)
    resent = [r for r in recipients if r["email"] in done]
    recipients = [r for r in recipients if r["email"] not in done]
    if a.limit:
        recipients = recipients[:a.limit]

    print(f"Notes    : {a.notes}  (colonne « {a.score_col} », sur {max_score:g})")
    print(f"Objet    : {subject}")
    print(f"Gabarit  : {a.template or PROJECT_TEMPLATE}")
    print(f"À envoyer: {len(recipients)} destinataire(s)")
    for who, why in skipped:
        print(f"  ⚠ écarté — {who} : {why}")
    if resent:
        print(f"  ⏭ {len(resent)} déjà servi(s) d'après {a.log.name} "
              "(--force pour renvoyer)")
    if not recipients:
        print("Rien à envoyer.")
        return 0

    ex = recipients[0]
    print("\n--- aperçu du message (1er destinataire) " + "-" * 30)
    print(f"To: {ex['full_name']} <{ex['email']}>")
    try:
        # L'objet passe par le même moteur que le corps : l'aperçu doit montrer
        # la ligne telle qu'elle partira, `$date` résolu.
        print("Subject: " + render(subject, ex, date=date, max_score=max_score,
                                   sender_name=sender_name) + "\n")
        print(render(template, ex, date=date, max_score=max_score,
                     sender_name=sender_name))
    except MailError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 1
    print("-" * 70)

    if not a.send:
        print(f"\nSimulation — RIEN n'a été envoyé. Ajoute --send pour l'envoi réel.")
        print("Conseil : d'abord --only <ta-propre-adresse> --send.")
        return 0
    if not sender:
        print("✘ --from est requis pour envoyer.", file=sys.stderr)
        return 1

    try:
        password = smtp_password()
    except MailError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 1

    print(f"\nEnvoi via {host}:{port} en tant que {user}…")
    try:
        sent, failures = send_all(
            recipients, host=host, port=port, user=user, password=password,
            sender=sender, sender_name=sender_name, subject=subject,
            template=template, date=date, max_score=max_score, delay=a.delay,
            log_path=a.log)
    except (smtplib.SMTPException, OSError) as e:
        print(f"✘ connexion SMTP : {e}", file=sys.stderr)
        return 1

    print(f"\n{sent} envoyé(s), {len(failures)} échec(s). Journal : {a.log}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
