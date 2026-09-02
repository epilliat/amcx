"""Sujet de l'examen — `sujet/exam.tex` est la **source de vérité unique**.

Deux modes coexistent :

- **canonique** (futur, sujets créés via l'UI) : le tex est structuré par des
  marqueurs `%%QCM-…` qui encadrent préambule, header, blocs ordonnés et feuille
  de réponses. Le round-trip UI ⇔ tex est garanti. CRUD libre.
- **legacy** (EXAM_2026 et tout sujet écrit à la main) : pas de marqueurs ; on
  parse uniquement les `\\begin{question*}…\\end{...}` comme aujourd'hui. CRUD
  réduit à l'édition de questions existantes (`save_questions`).

API publique :
- `parse_subject()`  : retourne `{config, blocks, mode}` (canonique ou legacy).
- `parse_tex()`      : compat — `{q: {…}}` indexé par ordre, dérivé du précédent.
- `effective_spec(q, copy=1)` : type / options / correct / tag (mapping per-copy).
- `get_bareme(copy=1)`        : barème par lettre pour la copie demandée.
- `max_score(q, copy=1)`, `total_max(copy=1)`
- `save_questions(updates)`  : compat — réécrit les blocs question (mode legacy
  ou mise à jour ciblée en canonique).
- `is_canonical(tex)`        : détecte le mode.
- CRUD canonique : `add_block`, `delete_block`, `move_block`, `update_block`,
  `duplicate_block`, `update_config`, `update_header`, `update_answer_sheet`,
  `regenerate_seed`.
- `compile_pdf()`    : `pdflatex exam.tex` → `sujet/DOC-sujet.pdf` + `exam.xy`.
- `pdf_regions()`    : région PDF de chaque question (pour l'aperçu).

AMC randomise l'ordre des réponses ; le calage (`exam.xy` ou `layout.sqlite`)
donne la lettre de feuille de chaque réponse par copie (`_tex_chars(copy)`).
"""

from __future__ import annotations

import copy as _copy
import json
import os
import random
import re
import secrets
import shutil
import subprocess
from datetime import datetime
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock

import layout_store

# SUJET_DIR vit dans le projet actif (cf. config.project_root()).
# Calculé à l'import (donc figé au démarrage du process) — un switch de projet
# implique un restart Flask, donc SUJET_DIR sera recalculé proprement.
from config import project_root as _project_root  # noqa: E402

ROOT = _project_root()
SUJET_DIR = ROOT / "sujet"

# `automultiplechoice.sty` vendorisé — vit dans l'INSTALLATION (comme les
# modèles ML), pas dans le projet : c'est du code, pas de la donnée.
# Il n'est pas sur CTAN, donc ni MiKTeX ni MacTeX ne peuvent l'installer ;
# sans lui, aucun sujet n'est compilable hors Debian/Ubuntu.
# Cf. auto_grading/tex/README.md.
TEX_DIR = Path(__file__).resolve().parent / "tex"
AMC_STY = TEX_DIR / "automultiplechoice.sty"
EXAM_TEX = SUJET_DIR / "exam.tex"
SUJET_PDF = SUJET_DIR / "DOC-sujet.pdf"
EXAM_XY = SUJET_DIR / "exam.xy"
OPEN_ZONES_JSON = SUJET_DIR / "open_zones.json"  # géométrie des cases freeform (HTR)
SUBJECT_JSON = SUJET_DIR / "subject.json"        # SOURCE DE VÉRITÉ (blocs propres)

_Q_BEGIN = re.compile(r"\\begin\{(questionmult|question)\}\{([^}]+)\}")
_REP_BEGIN = re.compile(r"\\begin\{(reponseshoriz|reponses)\}")
_REP_END = re.compile(r"\\end\{reponses(?:horiz)?\}")
_BAREME = re.compile(r"\s*\\bareme\{")

# --- marqueurs canoniques (toujours en colonne 0, jamais indentés) ---------
_QCM_BLOCK_RE = re.compile(r"^%%QCM-BLOCK\s+(?P<attrs>.+?)\s*$", re.MULTILINE)
_QCM_PREAMBLE_START = "%%QCM-PREAMBLE"
_QCM_PREAMBLE_END = "%%QCM-PREAMBLE-END"
_QCM_BLOCKS_START = "%%QCM-BLOCKS-START"
_QCM_BLOCKS_END = "%%QCM-BLOCKS-END"
_QCM_HEADER_START = "%%QCM-HEADER"
_QCM_HEADER_END = "%%QCM-HEADER-END"
_QCM_ANSWER_SHEET_START = "%%QCM-ANSWER-SHEET"
_QCM_ANSWER_SHEET_END = "%%QCM-ANSWER-SHEET-END"
# Section optionnelle : blocs `answerbox` avec `placement=end`, sérialisés
# APRÈS la feuille de réponses et AVANT la fermeture de \exemplaire.
_QCM_ANSWER_END_BLOCKS_START = "%%QCM-ANSWER-END-BLOCKS-START"
_QCM_ANSWER_END_BLOCKS_END = "%%QCM-ANSWER-END-BLOCKS-END"
_QCM_EXEMPLAIRE_OPEN = "%%QCM-EXEMPLAIRE-OPEN"
_QCM_EXEMPLAIRE_CLOSE = "%%QCM-EXEMPLAIRE-CLOSE"

# Kinds canoniques supportés (la validation amont d'`add_block` les vérifie).
_VALID_KINDS = ("text", "question_qcm", "question_open", "question_freeform",
                "answerbox")

# Kinds qu'on sait lire, éditer et sérialiser, mais qu'on n'autorise plus à
# CRÉER. `question_freeform` est désactivé parce qu'il ne s'imprime pas :
# `render_block` l'enveloppe dans `\element{open}{…}`, et un `\AMCOpen` ne
# survit pas au stockage dans ce registre de tokens (vérifié : sorti du groupe,
# il s'imprime ; dedans, rien — pas même une erreur LaTeX). Une question libre
# ajoutée au sujet en disparaissait donc silencieusement.
#
# ⚠ La liste ne filtre QUE la création : les blocs existants restent lisibles
# et supprimables, sinon un projet qui en contient deviendrait inéditable.
DISABLED_KINDS = frozenset({"question_freeform"})
_DISABLED_MSG = {
    "question_freeform": (
        "Les questions libres (HTR) sont désactivées : elles ne s'impriment "
        "pas dans le PDF compilé. Utilisez « zone réponse » pour un cadre "
        "manuscrit noté par le correcteur."),
}
# Marqueur (commentaire LaTeX) qui transporte le `data` d'un question_freeform
# entre deux saves : `expected_answer` / `match_mode` / `numeric_tol` / `points`
# ne sont PAS rendus dans le PDF (le HTR les lit côté serveur). On les sérialise
# en JSON sur une ligne au début du body pour un round-trip fidèle.
_FF_DATA_MARKER = "%%QCM-FREEFORM-DATA "

DEFAULT_SEED = 12378354


# --------------------------------------------------------------------------
# Modèle de données canonique
# --------------------------------------------------------------------------

@dataclass
class HeaderBlock:
    establishment: str = ""
    year: str = ""
    author: str = ""
    title: str = ""
    duration: str = ""
    subtitle: str = ""
    instructions: str = ""
    # `raw_tex` : LaTeX brut de l'en-tête. Si rempli, prime sur les champs
    # structurés ci-dessus (utilisé pour les sujets migrés depuis legacy dont
    # l'en-tête est trop complexe pour être décomposé proprement).
    raw_tex: str = ""


@dataclass
class AnswerSheetConfig:
    id_grid_digits: int = 4
    name_field: bool = True
    columns: int = 2
    extra_instructions: str = ""


@dataclass
class SubjectConfig:
    num_copies: int = 1
    random_seed: int = DEFAULT_SEED
    shuffle_answers: bool = True
    shuffle_questions: bool = False
    header: HeaderBlock = field(default_factory=HeaderBlock)
    answer_sheet: AnswerSheetConfig = field(default_factory=AnswerSheetConfig)
    # Préambule/feuille de réponses *bruts*, hérités d'un sujet existant
    # (migration legacy → canonique). Vides = on génère un préambule et une
    # feuille de réponses canoniques depuis `render_preamble`/`render_answer_sheet`.
    preamble_tex: str = ""
    answer_sheet_tex: str = ""


@dataclass
class Block:
    bid: str
    kind: str             # 'text' | 'question_qcm' | 'question_open' | 'question_freeform' | 'answerbox'
    data: dict = field(default_factory=dict)
    # positions du bloc dans le tex source (utilisé en mode legacy pour
    # `save_questions` qui patche en place ; -1 en mode canonique).
    _start: int = -1
    _end: int = -1


def _gen_bid(kind: str) -> str:
    """Identifiant stable court, préfixé par le kind."""
    prefix = {"text": "t", "question_qcm": "q", "question_open": "o",
              "question_freeform": "f", "answerbox": "a"}.get(kind, "b")
    return f"{prefix}-{secrets.token_hex(4)}"


def _block_to_dict(b: Block) -> dict:
    """Convertit un Block en dict JSON-sérialisable (pour l'API)."""
    return {"bid": b.bid, "kind": b.kind, "data": b.data}


def _config_to_dict(cfg: SubjectConfig) -> dict:
    return {
        "num_copies": cfg.num_copies,
        "random_seed": cfg.random_seed,
        "shuffle_answers": cfg.shuffle_answers,
        "shuffle_questions": cfg.shuffle_questions,
        "header": cfg.header.__dict__.copy(),
        "answer_sheet": cfg.answer_sheet.__dict__.copy(),
    }


def subject_to_dict(subject: dict) -> dict:
    """Sérialise un subject (résultat de `parse_subject`) en dict JSON pour l'API.

    NB : volontairement SANS `preamble_tex`/`answer_sheet_tex` (l'UI n'en a pas
    besoin). Pour la persistance complète (store), voir `_subject_to_store`.
    """
    return {
        "config": _config_to_dict(subject["config"]),
        "blocks": [_block_to_dict(b) for b in subject["blocks"]],
        "mode": subject.get("mode", "empty"),
    }


# --------------------------------------------------------------------------
# Store JSON : `sujet/subject.json` est la SOURCE DE VÉRITÉ du sujet.
# `exam.tex` n'est plus qu'un artefact, régénéré uniquement par `compile_pdf`.
# (De)sérialisation COMPLÈTE (inclut preamble_tex / answer_sheet_tex que
# `_config_to_dict` omet).
# --------------------------------------------------------------------------

def _subject_to_store(subject: dict) -> dict:
    """Subject {config, blocks} → dict JSON complet (persistance)."""
    cfg: SubjectConfig = subject["config"]
    return {
        "version": 1,
        "config": {
            "num_copies": cfg.num_copies,
            "random_seed": cfg.random_seed,
            "shuffle_answers": cfg.shuffle_answers,
            "shuffle_questions": cfg.shuffle_questions,
            "header": cfg.header.__dict__.copy(),
            "answer_sheet": cfg.answer_sheet.__dict__.copy(),
            "preamble_tex": cfg.preamble_tex,
            "answer_sheet_tex": cfg.answer_sheet_tex,
        },
        "blocks": [{"bid": b.bid, "kind": b.kind, "data": b.data}
                   for b in subject["blocks"]],
        # Le mode DOIT être persisté : un sujet legacy bootstrappé dans le
        # store restait « legacy » en mémoire mais repassait « canonical » à
        # la relecture — le verrou lecture seule sautait tout seul, et la
        # compilation suivante réécrivait exam.tex depuis le gabarit.
        "mode": subject.get("mode", "canonical"),
    }


def _subject_from_store(d: dict) -> dict:
    """dict JSON complet → Subject {config, blocks, mode}.

    `mode` est relu du store : un sujet legacy le reste tant qu'il n'a pas été
    migré explicitement.
    """
    cd = d.get("config", {}) or {}
    header = HeaderBlock(**{k: v for k, v in (cd.get("header") or {}).items()
                           if k in HeaderBlock.__dataclass_fields__})
    answer_sheet = AnswerSheetConfig(
        **{k: v for k, v in (cd.get("answer_sheet") or {}).items()
           if k in AnswerSheetConfig.__dataclass_fields__})
    cfg = SubjectConfig(
        num_copies=int(cd.get("num_copies", 1) or 1),
        random_seed=int(cd.get("random_seed", DEFAULT_SEED) or DEFAULT_SEED),
        shuffle_answers=bool(cd.get("shuffle_answers", True)),
        shuffle_questions=bool(cd.get("shuffle_questions", False)),
        header=header,
        answer_sheet=answer_sheet,
        preamble_tex=cd.get("preamble_tex", "") or "",
        answer_sheet_tex=cd.get("answer_sheet_tex", "") or "",
    )
    blocks = [Block(bid=b.get("bid") or _gen_bid(b.get("kind", "")),
                    kind=b.get("kind", ""), data=b.get("data") or {})
              for b in (d.get("blocks") or [])]
    return {"config": cfg, "blocks": blocks,
            "mode": d.get("mode") or "canonical"}


def _write_subject_json(subject: dict) -> None:
    """Écrit `subject.json` de façon atomique (tmp + os.replace)."""
    SUJET_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_subject_to_store(subject), ensure_ascii=False, indent=2)
    tmp = SUBJECT_JSON.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, SUBJECT_JSON)


# Avertissements de chargement du store, remontés à l'UI. Vidés à la lecture.
_STORE_WARNINGS: list[str] = []


def pop_store_warnings() -> list[str]:
    """Retourne et vide les avertissements de chargement du store."""
    out = list(_STORE_WARNINGS)
    _STORE_WARNINGS.clear()
    return out


def _load_subject_store() -> dict:
    """Charge le sujet depuis le store JSON (source de vérité).

    Bootstrap : si `subject.json` est absent mais `exam.tex` existe, on parse le
    .tex (canonique OU legacy) et on écrit `subject.json` une fois — le .tex
    n'est PAS modifié. Si rien n'existe → sujet vide.
    """
    with _io_lock:
        if SUBJECT_JSON.exists():
            try:
                return _subject_from_store(
                    json.loads(SUBJECT_JSON.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                # Store illisible : on repart du .tex, mais SANS le faire en
                # silence — le .tex n'est réécrit qu'à la compilation, donc
                # tout ce qui a été édité depuis serait perdu sans un mot.
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                corrupt = SUBJECT_JSON.with_name(f"subject.json.corrupt-{stamp}")
                try:
                    SUBJECT_JSON.replace(corrupt)
                except OSError:
                    corrupt = None
                _STORE_WARNINGS.append(
                    f"subject.json illisible ({e}) — repli sur exam.tex."
                    + (f" Fichier mis de côté : {corrupt.name}." if corrupt else "")
                    + " Les éditions faites depuis la dernière compilation"
                      " sont perdues.")
                print("⚠ " + _STORE_WARNINGS[-1])
        if EXAM_TEX.exists():
            sub = _parse_tex_subject(EXAM_TEX.read_text(encoding="utf-8"))
            try:
                _write_subject_json(sub)
            except OSError:
                pass
            return sub
        return {"config": SubjectConfig(), "blocks": [], "mode": "empty"}


def save_subject_store(subject: dict) -> None:
    """Persiste le sujet dans `subject.json` (NE TOUCHE PAS au .tex) + caches."""
    _write_subject_json(subject)
    _invalidate_caches()


# --------------------------------------------------------------------------
# Détection du mode
# --------------------------------------------------------------------------

def is_canonical(tex: str) -> bool:
    """Vrai ssi le tex porte les marqueurs canoniques (en colonne 0)."""
    return _QCM_BLOCKS_START in tex and _QCM_PREAMBLE_START in tex


# --------------------------------------------------------------------------
# Lettres de feuille AMC (mapping per-copy)
# --------------------------------------------------------------------------

_charmap_by_copy: dict[int, dict] = {}


def _tex_chars(copy: int = 1) -> dict:
    """{q: [char de feuille par réponse, dans l'ordre de déclaration LaTeX]}
    pour la copie demandée (1 = comportement historique).

    Si `layout_store.get_layout` n'accepte pas encore `copy=` (Phase 1 livrée
    avant Phase 2), on retombe sur la copie #1 sans crash.
    """
    if copy in _charmap_by_copy:
        return _charmap_by_copy[copy]
    cm: dict = {}
    try:
        try:
            lay = layout_store.get_layout(copy=copy)
        except TypeError:
            lay = layout_store.get_layout()
        for b in lay.sheet_boxes():
            if b.answer >= 1:
                cm.setdefault(b.question, []).append(b.char)
    except Exception:
        cm = {}
    _charmap_by_copy[copy] = cm
    return cm


# --------------------------------------------------------------------------
# Correspondance « numéro de question AMC » ↔ blocs du sujet
# --------------------------------------------------------------------------

# Blocs qui produisent des cases à lettres sur la feuille de réponses sans être
# des QCM notés automatiquement (cases de notation cochées par le correcteur).
_OPEN_KINDS = ("question_open", "question_freeform", "answerbox")

_qmap_by_copy: dict[int, dict] = {}


def letters_stale() -> bool:
    """True si les lettres de case affichées peuvent être fausses.

    Les lettres viennent du calage compilé (`exam.xy`). Si le sujet a été édité
    depuis la dernière compilation, elles décrivent l'ANCIEN sujet : ajouter ou
    retirer une réponse est sans risque (`_attach_chars` exige que le nombre
    corresponde et retire la lettre sinon), mais **réordonner** des réponses
    laisse des lettres silencieusement fausses — elles suivent la position, pas
    la réponse.
    """
    try:
        if not EXAM_XY.exists():
            return False          # jamais compilé : aucune lettre n'est affichée
        src = SUBJECT_JSON if SUBJECT_JSON.exists() else EXAM_TEX
        return src.exists() and src.stat().st_mtime > EXAM_XY.stat().st_mtime
    except OSError:
        return False


def charmap_for_copy(copy: int = 1) -> dict:
    """`{q_tex: [lettre par réponse, dans l'ordre de déclaration LaTeX]}`.

    Sert au sélecteur d'exemplaire de l'onglet Sujet : avec `shuffle_answers`,
    la lettre imprimée pour une même réponse change d'un exemplaire à l'autre.

    ⚠ Indexé par **numéro de QCM** (1, 2, … dans l'ordre du document, la clé de
    `parse_tex()`), pas par numéro AMC : `_tex_chars` est indexé par numéro AMC,
    qui compte aussi les colonnes du code étudiant et les barèmes (piège #1 du
    CLAUDE.md). Les questions non-QCM sont écartées.
    """
    raw = _tex_chars(copy=copy)
    qcm = (amc_question_map(copy=copy).get("qcm") or {})
    return {int(q_tex): list(raw[num]) for num, q_tex in qcm.items() if num in raw}


def amc_question_map(copy: int = 1) -> dict:
    """Rôle de chaque numéro de question du calage, pour la copie donnée.

    AMC numérote **toutes** les questions du sujet (QCM, ouvertes, colonnes du
    code étudiant) ; `parse_tex()` n'indexe que les QCM. Les deux coïncident
    tant que le rendu place les questions ouvertes après la feuille de réponses
    — mais rien ne le garantit sur un sujet importé. Cette carte rend la
    correspondance explicite au lieu de la supposer.

    Source primaire : `layout.question_names` (tag AMC → numéro), écrit par
    pdflatex dans le `.xy` — exact même si l'ordre diffère. Repli positionnel
    quand les tags manquent (calage ancien) ou sont ambigus (tag dupliqué).

    Retourne `{"qcm": {num_amc: q_tex}, "open": {num_amc: bid}, "id": [num…],
    "issues": [str…]}`. `issues` non vide = sujet et calage ont divergé
    (recompiler), et les notes affichées peuvent être fausses.
    """
    if copy in _qmap_by_copy:
        return _qmap_by_copy[copy]
    out = {"qcm": {}, "open": {}, "id": [], "issues": []}
    try:
        try:
            lay = layout_store.get_layout(copy=copy)
        except TypeError:
            lay = layout_store.get_layout()
        boxes_by_q: dict[int, list] = {}
        for b in lay.sheet_boxes():
            boxes_by_q.setdefault(b.question, []).append(b)
    except Exception as e:  # noqa: BLE001 — pas de calage (projet non compilé)
        out["issues"].append(f"calage illisible : {e}")
        _qmap_by_copy[copy] = out
        return out

    letters = []
    for q, bs in sorted(boxes_by_q.items()):
        chars = [b.char for b in bs if b.char]
        if chars and all(str(c).isdigit() for c in chars):
            out["id"].append(q)          # colonne du code étudiant
        else:
            letters.append((q, len(bs)))

    qcm = parse_tex()                     # {q_tex: info}, QCM en ordre document
    qcm_qs = sorted(qcm)
    subject = parse_subject()
    open_blocks = [b for b in subject["blocks"] if b.kind in _OPEN_KINDS]

    # tag AMC → numéro(s). Un tag dupliqué (question importée dans son propre
    # projet d'origine) est inutilisable : on le retire de l'index.
    by_tag: dict[str, list] = {}
    for num, name in (getattr(lay, "question_names", None) or {}).items():
        by_tag.setdefault(str(name).strip(), []).append(num)
    unique_tag = {t: n[0] for t, n in by_tag.items() if len(n) == 1}
    letter_nums = {num for num, _ in letters}

    # 1. QCM : par tag quand c'est possible, sinon par position.
    unmatched_tex = []
    for q_tex in qcm_qs:
        tag = str(qcm[q_tex].get("tag") or "").strip()
        num = unique_tag.get(tag)
        if num in letter_nums and num not in out["qcm"]:
            out["qcm"][num] = q_tex
        else:
            unmatched_tex.append(q_tex)
    free = [n for n, _ in letters if n not in out["qcm"]]
    if unmatched_tex:
        for num, q_tex in zip(free, unmatched_tex):
            out["qcm"][num] = q_tex
        if len(free) < len(unmatched_tex):
            out["issues"].append(
                f"{len(unmatched_tex) - len(free)} QCM du sujet sans question "
                f"correspondante dans le calage — sujet modifié depuis la "
                f"dernière compilation ?")

    # 2. Contrôle : le nombre de cases doit coller au nombre de réponses.
    n_boxes_of = dict(letters)
    for num, q_tex in out["qcm"].items():
        n_ans = len(qcm[q_tex].get("answers") or [])
        if n_boxes_of.get(num) != n_ans:
            out["issues"].append(
                f"Q{num} du calage a {n_boxes_of.get(num)} case(s) mais la "
                f"question « {qcm[q_tex].get('tag') or q_tex} » en déclare {n_ans}")

    # 3. Ce qui reste appartient aux blocs à cases de notation. AMC les nomme
    #    `bareme-<bid>` ; repli positionnel sinon.
    rest = [n for n, _ in letters if n not in out["qcm"]]
    by_bid = {}
    for num in rest:
        name = str((getattr(lay, "question_names", None) or {}).get(num, "")).strip()
        bid = name[len("bareme-"):] if name.startswith("bareme-") else None
        if bid:
            by_bid[num] = bid
    for num in rest:
        if num in by_bid:
            out["open"][num] = by_bid[num]
    leftover = [n for n in rest if n not in out["open"]]
    free_blocks = [b.bid for b in open_blocks if b.bid not in out["open"].values()]
    for num, bid in zip(leftover, free_blocks):
        out["open"][num] = bid
    # On ne signale que le SURPLUS côté calage : des questions à lettres qu'on
    # n'arrive à rattacher à aucun bloc du sujet. L'inverse est normal — un bloc
    # ouvert peut n'avoir aucune case de notation sur la feuille de réponses
    # (AMCOpen sans barème, cases rendues sur une autre page…).
    if len(rest) > len(open_blocks):
        out["issues"].append(
            f"{len(rest) - len(open_blocks)} question(s) du calage ne "
            f"correspondent à aucun bloc du sujet")
    _qmap_by_copy[copy] = out
    return out


def check_layout_consistency(verbose: bool = True) -> list[str]:
    """Vérifie la cohérence sujet ↔ calage. Retourne la liste des anomalies.

    Appelé au démarrage du serveur : un décalage ici fausse silencieusement
    toutes les notes, autant le dire fort.
    """
    issues = amc_question_map(1)["issues"]
    if issues and verbose:
        print("⚠ Sujet et calage (.xy) incohérents — les notes peuvent être fausses :")
        for i in issues:
            print(f"    · {i}")
        print("  → recompile le sujet (onglet Sujet → Compiler).")
    return issues


# --------------------------------------------------------------------------
# Parsing LaTeX bas niveau (helpers)
# --------------------------------------------------------------------------

def _balanced(s, i):
    """`s[i]` doit être '{'. Retourne (contenu, index juste après le '}' fermant)."""
    if s[i] != "{":
        raise ValueError("attendu '{'")
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
    raise ValueError("accolade non fermée")


def _frac(tok):
    """'1/2' -> 0.5 ; '-1/3' -> -0.333… ; '0' -> 0.0 ; sinon None."""
    if tok is None:
        return None
    tok = str(tok).strip()
    if not tok:
        return None
    try:
        if "/" in tok:
            num, den = tok.split("/", 1)
            return float(num) / float(den)
        return float(tok)
    except (ValueError, ZeroDivisionError):
        return None


def _parse_bareme(content):
    """'b=1/2,m=0' -> {'b': '1/2', 'm': '0'} (valeurs brutes, non converties)."""
    out = {}
    for part in content.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_block(body, kind, tag):
    """`body` = contenu entre `\\begin{question…}{tag}` et `\\end{…}`.

    Le type vient du `kind` LaTeX (`questionmult`→mult, `question`→single).
    """
    qtype = "mult" if kind == "questionmult" else "single"
    rm = _REP_BEGIN.search(body)
    if rm:
        env = rm.group(1)
        statement = body[:rm.start()]
        rest = body[rm.end():]
        em = _REP_END.search(rest)
        ans_raw = rest[:em.start()] if em else rest
    else:
        env, statement, ans_raw = "reponses", body, ""

    answers = []
    i = 0
    while i < len(ans_raw):
        cand = None
        for kw, correct in (("\\bonne", True), ("\\mauvaise", False)):
            k = ans_raw.find(kw, i)
            if k != -1 and (cand is None or k < cand[0]):
                cand = (k, kw, correct)
        if cand is None:
            break
        k, kw, correct = cand
        brace = ans_raw.find("{", k + len(kw))
        if brace == -1:
            break
        text, after = _balanced(ans_raw, brace)
        bm = {}
        bmatch = _BAREME.match(ans_raw, after)
        if bmatch:
            content, after = _balanced(ans_raw, bmatch.end() - 1)
            bm = _parse_bareme(content)
        ans = {"text": text, "correct": correct, "char": None}
        if qtype == "mult":
            ans["points"] = bm.get("b" if correct else "m", "0")
        answers.append(ans)
        i = after

    if qtype == "single":
        m = re.search(r"\\bareme\{", body)
        if m:
            content, _ = _balanced(body, m.end() - 1)
            bareme = {"value": _parse_bareme(content).get("b", "1")}
        else:
            bareme = {"value": "1"}
    else:
        bareme = {}

    return {"tag": tag, "type": qtype, "env": env,
            "statement": statement, "answers": answers, "bareme": bareme}


def _parse_legacy_questions(tex):
    """Parse les `\\begin{question*}` -> {q: {…}} avec positions dans le tex."""
    questions = {}
    q = 0
    for m in _Q_BEGIN.finditer(tex):
        q += 1
        kind, tag = m.group(1), m.group(2).strip()
        end = tex.find("\\end{%s}" % kind, m.end())
        if end == -1:
            continue
        try:
            info = _parse_block(tex[m.end():end], kind, tag)
        except ValueError:
            continue
        info["block"] = (m.start(), end + len("\\end{%s}" % kind))
        questions[q] = info
    return questions


def _attach_chars(questions, copy=1):
    """Remplit `answers[i]['char']` à partir du calage de la copie."""
    charmap = _tex_chars(copy=copy)
    for q, info in questions.items():
        chars = charmap.get(q)
        if chars and len(chars) == len(info["answers"]):
            for a, ch in zip(info["answers"], chars):
                a["char"] = ch
    return questions


# --------------------------------------------------------------------------
# Parsing canonique : structure complète
# --------------------------------------------------------------------------

def _parse_attrs(line):
    """'bid=q-a3f2k7 kind=question_qcm qtype=mult tag=foo' -> dict."""
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_grading_cases(scoring):
    """Parse les \\correctchoice/\\wrongchoice + \\scoring d'un AMCOpen."""
    cases = []
    i = 0
    n = len(scoring)
    while i < n:
        cand = None
        for kw, correct in (("\\correctchoice", True), ("\\wrongchoice", False)):
            k = scoring.find(kw, i)
            if k != -1 and (cand is None or k < cand[0]):
                cand = (k, kw, correct)
        if cand is None:
            break
        k, kw, _correct = cand
        j = k + len(kw)
        label = ""
        if j < n and scoring[j] == "[":
            end_b = scoring.find("]", j)
            if end_b > j:
                label = scoring[j + 1:end_b].strip()
                j = end_b + 1
        if j < n and scoring[j] == "{":
            _text, j = _balanced(scoring, j)
        sm = re.match(r"\s*\\scoring\{", scoring[j:])
        value = 0.0
        if sm:
            sv, dj = _balanced(scoring, j + sm.end() - 1)
            value = float(_frac(sv) or 0.0)
            j = dj
        cases.append({"label": label, "value": value})
        i = j
    return cases


def _parse_block_body(kind, body, attrs):
    """Parse le corps LaTeX d'un bloc canonique selon son kind."""
    if kind == "text":
        return {"tex": body}
    if kind == "question_qcm":
        qtype = attrs.get("qtype", "single")
        latex_kind = "questionmult" if qtype == "mult" else "question"
        tag = attrs.get("tag", "")
        m = re.search(r"\\begin\{" + latex_kind + r"\}\{([^}]+)\}", body)
        if m is None:
            return {"tag": tag, "qtype": qtype, "env": "reponses",
                    "statement": body, "answers": [], "value": "1"}
        end_m = re.search(r"\\end\{" + latex_kind + r"\}", body)
        end_pos = end_m.start() if end_m else len(body)
        inner = body[m.end():end_pos]
        info = _parse_block(inner, latex_kind, m.group(1).strip())
        return {
            "tag": info["tag"],
            "qtype": info["type"],
            "env": info["env"],
            "statement": info["statement"].strip(),
            "answers": [
                {"text": a["text"], "correct": a["correct"],
                 "bareme": a.get("points", "0")}
                for a in info["answers"]
            ],
            "value": info["bareme"].get("value", "1"),
        }
    if kind == "question_open":
        tag = attrs.get("tag", "")
        m = re.search(r"\\AMCOpen\s*\{", body)
        if m is None:
            return {"tag": tag, "statement": body, "lines": 4, "points": 1.0,
                    "grading_cases": [{"label": "0", "value": 0.0},
                                      {"label": "1", "value": 1.0}]}
        first_brace = m.end() - 1
        opt_content, after = _balanced(body, first_brace)
        lines = 4
        statement = ""
        lm = re.search(r"lines\s*=\s*(\d+)", opt_content)
        if lm:
            lines = int(lm.group(1))
        qm_brace = re.search(r"question\s*=\s*\{", opt_content)
        if qm_brace:
            statement, _ = _balanced(opt_content, qm_brace.end() - 1)
        else:
            qm2 = re.search(r"question\s*=\s*([^,]+)", opt_content)
            if qm2:
                statement = qm2.group(1).strip()
        sb = body.find("{", after)
        cases = []
        if sb != -1:
            scoring_content, _ = _balanced(body, sb)
            cases = _parse_grading_cases(scoring_content)
        points = max((c["value"] for c in cases), default=0.0)
        return {"tag": tag, "statement": statement, "lines": lines,
                "points": points, "grading_cases": cases}
    if kind == "question_freeform":
        # Round-trip : on lit la ligne `%%QCM-FREEFORM-DATA <json>` qui porte
        # `expected_answer`/`match_mode`/`numeric_tol`/`points` (cf.
        # `_render_freeform_body`). Le `\\AMCOpen` qui suit donne `lines` et
        # `statement`. Sans la ligne JSON (bloc édité à la main hors UI), on
        # tombe sur des défauts sûrs.
        import json as _json
        tag = attrs.get("tag", "")
        meta = {}
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(_FF_DATA_MARKER):
                try:
                    meta = _json.loads(line[len(_FF_DATA_MARKER):])
                except Exception:  # noqa: BLE001
                    meta = {}
                break
        # Extract statement + lines from the AMCOpen (idem `question_open`).
        m = re.search(r"\\AMCOpen\s*\{", body)
        statement = ""
        lines = 2
        if m is not None:
            first_brace = m.end() - 1
            opt_content, _after = _balanced(body, first_brace)
            lm = re.search(r"lines\s*=\s*(\d+)", opt_content)
            if lm:
                lines = int(lm.group(1))
            qm_brace = re.search(r"question\s*=\s*\{", opt_content)
            if qm_brace:
                statement, _ = _balanced(opt_content, qm_brace.end() - 1)
            else:
                qm2 = re.search(r"question\s*=\s*([^,]+)", opt_content)
                if qm2:
                    statement = qm2.group(1).strip()
        try:
            points = float(meta.get("points", 1.0))
        except (TypeError, ValueError):
            points = 1.0
        try:
            numeric_tol = float(meta.get("numeric_tol", 0.01))
        except (TypeError, ValueError):
            numeric_tol = 0.01
        return {
            "tag": meta.get("tag") or tag,
            "statement": statement.strip(),
            "lines": lines,
            "points": points,
            "expected_answer": meta.get("expected_answer", ""),
            "match_mode": meta.get("match_mode", "exact"),
            "numeric_tol": numeric_tol,
        }
    if kind == "answerbox":
        # Extraction tolérante du header :
        #   - nouveau format avec ref : `\noindent \fbox{\small\bfseries R<n>}\quad \textbf{title}\par\vspace{2pt}`
        #   - ou sans titre        : `\noindent \fbox{\small\bfseries R<n>}\quad \par\vspace{2pt}`
        #   - ancien format        : `\noindent\textbf{title}\par\vspace{2pt}`
        # On strip la ref `R<n>` à la relecture (render_subject la recalcule à
        # partir de l'ordre des blocs → la persister dans le tex la dupliquerait).
        placement = attrs.get("placement", "inline")
        height = "5cm"
        hm = re.search(r"\\begin\{answerbox\}\s*\[\s*([^\]]+?)\s*\]", body)
        if hm:
            height = hm.group(1).strip()
        # Coupe le body au début du cadre answerbox.
        cut = body.find("\\begin{answerbox}")
        head = body[:cut] if cut >= 0 else ""
        title = ""
        instructions = ""
        # Regex header tolérante — couvre tous les formats produits par les
        # versions successives de `_render_answerbox_body` :
        #   1. `\noindent \textbf{Question~\ref{q-<bid>}} — \textbf{title}\par\vspace{...}`
        #   2. `\noindent \textbf{Question~\ref{q-<bid>}}\par\vspace{...}` (sans titre)
        #   3. `\noindent\textbf{title}\par\vspace{...}` (sans barème)
        #   4. `\noindent \fbox{\small\bfseries R<n>}\quad \textbf{title}\par...` (legacy R<n>)
        # On extrait le `title` = dernier `\textbf{...}` du header qui n'est
        # PAS un wrapping de `Question~\ref{...}`.
        header_re = re.compile(
            r"\s*\\noindent\s*"
            r"(?:\\fbox\{\\small\\bfseries\s+R\d+\}\\quad\s*)?"  # legacy R<n>
            # « Question 3 » (nouveau) ou « Question \ref{q-…} » (ancien) + séparateur
            r"(?:\\textbf\{Question~?(?:\\ref\{q-[^}]+\}|\d+)\}(?:\s*[—-]+\s*)?)?"
            r"(?:\\textbf\{([^}]*)\})?"                            # titre éventuel
            r"\s*\\par(?:\\vspace\{[^}]*\})?\s*",
            re.DOTALL,
        )
        # Consomme TOUS les headers consécutifs au début du body (les
        # accumulations dues à d'anciens bugs de parsing sont nettoyées).
        cursor = head
        while True:
            tm = header_re.match(cursor)
            if not tm or tm.end() == 0:
                break
            captured = (tm.group(1) or "").strip()
            if captured and not title:
                title = captured
            # Le header doit contenir au moins un marqueur reconnaissable —
            # sinon on s'arrête pour ne pas avaler du contenu utilisateur.
            if ("\\fbox" not in tm.group(0)
                    and "\\textbf" not in tm.group(0)
                    and "\\ref" not in tm.group(0)):
                break
            cursor = cursor[tm.end():].lstrip("\n")
        instructions = cursor.strip("\n")
        # Barème : on extrait `bareme_max` (max score) et `bareme_step` (pas)
        # depuis les `\scoring{value}` du bloc `\element{bareme}{…}`.
        # `bareme_max` = max des scores, `bareme_step` = différence entre
        # 2 scores consécutifs (ou 1 si un seul step détectable).
        bareme_max = 0.0
        bareme_step = 1.0
        if "\\element{bareme}" in body:
            scores = []
            for m in re.finditer(r"\\scoring\{([0-9./]+)\}", body):
                s = m.group(1)
                try:
                    scores.append(_frac(s) if "/" in s else float(s))
                except (TypeError, ValueError):
                    pass
            scores = [s for s in scores if s is not None]
            if scores:
                bareme_max = max(scores)
                # Step = diff entre les 2 premiers non-nuls, OU 1 par défaut.
                if len(scores) >= 2:
                    diff = scores[1] - scores[0]
                    if diff > 0:
                        bareme_step = diff
        # Retombe à intégrale si le step est très proche d'un entier.
        if abs(bareme_max - round(bareme_max)) < 1e-9:
            bareme_max = int(round(bareme_max))
        if abs(bareme_step - round(bareme_step)) < 1e-9:
            bareme_step = int(round(bareme_step))
        return {"height": height, "placement": placement,
                "title": title, "instructions": instructions,
                "bareme_max": bareme_max, "bareme_step": bareme_step}
    return {"raw": body}


def _parse_header_tex(t):
    """Parse les métadonnées %%H:* du bloc HEADER canonique.

    Si le bloc commence par `%%H:raw-start` … `%%H:raw-end`, le contenu entre
    ces marqueurs est récupéré tel quel dans `h.raw_tex` (mode LaTeX brut).
    Sinon on parse les champs structurés (%%H:title{}, %%H:author{}, etc.).
    """
    h = HeaderBlock()
    raw_s = t.find("%%H:raw-start")
    raw_e = t.find("%%H:raw-end")
    if raw_s >= 0 and raw_e > raw_s:
        h.raw_tex = t[raw_s + len("%%H:raw-start"):raw_e].strip("\n")
        return h
    for k, pat in (("establishment", r"%%H:establishment\{([^}]*)\}"),
                   ("year",           r"%%H:year\{([^}]*)\}"),
                   ("author",         r"%%H:author\{([^}]*)\}"),
                   ("title",          r"%%H:title\{([^}]*)\}"),
                   ("duration",       r"%%H:duration\{([^}]*)\}"),
                   ("subtitle",       r"%%H:subtitle\{([^}]*)\}")):
        m = re.search(pat, t)
        if m:
            setattr(h, k, m.group(1))
    s = t.find("%%H:instructions-start")
    e = t.find("%%H:instructions-end")
    if s >= 0 and e > s:
        h.instructions = t[s + len("%%H:instructions-start"):e].strip("\n")
    return h


def _parse_answer_sheet_tex(t):
    """Parse les paramètres %%A:* du bloc ANSWER-SHEET canonique."""
    a = AnswerSheetConfig()
    m = re.search(r"%%A:id_grid_digits\{(\d+)\}", t)
    if m:
        a.id_grid_digits = int(m.group(1))
    a.name_field = "%%A:name_field{1}" in t
    m = re.search(r"%%A:columns\{(\d+)\}", t)
    if m:
        a.columns = int(m.group(1))
    s = t.find("%%A:extra_instructions-start")
    e = t.find("%%A:extra_instructions-end")
    if s >= 0 and e > s:
        a.extra_instructions = t[s + len("%%A:extra_instructions-start"):e].strip("\n")
    return a


def _parse_canonical(tex):
    """Parse un tex en mode canonique."""
    cfg = SubjectConfig()

    pre_s = tex.find(_QCM_PREAMBLE_START)
    pre_e = tex.find(_QCM_PREAMBLE_END)
    if pre_s >= 0 and pre_e > pre_s:
        preamble = tex[pre_s + len(_QCM_PREAMBLE_START):pre_e]
        m = re.search(r"\\AMCrandomseed\{(\d+)\}", preamble)
        if m:
            cfg.random_seed = int(m.group(1))
        # Si le préambule est custom (issu d'une migration), on le préserve tel
        # quel pour qu'un round-trip parse → render → parse ne perde rien.
        # Heuristique : si le préambule contient des packages ou macros
        # personnalisées (autres que le template fixe), on le considère custom.
        # Plus sûr : on conserve TOUJOURS la version sur disque ; le template
        # fixe ne sera regénéré que si l'utilisateur vide explicitement le champ.
        cfg.preamble_tex = preamble.strip("\n")

    m = re.search(r"^%%QCM-EXEMPLAIRE-OPEN\s+ncopies=(\d+)", tex, re.MULTILINE)
    if m:
        cfg.num_copies = int(m.group(1))
    cfg.shuffle_questions = "\\melangegroupe" in tex
    cfg.shuffle_answers = "%%QCM-NO-SHUFFLE-ANSWERS" not in tex

    hs = tex.find(_QCM_HEADER_START)
    he = tex.find(_QCM_HEADER_END)
    if hs >= 0 and he > hs:
        cfg.header = _parse_header_tex(tex[hs + len(_QCM_HEADER_START):he])

    as_s = tex.find(_QCM_ANSWER_SHEET_START)
    as_e = tex.find(_QCM_ANSWER_SHEET_END)
    if as_s >= 0 and as_e > as_s:
        as_raw = tex[as_s + len(_QCM_ANSWER_SHEET_START):as_e]
        cfg.answer_sheet = _parse_answer_sheet_tex(as_raw)
        # Si la feuille de réponses ne porte aucun marqueur %%A:, c'est qu'elle
        # vient d'une migration : on la préserve telle quelle.
        if "%%A:" not in as_raw:
            cfg.answer_sheet_tex = as_raw.strip("\n")

    blocks: list[Block] = []

    def _consume_blocks_section(section_start_marker: str,
                                section_end_marker: str) -> None:
        s = tex.find(section_start_marker)
        e = tex.find(section_end_marker)
        if s < 0 or e <= s:
            return
        body_offset = s + len(section_start_marker)
        body = tex[body_offset:e]
        for m in _QCM_BLOCK_RE.finditer(body):
            attrs = _parse_attrs(m.group("attrs"))
            bid = attrs.get("bid", "")
            kind = attrs.get("kind", "")
            if not bid or not kind:
                continue
            end_re = re.compile(r"^%%QCM-END\s+bid=" + re.escape(bid) + r"\s*$",
                                re.MULTILINE)
            em = end_re.search(body, m.end())
            if em is None:
                continue
            block_body = body[m.end():em.start()].strip("\n")
            b = Block(bid=bid, kind=kind,
                      data=_parse_block_body(kind, block_body, attrs))
            # Restaure la trace d'origine si le bloc vient d'une banque
            # (cf. `render_block`). Pas écrasé si déjà présent dans `data`.
            if attrs.get("bank_id") and "_bank_id" not in b.data:
                b.data["_bank_id"] = attrs["bank_id"]
            # Plancher/plafond surchargés par question (sinon hérite du global) :
            # stockés sur l'en-tête %%QCM-BLOCK (comme bank_id), pas dans le corps.
            if kind == "question_qcm":
                for _k in ("floor", "ceiling"):
                    if attrs.get(_k) is not None and _k not in b.data:
                        try:
                            b.data[_k] = float(attrs[_k])
                        except ValueError:
                            pass
            b._start = body_offset + m.start()
            b._end = body_offset + em.end()
            blocks.append(b)

    # Section principale (inline) : tous les kinds, sauf les answerbox `end`.
    _consume_blocks_section(_QCM_BLOCKS_START, _QCM_BLOCKS_END)
    # Section optionnelle (answer-end) : uniquement des answerbox `placement=end`.
    # Vide ou absente sur les sujets pré-existants → no-op (backward-compat).
    _consume_blocks_section(_QCM_ANSWER_END_BLOCKS_START,
                             _QCM_ANSWER_END_BLOCKS_END)
    return {"config": cfg, "blocks": blocks, "mode": "canonical"}


_LEGACY_SECTION_RE = re.compile(
    r"\\(section|subsection|chapter|part)\*?\{([^}]+)\}")


def _legacy_segment_title(seg: str) -> tuple[str | None, str | None]:
    """Devine (level, titre) d'un segment de tex legacy pour l'affichage.

    Priorité : `\\chapter` > `\\section` > `\\subsection` > `\\begin{answerbox}`.
    Retourne `(None, None)` si rien d'évident.
    """
    for level in ("chapter", "section", "subsection", "part"):
        m = re.search(r"\\" + level + r"\*?\{([^}]+)\}", seg)
        if m:
            return level, m.group(1).strip()
    if "\\begin{answerbox}" in seg:
        return "answerbox", "Cadre de réponse manuscrite"
    return None, None


def _split_legacy_tex(tex: str) -> dict | None:
    """Découpe un tex AMC legacy en ses morceaux structurels.

    Retourne `{preamble, body_start, body_end, body_content, answer_sheet_tex,
    num_copies}`, ou None si `\\exemplaire{N}{` est introuvable.

    Le préambule (avant `\\exemplaire`) et la feuille de réponses (depuis le
    dernier `\\newpage` qui précède le 1er marqueur AMC) doivent être conservés
    **verbatim** : ce sont eux qui fixent la géométrie des cases. Les
    régénérer depuis un gabarit change le calage `.xy` et désaligne toutes
    les copies déjà scannées.

    Utilisé à la fois par `_parse_legacy_subject` (lecture/bootstrap) et par
    `migrate_to_canonical` — les deux DOIVENT découper pareil, sinon migrer et
    bootstrapper ne donnent pas le même sujet.
    """
    ex_m = re.search(r"\\exemplaire\{(\d+)\}\{", tex)
    if not ex_m:
        return None
    body_start = ex_m.end()
    depth, j = 1, body_start
    while j < len(tex) and depth > 0:
        if tex[j] == "{":
            depth += 1
        elif tex[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return None
    body = tex[body_start:j]

    # Début de la feuille de réponses : dernier `\newpage` avant le 1er
    # marqueur AMC (`\formulaire`, `\champnom`, `\AMCcodeGridInt`…).
    as_marker = -1
    for marker in ("\\AMCdebutFormulaire", "\\formulaire",
                   "\\champnom", "\\AMCcodeGridInt"):
        i = body.find(marker)
        if i >= 0 and (as_marker < 0 or i < as_marker):
            as_marker = i
    as_start = -1
    if as_marker >= 0:
        np = body.rfind("\\newpage", 0, as_marker)
        as_start = np if np >= 0 else as_marker
    if as_start >= 0:
        body_content = body[:as_start]
        answer_sheet_tex = body[as_start:].strip()
        body_end = body_start + as_start
    else:
        body_content = body
        answer_sheet_tex = ""
        body_end = j
    return {
        "preamble":         tex[:ex_m.start()].rstrip(),
        "body":             body,
        "body_start":       body_start,
        "body_end":         body_end,
        "body_content":     body_content,
        "answer_sheet_tex": answer_sheet_tex,
        "num_copies":       int(ex_m.group(1)),
    }


def _parse_legacy_subject(tex):
    """En mode legacy : expose questions + **tous** les segments intercalaires
    du corps `\\exemplaire{N}{…}` (instructions, sections, énoncés d'exercice,
    `\\begin{answerbox}`, etc.) comme blocs `text` en lecture seule.

    L'objectif est que **tout** ce qui structure le sujet apparaisse dans
    l'outline et la liste centrale, même si l'utilisateur ne peut pas l'éditer
    via l'UI (le canonique débloque l'édition complète après migration).
    """
    cfg = SubjectConfig()
    m = re.search(r"\\AMCrandomseed\{(\d+)\}", tex)
    if m:
        cfg.random_seed = int(m.group(1))
    m = re.search(r"\\exemplaire\{(\d+)\}", tex)
    if m:
        cfg.num_copies = int(m.group(1))

    # Délimiter le corps de \exemplaire pour ne pas attraper le préambule ni
    # la feuille de réponses dans les segments text — et surtout les CONSERVER
    # verbatim : ce sont eux qui fixent la géométrie des cases. Sans ça, une
    # recompilation les régénère depuis le gabarit et change le calage `.xy`,
    # ce qui désaligne toutes les copies déjà scannées.
    split = _split_legacy_tex(tex)
    if split:
        cfg.preamble_tex = split["preamble"]
        cfg.answer_sheet_tex = split["answer_sheet_tex"]
        body_start, body_end = split["body_start"], split["body_end"]
    else:
        body_start, body_end = 0, len(tex)
        for marker in ("\\AMCdebutFormulaire", "\\champnom",
                       "\\AMCcodeGridInt", "\\formulaire"):
            i = tex.find(marker, body_start)
            if i >= 0 and i < body_end:
                body_end = i

    qs = _attach_chars(_parse_legacy_questions(tex), copy=1)
    # Frontières dans le body : questions ET sections/subsections/chapters.
    # Chaque frontière démarre un nouveau bloc qui s'étend jusqu'à la
    # suivante (ou body_end), ce qui donne un bloc distinct par titre de
    # section dans l'outline.
    boundaries: list[dict] = []
    for q in sorted(qs):
        info = qs[q]
        if body_start <= info["block"][0] < body_end:
            boundaries.append({"pos": info["block"][0], "end": info["block"][1],
                                "kind": "qcm", "q": q, "info": info})
    for sm in _LEGACY_SECTION_RE.finditer(tex):
        if not (body_start <= sm.start() < body_end):
            continue
        boundaries.append({"pos": sm.start(), "end": sm.end(),
                            "kind": "section", "level": sm.group(1),
                            "title": sm.group(2).strip()})
    boundaries.sort(key=lambda b: b["pos"])

    blocks: list[Block] = []
    text_idx = 0

    def _emit_text(start_pos: int, end_pos: int,
                    level: str | None = None, title: str | None = None):
        nonlocal text_idx
        seg = tex[start_pos:end_pos].strip()
        if not seg:
            return
        # Saute les segments triviaux (juste \newpage, \vspace, commentaires…).
        meaningful = re.sub(r"%.*", "", seg)
        meaningful = re.sub(r"\\(newpage|vspace|hspace|hrule|smallskip|"
                            r"medskip|bigskip|hfill|noindent)\b\*?(\{[^}]*\})?",
                            "", meaningful)
        if len(meaningful.strip()) < 10 and not title:
            return
        text_idx += 1
        if level is None or title is None:
            lvl, ttl = _legacy_segment_title(seg)
            level = level or lvl or "intercalaire"
            title = title or ttl or _legacy_text_preview(seg)
        blocks.append(Block(
            bid=f"text-legacy-{text_idx}",
            kind="text",
            data={"tex": seg, "readonly": True, "level": level, "title": title},
        ))

    # En-tête du sujet : tout ce qui est avant la 1ère frontière est exposé
    # via `cfg.header.raw_tex` (édité dans le bandeau de réglages), pas comme
    # bloc text intercalaire — ça évite la duplication entre les réglages et
    # la liste centrale.
    if boundaries:
        intro = tex[body_start:boundaries[0]["pos"]].strip()
    else:
        intro = tex[body_start:body_end].strip()
    if intro:
        cfg.header.raw_tex = intro

    for i, b in enumerate(boundaries):
        next_pos = boundaries[i + 1]["pos"] if i + 1 < len(boundaries) else body_end
        if b["kind"] == "qcm":
            info = b["info"]
            data = {
                "tag": info["tag"],
                "qtype": info["type"],
                "env": info["env"],
                "statement": info["statement"],
                "answers": [
                    {"text": a["text"], "correct": a["correct"],
                     "bareme": a.get("points", "0")}
                    for a in info["answers"]
                ],
                "value": info["bareme"].get("value", "1"),
            }
            blk = Block(bid=f"q-legacy-{b['q']}", kind="question_qcm", data=data)
            blk._start, blk._end = info["block"]
            blocks.append(blk)
            # Gap entre la fin de cette question et la prochaine frontière :
            # texte intercalaire (énoncé d'exercice qui suit, instructions, …).
            if b["end"] < next_pos:
                _emit_text(b["end"], next_pos)
        else:
            # Frontière section : DEUX blocs distincts pour bien séparer
            # « le titre de section » du « contenu qui suit » :
            #   1. Bloc text = juste la commande \section{X} (titre seul)
            #   2. Bloc text = contenu jusqu'à la frontière suivante (sans titre forcé)
            _emit_text(b["pos"], b["end"], level=b["level"], title=b["title"])
            if b["end"] < next_pos:
                _emit_text(b["end"], next_pos)

    return {"config": cfg, "blocks": blocks, "mode": "legacy"}


def _legacy_text_preview(seg: str, max_len: int = 60) -> str:
    """Premiers caractères significatifs d'un segment, pour le titre par défaut."""
    s = re.sub(r"%.*", "", seg)
    s = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^}]*\})?", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "Texte LaTeX"
    return s[:max_len] + ("…" if len(s) > max_len else "")


def _parse_tex_subject(tex: str):
    """Parse un tex (canonique OU legacy) → `{config, blocks, mode}`.

    Utilisé uniquement pour le **bootstrap** du store JSON et l'**import** d'un
    .tex existant. N'est plus le chemin de lecture courant (cf. `parse_subject`).
    """
    if is_canonical(tex):
        return _parse_canonical(tex)
    return _parse_legacy_subject(tex)


def parse_subject(tex=None):
    """Parse le sujet entier. Retourne `{config: SubjectConfig, blocks: [Block], mode}`.

    **Source de vérité = `sujet/subject.json`** (store JSON, blocs propres).
    - `tex is None` (cas courant) → lit le store JSON (`_load_subject_store`,
      avec bootstrap depuis `exam.tex` au 1er accès). `mode` = 'canonical' (ou
      'empty' si aucun sujet).
    - `tex` fourni → parse ce tex (import / migration), sans toucher au store.
    """
    if tex is not None:
        return _parse_tex_subject(tex)
    return _load_subject_store()


# --------------------------------------------------------------------------
# Rendu (sérialisation Subject -> tex canonique)
# --------------------------------------------------------------------------

_PREAMBLE_TEMPLATE = (
    "\\documentclass[a4paper]{article}\n"
    "\\usepackage[utf8]{inputenc}\n"
    "\\usepackage[T1]{fontenc}\n"
    "\\usepackage{amsmath, amsfonts, amssymb}\n"
    "\\usepackage{enumitem}\n"
    "\\usepackage[francais,bloc,ensemble,nowatermark]{automultiplechoice}\n"
    "\\usepackage{verbatim,multicol}\n"
    "\\usepackage[most]{tcolorbox}\n"
    "\n"
    "\\newtcolorbox{answerbox}[1][4cm]{\n"
    "  width=\\linewidth, height=#1, colback=white, colframe=black,\n"
    "  arc=0pt, outer arc=0pt, boxrule=0.4pt,\n"
    "  left=4pt, right=4pt, top=2pt, bottom=2pt,\n"
    "}\n"
    "\n"
    "\\def\\R{\\mathbb{R}}\n"
    "\\def\\N{\\mathcal{N}}\n"
    "\\def\\E{\\mathbb{E}}\n"
    "\\def\\P{\\mathbb{P}}\n"
    "\\def\\Z{\\mathbb{Z}}\n"
    "\n"
    "\\begin{document}\n"
    "\\AMCrandomseed{{{SEED}}}\n"
    "\\def\\AMCformQuestion#1{{\\sc Question #1 :}}\n"
)


def render_preamble(cfg: SubjectConfig) -> str:
    """Génère le préambule LaTeX du sujet.

    Si `cfg.preamble_tex` est rempli (sujet migré depuis un legacy), on l'utilise
    tel quel après avoir patché la valeur de `\\AMCrandomseed` (qu'on garde
    pilotable depuis l'UI). Sinon on génère un préambule canonique standard.
    """
    if cfg.preamble_tex.strip():
        # Patche le seed dans le préambule custom (ou laisse intact s'il n'en a pas).
        return re.sub(r"\\AMCrandomseed\{\d+\}",
                      f"\\\\AMCrandomseed{{{cfg.random_seed}}}",
                      cfg.preamble_tex)
    return _PREAMBLE_TEMPLATE.replace("{{SEED}}", str(cfg.random_seed))


def render_header(h: HeaderBlock) -> str:
    """Génère le bloc HEADER : métadonnées commentées + rendu LaTeX visible.

    Priorité :
    1. Si `h.raw_tex` est rempli → on l'utilise tel quel (round-trip via
       marqueurs `%%H:raw-start` / `%%H:raw-end`). C'est le cas par défaut
       pour un sujet migré depuis legacy.
    2. Sinon si tous les champs structurés sont vides → chaîne vide
       (pas de LaTeX parasite qui décalerait le PDF).
    3. Sinon → rendu structuré (tableau + titre + instructions).
    """
    if h.raw_tex.strip():
        return "%%H:raw-start\n" + h.raw_tex.rstrip() + "\n%%H:raw-end"
    all_empty = not any(getattr(h, k, "") for k in
                        ("establishment", "year", "author", "title",
                         "duration", "subtitle", "instructions"))
    if all_empty:
        return ""

    meta = []
    for k in ("establishment", "year", "author", "title", "duration", "subtitle"):
        v = getattr(h, k, "")
        if v:
            meta.append(f"%%H:{k}{{{v}}}")

    parts = list(meta)
    parts.append("")
    parts.append("\\noindent\\begin{tabular}{p{0.5\\linewidth}p{0.5\\linewidth}}")
    parts.append(f"{{\\sc {h.establishment}}} & \\hfill {h.year} \\\\")
    parts.append(f" & \\hfill {h.author}")
    parts.append("\\end{tabular}\n")
    if h.title:
        parts.append("\\begin{center}")
        parts.append(f"\\textbf{{{h.title}}}\\\\")
        if h.duration:
            parts.append(f"{{\\small {h.duration}}}\\\\")
        if h.subtitle:
            parts.append(f"{{\\small \\textit{{{h.subtitle}}}}}")
        parts.append("\\end{center}\n")
    if h.instructions:
        parts.append("%%H:instructions-start")
        parts.append(h.instructions)
        parts.append("%%H:instructions-end")
    return "\n".join(parts)


def _copy_grid_digits(num_copies: int) -> int:
    """Nombre de chiffres requis pour identifier toutes les copies (ceil log10)."""
    if num_copies < 2:
        return 0
    n, d = num_copies, 1
    while n >= 10:
        n //= 10
        d += 1
    return d


def render_answer_sheet(a: AnswerSheetConfig,
                        num_copies: int = 1,
                        custom_tex: str = "",
                        force_columns: int | None = None) -> str:
    """Génère le bloc ANSWER-SHEET canonique.

    Si `custom_tex` est rempli (sujet migré depuis un legacy), on l'utilise tel
    quel : la feuille de réponses originale est préservée intacte. Sinon on
    génère une feuille canonique avec `\\champnom`, `\\AMCcodeGridInt{etu}{N}`
    et `\\formulaire` selon `AnswerSheetConfig`.

    `force_columns` : si non None, écrase `a.columns` (utilisé pour passer en
    1 colonne quand le sujet contient des `\\AMCOpen` dont la ligne de
    notation du correcteur déborderait dans un multicols à 2 colonnes).

    Si `num_copies > 1`, ajoute automatiquement une grille `\\AMCcode{copie}{N}`
    juste avant le code étudiant — c'est elle qui permettra à `cv_grade.detect_copy_id`
    d'identifier la copie scannée. `N = ceil(log10(num_copies+1))` (assez de
    chiffres pour identifier toutes les copies).
    """
    if custom_tex.strip():
        # On préserve la feuille de réponses originale. Si num_copies > 1, on
        # injecte la grille `copie` juste avant `\formulaire` si absente.
        out = custom_tex
        copy_digits = _copy_grid_digits(num_copies)
        if copy_digits > 0 and "\\AMCcode{copie}" not in out:
            inject = (f"\n\\noindent\\textbf{{N\\textsuperscript{{o}} copie :}}"
                      f"\\quad \\AMCcode{{copie}}{{{copy_digits}}}\\hfill\n"
                      f"\\vspace{{0.4cm}}\n")
            # On essaie d'insérer juste avant \formulaire ; sinon en début.
            if "\\formulaire" in out:
                out = out.replace("\\formulaire", inject + "\\formulaire", 1)
            else:
                out = inject + out
        return out
    parts = [
        f"%%A:id_grid_digits{{{a.id_grid_digits}}}",
        f"%%A:name_field{{{1 if a.name_field else 0}}}",
        f"%%A:columns{{{a.columns}}}",
    ]
    if a.extra_instructions:
        parts.append("%%A:extra_instructions-start")
        parts.append(a.extra_instructions)
        parts.append("%%A:extra_instructions-end")
    parts.append("")
    parts.append("\\AMCdebutFormulaire")
    parts.append("")
    # En-tête de la feuille de réponses (titre centré + filet, identique
    # EXAM_2026).
    parts.append("\\vspace{0.5cm}\\hrule\\vspace{0.5cm}")
    parts.append("{\\Large\\bf\\begin{center}{Feuille de réponses}\\end{center}}")
    parts.append("")

    copy_digits = _copy_grid_digits(num_copies)
    if copy_digits > 0:
        # Grille de numéro de copie : tag interne `copie[1..N]`, cases `0..9`.
        # cv_grade.detect_copy_id la repère via ce tag pour identifier la copie.
        parts.append("\\noindent\\textbf{N\\textsuperscript{o} copie :}\\quad "
                     f"\\AMCcode{{copie}}{{{copy_digits}}}\\hfill")
        parts.append("\\vspace{0.4cm}")
        parts.append("")

    # === Identifiants ===
    # Layout EXAM_2026 fidèlement repris : grille code étudiant CENTRÉE avec
    # `\hspace*{\fill}` triplet, à sa DROITE une minipage de 6.5cm qui contient
    # l'instruction « ← codez votre numéro… ↓↓↓ » puis le `\champnom`.
    parts.append("{\\noindent \\large\\bf \\underline{Identifiants} :}\\\\")
    parts.append("")
    if a.name_field:
        parts.append(
            "\\vskip5mm {\\setlength{\\parindent}{0pt}\\hspace*{\\fill}"
            f"\\AMCcodeGridInt{{etu}}{{{a.id_grid_digits}}}"
            "\\hspace*{\\fill}\n"
            "\\begin{minipage}[b]{6.5cm}\n"
            "$\\longleftarrow{}$\\hspace{0pt plus 1cm} codez votre numéro "
            "d'étu\\-diant ci-contre, et inscrivez votre nom et prénom "
            "ci-dessous $\\downarrow\\downarrow\\downarrow$\n"
            "\n"
            "\\vspace{3ex}\n"
            "\n"
            "\\hfill\\champnom{\\fbox{\n"
            "    \\begin{minipage}{.9\\linewidth}\n"
            "      Nom et prénom :\n"
            "      \n"
            "      \\vspace*{.5cm}\\dotfill\n"
            "\n"
            "      \\vspace*{.5cm}\\dotfill\n"
            "      \\vspace*{1mm}\n"
            "    \\end{minipage}\n"
            "  }}\\hfill\\vspace{5ex}\\end{minipage}\\hspace*{\\fill}\n"
            "}"
        )
    else:
        parts.append(
            "\\vskip5mm {\\setlength{\\parindent}{0pt}\\hspace*{\\fill}"
            f"\\AMCcodeGridInt{{etu}}{{{a.id_grid_digits}}}"
            "\\hspace*{\\fill}}"
        )
    parts.append("")
    parts.append("\\vspace{0.7cm}")
    parts.append("")

    # === Réponses ===
    # Rappels par défaut identiques à EXAM_2026 (consignes Tipp-Ex et
    # exclusivité des cases). `extra_instructions` est ajouté en plus si non vide.
    parts.append("{\\noindent \\large\\bf \\underline{Réponses} :}\\\\")
    parts.append("")
    parts.append("{  \\noindent \\bf Les réponses aux questions sont à donner "
                 "exclusivement ci-dessous ; les réponses données ailleurs ne "
                 "seront pas prises en compte.")
    parts.append("}")
    parts.append("")
    parts.append("\\noindent   \\underline{Attention} : les cases doivent être "
                 "noircies et non simplement cochées. En cas d'erreur, utiliser "
                 "du Tipp-Ex pour effacer complètement la case (NE PAS "
                 "redessiner la case effacée).")
    parts.append("")
    if a.extra_instructions.strip():
        parts.append(a.extra_instructions.strip())
        parts.append("")
    parts.append("\\vspace{0.5cm}")
    parts.append("")

    # === Formulaire ===
    # `\formulaire` insère le groupe AMC par défaut « questions ». Les `\AMCOpen`
    # sont volontairement sortis de ce groupe (cf. `render_block`) et placés
    # dans le groupe « open » → ils ne sortiront PAS dans le multicols, mais à
    # pleine largeur via `\insertgroup{open}` juste après. Ainsi les QCM
    # gardent leur mise en page dense en 2 colonnes et les AMCOpen ne
    # débordent plus.
    columns = a.columns if force_columns is None else force_columns
    if columns <= 1:
        parts.append("\\formulaire")
    else:
        parts.append(f"\\begin{{multicols}}{{{columns}}}\\columnseprule=.4pt")
        parts.append("")
        parts.append("\\formulaire")
        parts.append("")
        parts.append("\\end{multicols}")
    # AMCOpen à pleine largeur. On garde le `\insertgroup{open}` derrière un
    # `\ifcsname open@k\endcsname … \fi` car AMC plante (« Missing number,
    # treated as zero ») si on l'insère sur un groupe jamais déclaré (cas du
    # template minimal sans question_open).
    parts.append("")
    parts.append("\\ifcsname open@k\\endcsname\\insertgroup{open}\\fi")
    # Barèmes des answerbox : multicols 2 col (même mise en page que les QCM
    # via `\formulaire`), encadré dans un tcolorbox titré « Réservé
    # correcteur ». Les espacements/font sont resserrés dans chaque question
    # barème (cf. `_render_answerbox_body`). `\AMC@shuffleGfalse` préserve
    # l'ordre R1, R2, R3.
    parts.append("")
    parts.append(
        "\\ifcsname bareme@k\\endcsname\n"
        "\\begin{tcolorbox}[title=R\\'eserv\\'e correcteur, "
        "colback=white, colframe=black!55, fonttitle=\\bfseries\\small, "
        "boxrule=0.5pt, arc=2pt, left=4pt, right=4pt, top=2pt, bottom=2pt, "
        "before skip=8pt, after skip=4pt]\n"
        "\\begin{multicols}{2}\\columnseprule=.4pt\n"
        "{\\csname AMC@shuffleGfalse\\endcsname\\insertgroup{bareme}}\n"
        "\\end{multicols}\n"
        "\\end{tcolorbox}\n"
        "\\fi"
    )
    return "\n".join(parts)


def _render_qcm_body(data: dict) -> str:
    """Régénère le bloc `\\begin{question*}…\\end{...}` canonique."""
    kind = "questionmult" if data.get("qtype") == "mult" else "question"
    env = data.get("env") or "reponses"
    out = [
        f"\\begin{{{kind}}}{{{str(data.get('tag', '')).strip()}}}",
        str(data.get("statement", "")).strip(),
        f"\\begin{{{env}}}",
    ]
    for a in data.get("answers", []):
        text = str(a.get("text", ""))
        cmd = "\\bonne" if a.get("correct") else "\\mauvaise"
        if kind == "questionmult":
            pts = str(a.get("bareme", "0")).strip() or "0"
            if a.get("correct"):
                out.append(f"{cmd}{{{text}}}\\bareme{{b={pts},m=0}}")
            else:
                out.append(f"{cmd}{{{text}}}\\bareme{{b=0,m={pts}}}")
        else:
            out.append(f"{cmd}{{{text}}}")
    out.append(f"\\end{{{env}}}")
    if kind == "question":
        v = str(data.get("value", "1")).strip() or "1"
        fv = _frac(v)
        if fv is not None and fv != 1.0:
            out.append(f"\\bareme{{b={v}}}")
    out.append(f"\\end{{{kind}}}")
    return "\n".join(out)


def _render_freeform_body(data: dict, bid: str = "") -> str:
    """Régénère un AMCOpen pour `question_freeform` + ligne JSON des metadata.

    Différence vs `question_open` : pas de cases de notation manuelle. À la place,
    la note est calculée à partir du HTR (cf. `cv_grade`/`htr.match_answer`). On
    laisse quand même 2 cases binaires (0 / points) dans le `\\AMCOpen` parce
    qu'AMC ne supporte pas un bloc vide. Si la pipeline HTR n'est pas installée,
    le correcteur peut toujours tiquer manuellement la case correspondante sur
    la feuille — et son tick l'emporte (cf. `score.py`).

    La ligne `%%QCM-FREEFORM-DATA <json>` transporte `expected_answer`,
    `match_mode`, `numeric_tol`, `points` à travers le round-trip save/parse,
    sans polluer le PDF (commentaire LaTeX → ignoré à la compilation).
    """
    import json as _json
    statement = str(data.get("statement", "")).strip()
    lines = int(data.get("lines") or 2)
    try:
        points = float(data.get("points", 1.0))
    except (TypeError, ValueError):
        points = 1.0
    meta = {
        "expected_answer": data.get("expected_answer", ""),
        "match_mode": data.get("match_mode", "exact"),
        "numeric_tol": float(data.get("numeric_tol") or 0.01),
        "points": points,
        "tag": data.get("tag", ""),
    }
    meta_line = _FF_DATA_MARKER + _json.dumps(meta, ensure_ascii=False)
    # Marker visible mais minuscule (~1pt) embarqué dans le `question=` : sert à
    # `calibrate_open_zones()` (post-compile) pour localiser la case-réponse via
    # `page.search_for("ffz<bid>")` puis cropper le rectangle juste en dessous.
    # `\makebox[0pt][l]{...}` = boîte zéro-width → ne décale rien dans le layout.
    bid_slug = (bid or meta["tag"] or "anonymous").replace("-", "")
    marker = (r"\makebox[0pt][l]{\color{gray!30}\fontsize{1pt}{1pt}"
              rf"\selectfont\ttfamily ffz{bid_slug}}}")
    statement_with_marker = f"{statement} {marker}"
    open_args = f"question={{{statement_with_marker}}}, lines={lines}"
    scoring = (
        f"  \\wrongchoice[0]{{0}}\\scoring{{0}}\n"
        f"  \\correctchoice[1]{{1}}\\scoring{{{points:g}}}"
    )
    return f"{meta_line}\n\\AMCOpen{{%\n  {open_args}\n}}{{%\n{scoring}\n}}"


def _render_open_body(data: dict) -> str:
    """Régénère un \\AMCOpen{question=…, lines=N}{<grading cases>}.

    `\\AMCOpen` doit être appelé au top-level de `\\exemplaire{}` ; AMC ne
    supporte pas que ses `\\correctchoice/\\wrongchoice` soient à l'intérieur
    d'un `\\begin{minipage}` (erreur LaTeX « missing \\item »). La largeur
    de la ligne de notation dans le formulaire est contrainte côté
    `render_answer_sheet` via `\\rightskip` (mode 1 colonne forcé en cas
    d'AMCOpen).
    """
    statement = str(data.get("statement", "")).strip()
    lines = int(data.get("lines") or 4)
    cases = data.get("grading_cases") or [
        {"label": "0", "value": 0.0},
        {"label": "1", "value": 1.0},
    ]
    open_args = f"question={{{statement}}}, lines={lines}"
    scoring_lines = []
    for c in cases:
        label = str(c.get("label", "")).strip() or "0"
        try:
            value = float(c.get("value", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        cmd = "\\correctchoice" if value > 0 else "\\wrongchoice"
        scoring_lines.append(f"  {cmd}[{label}]{{{label}}}\\scoring{{{value:g}}}")
    scoring = "\n".join(scoring_lines)
    return f"\\AMCOpen{{%\n  {open_args}\n}}{{%\n{scoring}\n}}"


def _render_answerbox_body(data: dict, bid: str = "", qnum: int | None = None) -> str:
    """Régénère un cadre `\\begin{answerbox}` avec titre/instructions optionnels.

    Format identique inline et en fin — c'est `render_subject` qui choisit où
    le placer dans le .tex en fonction de `data['placement']`.

    Si `data['bareme_max'] > 0`, on enregistre une `\\begin{question}` AMC dans
    le groupe `bareme` (via `\\element{bareme}{…}`). Cette question n'apparaît
    PAS inline (le groupe `bareme` est inséré en fin de feuille de réponses via
    `\\insertgroup{bareme}` dans `render_answer_sheet`). Inline, on affiche
    « Question <qnum> » pour que le prof matche la zone d'écriture avec la
    ligne barème de la feuille de réponses.

    ⚠ `qnum` est le **numéro d'ordre du bloc parmi les questions du sujet**,
    calculé par `render_subject`. On n'utilise **plus** `\\ref` sur un `\\label`
    posé dans le groupe barème : la grille « N° copie » qu'injecte
    `render_answer_sheet` dès `num_copies > 1` fait avancer le compteur LaTeX,
    si bien que le `\\ref` se décalait de `num_copies - 1` (mesuré : « Question 5 »
    inline contre « Question 3 » sur la feuille de réponses, pour 3 exemplaires).
    `qnum=None` (questions mélangées, cf. `render_subject`) → aucun numéro n'est
    affiché, seulement le titre.
    """
    height = str(data.get("height") or "5cm").strip() or "5cm"
    title = str(data.get("title") or "").strip()
    instructions = str(data.get("instructions") or "").strip()
    try:
        bareme_max = float(data.get("bareme_max") or 0)
    except (TypeError, ValueError):
        bareme_max = 0
    try:
        bareme_step = float(data.get("bareme_step") or 1)
        if bareme_step <= 0:
            bareme_step = 1
    except (TypeError, ValueError):
        bareme_step = 1

    parts: list[str] = []
    # Inline : « Question N » + titre, N = numéro d'ordre dans le sujet (cf.
    # docstring). Pas de barème, ou numéro inconnu → juste le titre.
    if bareme_max > 0 and bid and qnum:
        head_parts = [f"\\textbf{{Question~{int(qnum)}}}"]
        if title:
            head_parts.append(f"\\textbf{{{title}}}")
        parts.append("\\noindent " + " — ".join(head_parts) + "\\par\\vspace{2pt}")
    elif title:
        parts.append(f"\\noindent\\textbf{{{title}}}\\par\\vspace{{2pt}}")
    if instructions:
        parts.append(instructions)
    parts.append(f"\\begin{{answerbox}}[{height}]\\end{{answerbox}}")
    if bareme_max > 0 and bid:
        choices = []
        # Nombre de cases = (max / step) + 1. Le label affiché dans chaque
        # case est l'INDEX (0, 1, 2, …) — pas le score lui-même. Le score
        # passé à `\scoring{}` est i * step (peut être fractionnaire).
        n_cases = int(round(bareme_max / bareme_step)) + 1
        for i in range(n_cases):
            score = i * bareme_step
            # Formate proprement (entier sans .0, fractionnaire sans zéros).
            if score == int(score):
                score_str = str(int(score))
            else:
                score_str = ("%g" % score)
            cmd = "\\correctchoice" if i > 0 else "\\wrongchoice"
            # `{i}` est la 1re arg de \correctchoice (texte visible, ignoré
            # par notre `\AMCanswer` redéfini) ; `\scoring{score_str}` est la
            # vraie note octroyée si cette case est cochée.
            choices.append(f"  {cmd}{{{i}}}\\scoring{{{score_str}}}")
        # Le libellé sur la feuille de réponses ne contient PAS le titre (qui
        # vit inline avec l'énoncé) — juste « (sur N) » pour le contexte.
        # `\AMC@ordretrue` (via \csname pour contourner le `@` interne) :
        # désactive le mélange des réponses POUR cette question → les cases
        # restent dans l'ordre source 0, 1, …, max. Indispensable côté correcteur.
        # Rendu compact "chiffre dans la case, cases à côté de la question" :
        # - `\AMC@inside@digittrue` (AMC interne) : le chiffre apparaît INSIDE
        #   la case AMC (au lieu de la lettre A,B,C, …).
        # - `\AMCchoiceLabel` redéfini pour soustraire 1 → 0,1,…,N (au lieu de 1..N+1).
        # - `\begin{choicescustom}` + `\AMCanswer` redéfini : cases inline,
        #   pas de `\par\begin{center}` (sinon les cases passent à la ligne).
        # - `\AMC@ordretrue` : pas de mélange des cases (ordre source préservé).
        parts.append(
            "\\element{bareme}{%\n"
            "\\begingroup"
            "\\csname AMC@ordretrue\\endcsname"
            "\\csname AMC@inside@digittrue\\endcsname"
            "\\def\\AMCchoiceLabel##1{\\the\\numexpr\\value{##1}-1\\relax}"
            "\\def\\AMCanswer##1##2{##1\\hspace{0.5em}}\n"
            f"\\begin{{question}}{{bareme-{bid}}}\n"
            f"\\label{{q-{bid}}}%\n"
            "\\begin{choicescustom}\n"
            + "\n".join(choices) + "\n"
            "\\end{choicescustom}\n"
            "\\end{question}\n"
            "\\endgroup\n"
            "}"
        )
    return "\n".join(parts)


def _render_block_body(b: Block, cfg: SubjectConfig | None = None,
                       qnum: int | None = None) -> str:
    if b.kind == "text":
        return b.data.get("tex", "")
    if b.kind == "question_qcm":
        return _render_qcm_body(b.data)
    if b.kind == "question_open":
        return _render_open_body(b.data)
    if b.kind == "question_freeform":
        return _render_freeform_body(b.data, bid=b.bid)
    if b.kind == "answerbox":
        return _render_answerbox_body(b.data, bid=b.bid, qnum=qnum)
    return b.data.get("raw", "")


def render_block(b: Block, cfg: SubjectConfig | None = None,
                 qnum: int | None = None) -> str:
    """Sérialise un Block avec ses marqueurs `%%QCM-BLOCK`/`%%QCM-END`.

    Si `cfg.shuffle_questions`, les questions sont enveloppées dans
    `\\element{questions}{...}` (cf. piège A du plan).

    `qnum` = numéro d'ordre du bloc parmi les questions du sujet, utilisé par
    l'en-tête des `answerbox` (cf. `_render_answerbox_body`).
    """
    attrs = [f"bid={b.bid}", f"kind={b.kind}"]
    if b.kind == "question_qcm":
        attrs.append(f"qtype={b.data.get('qtype', 'single')}")
        if b.data.get("tag"):
            attrs.append(f"tag={b.data['tag']}")
        # Override plancher/plafond par question (uniquement si surchargé).
        for _k in ("floor", "ceiling"):
            v = b.data.get(_k)
            if v is not None and v != "":
                try:
                    attrs.append(f"{_k}={float(v):g}")
                except (TypeError, ValueError):
                    pass
    elif b.kind == "question_open":
        if b.data.get("tag"):
            attrs.append(f"tag={b.data['tag']}")
    elif b.kind == "question_freeform":
        if b.data.get("tag"):
            attrs.append(f"tag={b.data['tag']}")
    elif b.kind == "answerbox":
        # `placement` voyage avec le bloc : la section où il vit dans le .tex
        # (inline vs answer-end) implique déjà placement, mais on l'écrit
        # aussi sur l'en-tête pour un round-trip plus robuste.
        placement = b.data.get("placement", "inline")
        attrs.append(f"placement={placement}")
    # Trace d'origine pour un bloc importé depuis la banque (cf. bank.py).
    # Stocké dans la ligne d'en-tête car le `data` est reconstruit depuis le
    # corps LaTeX au parse — ce qui n'est pas dans le corps disparaît.
    if b.data.get("_bank_id"):
        attrs.append(f"bank_id={b.data['_bank_id']}")
    head = "%%QCM-BLOCK " + " ".join(attrs)
    tail = f"%%QCM-END bid={b.bid}"
    body = _render_block_body(b, cfg, qnum=qnum)
    if cfg and cfg.shuffle_questions and b.kind in (
            "question_qcm", "question_open", "question_freeform"):
        body = "\\element{questions}{\n" + body + "\n}"
    elif b.kind in ("question_open", "question_freeform"):
        # On sort les `\AMCOpen` du groupe par défaut « questions » (que
        # `\formulaire` insère dans le multicols) pour les rendre à pleine
        # largeur sur la feuille de réponses, via `\insertgroup{open}` qui
        # suit le multicols. Les QCM, eux, restent en multicols.
        body = "\\element{open}{\n" + body + "\n}"
    return head + "\n" + body + "\n" + tail


def _is_answer_end_block(b: Block) -> bool:
    """True ssi `b` est un answerbox avec `placement=end` (rendu en fin)."""
    return (b.kind == "answerbox"
            and str(b.data.get("placement", "inline")) == "end")


def render_subject(subject: dict) -> str:
    """Régénère le tex complet canonique à partir d'un Subject.

    Les blocs `answerbox` avec `placement=end` sont rendus dans une section
    séparée `%%QCM-ANSWER-END-BLOCKS-…` placée APRÈS `%%QCM-ANSWER-SHEET-END`
    et AVANT la fermeture de `\\exemplaire`. Tous les autres blocs (et les
    answerbox `placement=inline`) restent dans `%%QCM-BLOCKS-…`.
    """
    cfg: SubjectConfig = subject["config"]
    blocks: list[Block] = subject["blocks"]
    inline_blocks = [b for b in blocks if not _is_answer_end_block(b)]
    end_blocks = [b for b in blocks if _is_answer_end_block(b)]
    # Numéro imprimé dans l'en-tête de chaque `answerbox`, qui doit être celui
    # qu'AMC affiche sur sa ligne de barème (cf. `_render_answerbox_body`).
    #
    # ⚠ Ce n'est PAS l'ordre du document. AMC numérote les questions dans
    # l'ordre où la **feuille de réponses** les assemble :
    #   `\formulaire` (groupe « questions » = les QCM) → `\insertgroup{open}`
    #   → `\insertgroup{bareme}` (les answerbox).
    # Un answerbox placé en tête du sujet reste donc numéroté APRÈS tous les
    # QCM (vérifié : answerbox en 1er → AMC imprime « Question 3 » avec 2 QCM).
    # Les `question_open` / `question_freeform` ne produisent aucune entrée
    # numérotée sur la feuille (vérifié) : ils ne décalent rien.
    #
    # Si les questions sont mélangées, leur ordre change d'un exemplaire à
    # l'autre : aucun numéro fixe ne peut être juste, on n'en met aucun.
    qnums: dict[str, int] = {}
    if not cfg.shuffle_questions:
        n_qcm = sum(1 for b in blocks if b.kind == "question_qcm")
        k = 0
        for b in blocks:
            if b.kind == "answerbox":
                k += 1
                qnums[b.bid] = n_qcm + k
    parts: list[str] = []
    parts.append(_QCM_PREAMBLE_START)
    parts.append(render_preamble(cfg))
    parts.append(_QCM_PREAMBLE_END)
    parts.append("")
    parts.append(f"{_QCM_EXEMPLAIRE_OPEN} ncopies={cfg.num_copies}")
    parts.append(f"\\exemplaire{{{cfg.num_copies}}}{{")
    parts.append("")
    parts.append(_QCM_HEADER_START)
    parts.append(render_header(cfg.header))
    parts.append(_QCM_HEADER_END)
    parts.append("")
    parts.append(_QCM_BLOCKS_START)
    if cfg.shuffle_questions:
        parts.append("\\melangegroupe{questions}")
    for b in inline_blocks:
        parts.append(render_block(b, cfg, qnum=qnums.get(b.bid)))
        # Ligne vide entre 2 blocs LaTeX = paragraphe break → préserve
        # l'espacement vertical du sujet original. Sans ça, les marqueurs
        # `%%QCM-…` mangent les lignes vides entre blocs et les cases
        # remontent légèrement dans le PDF (cf. calage avant/après migration).
        parts.append("")
    if cfg.shuffle_questions:
        parts.append("\\restituegroupe{questions}")
    parts.append(_QCM_BLOCKS_END)
    parts.append("")
    parts.append("\\newpage")
    parts.append(_QCM_ANSWER_SHEET_START)
    # Les `\AMCOpen` sont sortis du multicols : ils vivent dans le groupe AMC
    # « open » que `render_answer_sheet` insère APRÈS `\formulaire` à pleine
    # largeur. Du coup le multicols à 2 colonnes reste valable pour les QCM
    # quel que soit le contenu du sujet.
    parts.append(render_answer_sheet(cfg.answer_sheet, num_copies=cfg.num_copies,
                                      custom_tex=cfg.answer_sheet_tex))
    parts.append(_QCM_ANSWER_SHEET_END)
    if end_blocks:
        parts.append("")
        parts.append(_QCM_ANSWER_END_BLOCKS_START)
        for b in end_blocks:
            parts.append(render_block(b, cfg, qnum=qnums.get(b.bid)))
            parts.append("")
        parts.append(_QCM_ANSWER_END_BLOCKS_END)
    parts.append("}")
    parts.append(_QCM_EXEMPLAIRE_CLOSE)
    parts.append("\\end{document}")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# CRUD canonique (haut niveau, lit/écrit exam.tex)
# --------------------------------------------------------------------------

# RLock (réentrant) : le CRUD prend le lock puis appelle `parse_subject()` →
# `_load_subject_store()` qui reprend le même lock dans le même thread.
_io_lock = RLock()


def _save_subject(subject: dict) -> None:
    """Persiste le sujet dans le store JSON (source de vérité) + invalide les caches.

    **N'écrit PLUS le .tex** : `exam.tex` est régénéré uniquement par
    `compile_pdf()`. Tout le CRUD (add/update/delete/move/… + config/header/…)
    passe par ici → « Sauvegarder » ne touche jamais au LaTeX.
    """
    save_subject_store(subject)


def _invalidate_caches() -> None:
    global _charmap_by_copy, _qmap_by_copy, _regions
    _cache["mtime"] = None
    _charmap_by_copy = {}
    _qmap_by_copy = {}
    _regions = None


def _require_canonical(subject: dict) -> None:
    if subject.get("mode") != "canonical":
        raise PermissionError(
            "Le sujet est en mode legacy. Pour utiliser le CRUD complet "
            "(insertion/suppression/réorganisation), il faut d'abord migrer "
            "le sujet vers le format canonique."
        )


def _default_block_data(kind: str, data: dict | None) -> dict:
    if data:
        return data
    if kind == "text":
        return {"tex": "Texte libre — édite ce contenu en LaTeX."}
    if kind == "question_qcm":
        return {
            "tag": f"q_{secrets.token_hex(2)}",
            "qtype": "single",
            "env": "reponses",
            "statement": "Énoncé de la question…",
            "answers": [
                {"text": "Réponse A", "correct": False, "bareme": "0"},
                {"text": "Réponse B (bonne)", "correct": True, "bareme": "1"},
                {"text": "Réponse C", "correct": False, "bareme": "0"},
            ],
            "value": "1",
        }
    if kind == "question_open":
        return {
            "tag": f"o_{secrets.token_hex(2)}",
            "statement": "Énoncé de la question ouverte…",
            "lines": 4,
            "points": 2.0,
            "grading_cases": [
                {"label": "0", "value": 0.0},
                {"label": "1", "value": 1.0},
                {"label": "2", "value": 2.0},
            ],
        }
    if kind == "question_freeform":
        # Question à réponse libre AUTO-GRADÉE via HTR (cf. `htr.py`).
        # Le PDF imprime un \AMCOpen comme une question ouverte classique, mais
        # la note vient de la lecture HTR + match contre `expected_answer`.
        # Sans le module `[htr]` installé, la question reste correctible à la
        # main (les 2 cases binaires 0/points sont là).
        return {
            "tag": f"f_{secrets.token_hex(2)}",
            "statement": "Calcule 6 × 7 :",
            "expected_answer": "42",
            "match_mode": "exact",  # exact|numeric_tol|contains|regex
            "numeric_tol": 0.01,    # ignoré si match_mode != numeric_tol
            "lines": 2,
            "points": 1.0,
        }
    if kind == "answerbox":
        # Cadre `\begin{answerbox}` (correction manuelle, pas de cases AMC).
        # Placement : "inline" = dans le flux des questions ; "end" = sur la
        # feuille de réponses, après `\formulaire`. cf. `render_subject`.
        # `bareme_max` > 0 : ajoute une grille AMC dans le groupe `bareme`
        # (insérée à la fin de la feuille de réponses, avec les QCM). Le prof
        # coche une note 0..bareme_max. Tag « bareme-<bid> » détectable a
        # posteriori via `sujet/exam.xy` (cv_grade).
        # `bareme_step` (défaut 1) : pas du barème. step=0.25 + max=1 →
        # 5 cases avec scores 0, 0.25, 0.5, 0.75, 1. Le label affiché dans
        # la case reste l'index 0, 1, 2, 3, 4 (séquentiel).
        # Défaut = 3 → barème /3 entier, suffisant pour les questions ouvertes
        # courtes ; 0 désactive complètement la grille.
        return {
            "height": "5cm",
            "placement": "inline",
            "title": "",
            "instructions": "",
            "bareme_max": 3,
            "bareme_step": 1,
        }
    return {}


def add_block(kind: str, after_bid: str | None = None,
              data: dict | None = None, bid: str | None = None) -> str:
    """Insère un bloc après `after_bid` (None = en fin). Retourne le bid créé.

    `bid` permet de **réutiliser un identifiant** : c'est ce qui rend
    l'annulation d'une suppression exacte. Sans lui, le bloc restauré recevrait
    un identifiant neuf — or le bid est gravé dans le calage compilé
    (`bareme-<bid>` d'un answerbox, marqueur `ffz<bid>` d'une question libre) :
    l'aperçu et le HTR perdraient le lien jusqu'à la recompilation suivante.
    Ignoré si l'identifiant est déjà pris.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind invalide: {kind}")
    if kind in DISABLED_KINDS:
        raise ValueError(_DISABLED_MSG.get(kind, f"kind désactivé: {kind}"))
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        taken = {b.bid for b in subject["blocks"]}
        bid = bid if (bid and bid not in taken) else _gen_bid(kind)
        block = Block(bid=bid, kind=kind, data=_default_block_data(kind, data))
        blocks = subject["blocks"]
        if not after_bid:
            blocks.append(block)
        else:
            idx = next((i for i, b in enumerate(blocks) if b.bid == after_bid), None)
            if idx is None:
                blocks.append(block)
            else:
                blocks.insert(idx + 1, block)
        _save_subject(subject)
        return bid


def delete_block(bid: str) -> None:
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        subject["blocks"] = [b for b in subject["blocks"] if b.bid != bid]
        _save_subject(subject)


def move_block(bid: str, after_bid: str | None) -> None:
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        blocks = subject["blocks"]
        idx = next((i for i, b in enumerate(blocks) if b.bid == bid), None)
        if idx is None:
            raise KeyError(bid)
        moved = blocks.pop(idx)
        if not after_bid:
            blocks.insert(0, moved)
        else:
            target = next((i for i, b in enumerate(blocks) if b.bid == after_bid), None)
            if target is None:
                blocks.append(moved)
            else:
                blocks.insert(target + 1, moved)
        _save_subject(subject)


def update_block(bid: str, data: dict) -> None:
    """Met à jour le `data` d'un bloc. Autorisé aussi en mode legacy pour
    les blocs question_qcm (délégué à `save_questions`)."""
    with _io_lock:
        subject = parse_subject()
        if subject.get("mode") == "legacy":
            idx = next((i for i, b in enumerate(subject["blocks"]) if b.bid == bid), None)
            if idx is None:
                raise KeyError(bid)
            q = idx + 1
            upd = {
                "q": q,
                "tag": data.get("tag", ""),
                "type": data.get("qtype", "single"),
                "env": data.get("env", "reponses"),
                "statement": data.get("statement", ""),
                "answers": data.get("answers", []),
                "value": data.get("value", "1"),
            }
            save_questions([upd])
            return
        block = next((b for b in subject["blocks"] if b.bid == bid), None)
        if block is None:
            raise KeyError(bid)
        block.data = data
        _save_subject(subject)


def duplicate_block(bid: str) -> str:
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        idx = next((i for i, b in enumerate(subject["blocks"]) if b.bid == bid), None)
        if idx is None:
            raise KeyError(bid)
        src = subject["blocks"][idx]
        clone = Block(bid=_gen_bid(src.kind), kind=src.kind,
                      data=_copy.deepcopy(src.data))
        subject["blocks"].insert(idx + 1, clone)
        _save_subject(subject)
        return clone.bid


def update_config(patch: dict) -> None:
    """Patche SubjectConfig (num_copies, random_seed, shuffle_*).

    En mode legacy : édite directement les commandes existantes dans le tex
    via regex (utile pour activer la randomisation sur un sujet legacy).
    """
    with _io_lock:
        subject = parse_subject()
        if subject.get("mode") == "legacy":
            tex = EXAM_TEX.read_text(encoding="utf-8")
            changed = False
            if "num_copies" in patch:
                n = int(patch["num_copies"])
                tex2, sub = re.subn(r"\\exemplaire\{\d+\}",
                                    f"\\\\exemplaire{{{n}}}", tex)
                if sub:
                    tex, changed = tex2, True
            if "random_seed" in patch:
                s = int(patch["random_seed"])
                tex2, sub = re.subn(r"\\AMCrandomseed\{\d+\}",
                                    f"\\\\AMCrandomseed{{{s}}}", tex)
                if sub:
                    tex, changed = tex2, True
            if changed:
                EXAM_TEX.write_text(tex, encoding="utf-8")
                _invalidate_caches()
            return
        cfg = subject["config"]
        for k, v in patch.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        _save_subject(subject)


def update_header(patch: dict) -> None:
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        h = subject["config"].header
        for k, v in patch.items():
            if hasattr(h, k):
                setattr(h, k, v)
        _save_subject(subject)


def update_answer_sheet(patch: dict) -> None:
    """Patch les champs de `AnswerSheetConfig` (id_grid_digits, name_field,
    columns, extra_instructions) ; accepte aussi `answer_sheet_tex` (LaTeX
    brut prioritaire sur les champs structurés).

    Vider `answer_sheet_tex` (chaîne vide après strip) réactive la génération
    canonique à partir des champs structurés.
    """
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        cfg = subject["config"]
        a = cfg.answer_sheet
        for k, v in patch.items():
            if k == "answer_sheet_tex":
                cfg.answer_sheet_tex = str(v or "").strip("\n")
            elif hasattr(a, k):
                setattr(a, k, v)
        _save_subject(subject)


def regenerate_seed() -> int:
    """Pose un nouveau seed aléatoire à 8 chiffres et persiste."""
    seed = random.randint(10_000_000, 99_999_999)
    update_config({"random_seed": seed})
    return seed


# --------------------------------------------------------------------------
# Migration legacy → canonique (« danger zone »)
# --------------------------------------------------------------------------

def migrate_to_canonical() -> dict:
    """Migre un sujet legacy (sans marqueurs `%%QCM-`) vers le format canonique.

    Stratégie **non destructive** :
    - le préambule original (du début à `\\exemplaire{N}{` exclu) est préservé
      dans `SubjectConfig.preamble_tex` ;
    - la feuille de réponses (depuis le dernier `\\newpage` du body) est
      préservée dans `SubjectConfig.answer_sheet_tex` ;
    - chaque `\\begin{question*}…\\end{...}` devient un bloc `question_qcm` ;
    - le texte libre entre questions devient des blocs `text` (préserve les
      `\\section*{…}`, instructions, etc.) ;
    - un backup du tex original est écrit dans `sujet/exam.tex.legacy-backup`.

    Retourne `{ok, log}`. Si le sujet est déjà canonique : no-op.
    """
    if not EXAM_TEX.exists():
        return {"ok": False, "log": "sujet/exam.tex introuvable."}
    with _io_lock:
        tex = EXAM_TEX.read_text(encoding="utf-8")
        if is_canonical(tex):
            return {"ok": True, "log": "Le sujet est déjà au format canonique."}

        # Backup avant toute modif (irréversible côté UI sans restore manuel).
        backup = EXAM_TEX.with_suffix(".tex.legacy-backup")
        backup.write_text(tex, encoding="utf-8")

        # 1-2. Découpe structurelle : préambule / corps / feuille de réponses.
        # Même helper que `_parse_legacy_subject` — migrer et bootstrapper le
        # store doivent produire exactement le même sujet.
        # (`post`, après \exemplaire, vaut \end{document} : non conservé,
        # `render_subject` le réécrit canoniquement.)
        split = _split_legacy_tex(tex)
        if split is None:
            return {"ok": False, "log": "`\\exemplaire{N}{` introuvable ou mal "
                    "fermé — structure de sujet AMC inattendue."}
        num_copies = split["num_copies"]
        preamble = split["preamble"]
        body = split["body"]
        body_content = split["body_content"]
        answer_sheet_tex = split["answer_sheet_tex"]

        # 3. Découper le body en frontières {questions, \section, \subsection,
        # \chapter}. Chaque section devient son propre bloc text éditable, ce
        # qui donne la même granularité que `_parse_legacy_subject`.
        boundaries: list[dict] = []
        for m in _Q_BEGIN.finditer(body_content):
            end = body_content.find("\\end{%s}" % m.group(1), m.end())
            if end == -1:
                continue
            try:
                info = _parse_block(body_content[m.end():end], m.group(1),
                                     m.group(2).strip())
            except ValueError:
                continue
            boundaries.append({"pos": m.start(),
                                "end": end + len("\\end{%s}" % m.group(1)),
                                "kind": "qcm", "info": info})
        for sm in _LEGACY_SECTION_RE.finditer(body_content):
            boundaries.append({"pos": sm.start(), "end": sm.end(),
                                "kind": "section", "level": sm.group(1),
                                "title": sm.group(2).strip()})
        boundaries.sort(key=lambda b: b["pos"])

        blocks: list[Block] = []

        def _add_text(start_p: int, end_p: int):
            seg = body_content[start_p:end_p].strip()
            if not seg:
                return
            meaningful = re.sub(r"%.*", "", seg)
            meaningful = re.sub(r"\\(newpage|vspace|hspace|hrule|smallskip|"
                                 r"medskip|bigskip|hfill|noindent)\b\*?(\{[^}]*\})?",
                                 "", meaningful)
            if len(meaningful.strip()) < 10:
                return
            blocks.append(Block(bid=_gen_bid("text"), kind="text",
                                data={"tex": seg}))

        # Intro (avant la 1ère frontière) → cfg.header.raw_tex, pas un bloc text.
        if boundaries:
            intro = body_content[:boundaries[0]["pos"]].strip()
        else:
            intro = body_content.strip()
        intro_header = intro

        for i, b in enumerate(boundaries):
            next_pos = (boundaries[i + 1]["pos"] if i + 1 < len(boundaries)
                        else len(body_content))
            if b["kind"] == "qcm":
                info = b["info"]
                data = {
                    "tag": info["tag"],
                    "qtype": info["type"],
                    "env": info["env"],
                    "statement": info["statement"].strip(),
                    "answers": [
                        {"text": a["text"], "correct": a["correct"],
                         "bareme": a.get("points", "0")}
                        for a in info["answers"]
                    ],
                    "value": info["bareme"].get("value", "1"),
                }
                blocks.append(Block(bid=_gen_bid("question_qcm"),
                                     kind="question_qcm", data=data))
                if b["end"] < next_pos:
                    _add_text(b["end"], next_pos)
            else:
                # Section : 2 blocs distincts (titre seul + contenu).
                _add_text(b["pos"], b["end"])
                if b["end"] < next_pos:
                    _add_text(b["end"], next_pos)

        # 4. Construire le Subject canonique.
        seed_m = re.search(r"\\AMCrandomseed\{(\d+)\}", preamble)
        random_seed = int(seed_m.group(1)) if seed_m else DEFAULT_SEED
        cfg = SubjectConfig(
            num_copies=num_copies,
            random_seed=random_seed,
            shuffle_answers=True,
            shuffle_questions="\\melangegroupe" in body,
            header=HeaderBlock(raw_tex=intro_header),  # en-tête legacy → champ raw
            answer_sheet=AnswerSheetConfig(),
            preamble_tex=preamble,
            answer_sheet_tex=answer_sheet_tex,
        )
        subject = {"config": cfg, "blocks": blocks, "mode": "canonical"}
        _save_subject(subject)

        return {
            "ok": True,
            "log": (f"Migration réussie : {len(blocks)} blocs créés "
                    f"({sum(1 for b in blocks if b.kind == 'question_qcm')} QCM, "
                    f"{sum(1 for b in blocks if b.kind == 'text')} texte). "
                    f"Backup : {backup.name}"),
            "n_blocks": len(blocks),
            "backup": backup.name,
        }


# --------------------------------------------------------------------------
# Compat : parse_tex() (lecture indexée par ordre)
# --------------------------------------------------------------------------

_cache = {"mtime": None, "questions": {}}


def parse_tex():
    """Compat : `{q: {tag, type, env, statement, answers:[{...,char}], bareme, block}}`.

    Dérivé de `parse_subject()` (store JSON) pour mutualiser le parsing. Seuls
    les blocs `question_qcm` sont exposés (les `text`/`question_open` n'ont pas
    de notion de "q" indexé). Cache keyé sur le mtime de `subject.json`.
    """
    # Clé de cache = mtime du store JSON (source de vérité). Si le store n'existe
    # pas encore mais qu'un exam.tex est présent, parse_subject() le bootstrappe ;
    # on prend alors le mtime de l'exam.tex en repli pour amorcer le cache.
    if SUBJECT_JSON.exists():
        mt = SUBJECT_JSON.stat().st_mtime
    elif EXAM_TEX.exists():
        mt = EXAM_TEX.stat().st_mtime
    else:
        return {}
    if _cache["mtime"] != mt:
        subject = parse_subject()
        qs: dict[int, dict] = {}
        q = 0
        for b in subject["blocks"]:
            if b.kind != "question_qcm":
                continue
            q += 1
            d = b.data
            answers = [
                {"text": a["text"], "correct": a["correct"], "char": None,
                 "points": a.get("bareme", "0")}
                for a in d.get("answers", [])
            ]
            info = {
                "tag": d.get("tag", ""),
                "type": d.get("qtype", "single"),
                "env": d.get("env", "reponses"),
                "statement": d.get("statement", ""),
                "answers": answers,
                "bareme": ({"value": d.get("value", "1")}
                           if d.get("qtype") == "single" else {}),
                "floor": d.get("floor"),
                "ceiling": d.get("ceiling"),
                "block": (b._start, b._end),
            }
            qs[q] = info
        _attach_chars(qs, copy=1)
        _cache["questions"] = qs
        _cache["mtime"] = mt
    return _cache["questions"]


# --------------------------------------------------------------------------
# Spécification d'une question (lue par score.py et l'UI)
# --------------------------------------------------------------------------

def effective_spec(q: int, copy: int = 1):
    """`{type, options, correct, tag, is_open?, open_values?}` pour la copie.

    Pour un `question_qcm` : `options` = concat triée des lettres,
    `correct` = concat triée des lettres correctes.

    Pour un `question_open` (uniquement accessible aux blocs canoniques,
    indexé par sa position parmi les questions QCM exposées dans `parse_tex`) :
    `is_open=True`, `open_values={lettre: valeur}`, `correct` = vide.
    """
    # Défaut vide : une question absente du sujet n'a ni options ni bonnes
    # réponses. Aucun repli sur une clé de correction externe — elle
    # appartiendrait à un autre examen (cf. suppression d'answer_key.py).
    spec = {"type": "single", "options": "", "correct": "", "tag": ""}
    try:
        info = parse_tex().get(q)
    except Exception:
        info = None
    if not info:
        return spec
    spec["type"] = info["type"]
    spec["tag"] = info["tag"]
    answers = info["answers"]
    charmap = _tex_chars(copy=copy).get(q)
    if charmap and len(charmap) == len(answers):
        spec["options"] = "".join(sorted(charmap))
        spec["correct"] = "".join(sorted(c for a, c in zip(answers, charmap)
                                         if a["correct"]))
    elif answers and all(a.get("char") for a in answers):
        spec["options"] = "".join(sorted(a["char"] for a in answers))
        spec["correct"] = "".join(sorted(a["char"] for a in answers if a["correct"]))
    return spec


def get_bareme(copy: int = 1):
    """`{q -> {value: float} (single) | {chars: {char: points}} (mult)}` pour la copie."""
    try:
        qs = parse_tex()
    except Exception:
        return {}
    out = {}
    for q, info in qs.items():
        if info["type"] == "single":
            v = _frac(info["bareme"].get("value"))
            out[q] = {"value": 1.0 if v is None else v}
            continue
        charmap = _tex_chars(copy=copy).get(q)
        chars, ok = {}, True
        if charmap and len(charmap) == len(info["answers"]):
            for a, ch in zip(info["answers"], charmap):
                pts = _frac(a.get("points"))
                if pts is None:
                    ok = False
                    break
                chars[ch] = pts
        else:
            for a in info["answers"]:
                ch, pts = a.get("char"), _frac(a.get("points"))
                if ch is None or pts is None:
                    ok = False
                    break
                chars[ch] = pts
        out[q] = {"chars": chars} if (ok and chars) else {}
    return out


def max_score(q: int, copy: int = 1) -> float:
    """Score maximal d'une question avec le barème courant (copie donnée)."""
    try:
        info = parse_tex().get(q)
    except Exception:
        info = None
    if info is None:
        return 0.0        # question inconnue du sujet → aucun point
    if info["type"] == "single":
        v = _frac(info["bareme"].get("value"))
        return 1.0 if v is None else float(v)
    tot = 0.0
    for a in info["answers"]:
        if a["correct"]:
            p = _frac(a.get("points"))
            tot += 0.0 if p is None else p
    return round(tot, 6)


def total_max(copy: int = 1) -> float:
    """Total maximal du QCM (copie donnée)."""
    return round(sum(max_score(q, copy=copy) for q in parse_tex()) or 0.0, 6)


# --------------------------------------------------------------------------
# Édition (compat) — save_questions : réécriture in-place des blocs question
# --------------------------------------------------------------------------

def apply_edits(tex, spans):
    """Applique des remplacements `(start, end, new_text)` (insertion = start==end)."""
    for start, end, new in sorted(spans, key=lambda s: s[0], reverse=True):
        tex = tex[:start] + new + tex[end:]
    return tex


def _render_block(upd):
    """Compat : rendu d'un bloc question pour `save_questions`.

    `upd` : {tag, type, env, statement, answers:[{text, correct, bareme}], value}.
    """
    return _render_qcm_body({
        "tag": upd.get("tag", ""),
        "qtype": upd.get("type", "single"),
        "env": upd.get("env") or "reponses",
        "statement": upd.get("statement", ""),
        "answers": [
            {"text": a.get("text", ""), "correct": a.get("correct"),
             "bareme": a.get("bareme", "0")}
            for a in upd.get("answers", [])
        ],
        "value": upd.get("value", "1"),
    })


def save_questions(updates):
    """Compat : met à jour le `data` des blocs question (par index `q`) dans le
    store JSON. Ne touche pas au .tex.

    `updates` : [{q, tag, type, env, statement, answers:[{text, correct, bareme}], value}].
    `q` = numéro (1-based) du QCM dans l'ordre du document.
    """
    with _io_lock:
        subject = parse_subject()
        qcm_blocks = [b for b in subject["blocks"] if b.kind == "question_qcm"]
        for upd in updates:
            q = int(upd["q"])
            if q < 1 or q > len(qcm_blocks):
                raise KeyError(q)
            b = qcm_blocks[q - 1]
            # Préserve les clés non éditées (floor/ceiling/_bank_id…), patche le reste.
            b.data.update({
                "tag": upd.get("tag", b.data.get("tag", "")),
                "qtype": "mult" if upd.get("type") == "mult" else "single",
                "env": "reponseshoriz" if upd.get("env") == "reponseshoriz" else "reponses",
                "statement": upd.get("statement", ""),
                "answers": [{"text": str(a.get("text", "")),
                             "correct": bool(a.get("correct")),
                             "bareme": str(a.get("bareme", ""))}
                            for a in upd.get("answers", [])],
                "value": str(upd.get("value", "1")) or "1",
            })
        save_subject_store(subject)


# --------------------------------------------------------------------------
# Régions PDF (aperçu au survol)
# --------------------------------------------------------------------------

_regions = None


def _pdf_region_hints():
    """Indices tirés du PDF compilé pour resserrer les régions de question.

    Retourne `{"lines": {page: [(y0, y1, size)]}, "body": {page: size},
    "rects": {page: [(y0, y1, header_text)]}}` en **px 300 dpi** (repère du
    calage), ou `{}` si le PDF est illisible.

    - `lines` : lignes de texte, triées, avec la taille de police max de la
      ligne — c'est ce qui distingue un titre de section du corps de texte.
    - `rects` : rectangles pleine largeur (les cadres `answerbox`), avec le
      texte de la ligne qui les précède (leur en-tête).
    """
    if not SUJET_PDF.exists():
        return {}
    try:
        import fitz
    except Exception:
        return {}
    S = 300.0 / 72.0
    out = {"lines": {}, "body": {}, "rects": {}}
    try:
        doc = fitz.open(str(SUJET_PDF))
    except Exception:
        return {}
    try:
        for i in range(doc.page_count):
            page = doc[i]
            n = i + 1
            lines = []
            try:
                for blk in page.get_text("dict")["blocks"]:
                    for ln in blk.get("lines", []):
                        spans = ln.get("spans", [])
                        if not spans:
                            continue
                        txt = "".join(sp.get("text", "") for sp in spans).strip()
                        if not txt:
                            continue
                        size = max(sp.get("size", 0.0) for sp in spans)
                        lines.append((ln["bbox"][1] * S, ln["bbox"][3] * S, size, txt))
            except Exception:
                pass
            lines.sort(key=lambda t: (t[0], t[1]))
            out["lines"][n] = lines
            if lines:
                sizes = sorted(l[2] for l in lines)
                out["body"][n] = sizes[len(sizes) // 2]

            # Cadres `answerbox` : rectangles nettement plus larges que hauts,
            # occupant la largeur du texte. Le `\champnom` de la feuille de
            # réponses est dans une minipage étroite, il ne passe pas le filtre.
            rects, seen = [], set()
            try:
                pw = float(page.rect.width) * S
                for dr in page.get_drawings():
                    r = dr.get("rect")
                    if r is None:
                        continue
                    w, h = r.width * S, r.height * S
                    if w < 0.60 * pw or h < 150.0:
                        continue
                    key = (round(r.y0 * S), round(r.y1 * S))
                    if key in seen:
                        continue
                    seen.add(key)
                    y0, y1 = r.y0 * S, r.y1 * S
                    # En-tête = ligne de texte juste au-dessus du cadre.
                    head = ""
                    for ly0, ly1, _sz, txt in lines:
                        if ly1 <= y0 + 2 and y0 - ly1 < 160:
                            head = txt
                    rects.append((y0, y1, head))
            except Exception:
                pass
            rects.sort()
            out["rects"][n] = rects
    finally:
        doc.close()
    return out


def _statement_top(lines, body_size, y_boxes):
    """Haut de l'énoncé d'une question dont les cases commencent à `y_boxes`.

    Remonte ligne à ligne depuis la dernière ligne au-dessus des cases et
    s'arrête au premier **saut de paragraphe** ou au premier **titre** (police
    sensiblement plus grande). Sans ça, la région de la 1re question d'une page
    part du haut de la feuille et avale l'en-tête et le titre du sujet.
    """
    above = [l for l in lines if l[1] <= y_boxes + 2.0]
    if not above:
        return None
    i = len(above) - 1
    top = above[i][0]
    while i > 0:
        cur, prev = above[i], above[i - 1]
        line_h = max(1.0, cur[1] - cur[0])
        if cur[0] - prev[1] > 1.15 * line_h:     # saut de paragraphe
            break
        if body_size and prev[2] > 1.15 * body_size:   # titre de section
            break
        i -= 1
        top = above[i][0]
    return top


def pdf_regions():
    """{q: {page, x0, y0, x1, y1}} — région de chaque question dans le PDF du
    sujet, en pixels 300 dpi.

    Base = les cases du calage (`layout_box`), affinée avec le texte du PDF :
    - le haut d'une région est le haut de l'**énoncé** (cf. `_statement_top`),
      pas la fin de la question précédente — sinon la 1re question d'une page
      englobe l'en-tête et le titre ;
    - le bas s'arrête avant l'énoncé de la question suivante ;
    - pour un bloc `answerbox`, la région devient le **cadre de réponse inline**
      (celui où l'étudiant écrit) et non la ligne de barème « Réservé
      correcteur » de la feuille de réponses, qui est ce que désigne son
      numéro AMC.

    Note : les questions QCM/AMCOpen ont leurs cases sur les pages d'énoncé,
    mais les barèmes des answerbox (`tag = bareme-<bid>`) ont leurs cases sur
    la feuille de réponses. On INCLUT donc la feuille de réponses uniquement
    pour les questions dont le tag commence par `bareme-` (sinon les cases du
    code étudiant — `etu[i]` — pollueraient les régions).
    """
    global _regions
    if _regions is not None:
        return _regions
    _regions = {}
    try:
        lay = layout_store.get_layout()
    except Exception:
        return _regions
    asp = lay.answer_sheet_page

    def _is_bareme(q: int) -> bool:
        return lay._question_name(q).startswith("bareme-")

    rows = [(b.question, b.page, b.ymin, b.ymax)
            for b in lay.boxes
            if b.page != asp or _is_bareme(b.question)]
    pages = {p: (pi.width, pi.height) for p, pi in lay.pages.items()}
    boxes = {}
    for q, page, ymin, ymax in rows:
        d = boxes.setdefault(q, {"page": page, "ymin": ymin, "ymax": ymax})
        d["ymin"] = min(d["ymin"], ymin)
        d["ymax"] = max(d["ymax"], ymax)
    by_page = {}
    for q, d in boxes.items():
        by_page.setdefault(d["page"], []).append((q, d))

    hints = _pdf_region_hints()
    TOP, PAD, LEAD = 240.0, 70.0, 14.0
    for page, items in by_page.items():
        items.sort(key=lambda it: it[1]["ymin"])
        w, h = pages.get(page, (2480.0, 3508.0))
        lines = hints.get("lines", {}).get(page, [])
        body = hints.get("body", {}).get(page, 0.0)

        # Haut de l'énoncé de chaque question de la page (None si indisponible).
        tops = {}
        for q, d in items:
            if _is_bareme(q) or not lines:
                continue
            t = _statement_top(lines, body, d["ymin"])
            if t is not None and t < d["ymin"]:
                tops[q] = t

        prev_bottom = TOP
        for idx, (q, d) in enumerate(items):
            if _is_bareme(q):
                # Barème : on ne veut PAS hériter du `prev_bottom` (cas QCM
                # avec énoncé ; ici il n'y a rien d'utile au-dessus). On serre
                # la région autour de la ligne du barème elle-même.
                y0 = max(0.0, d["ymin"] - 60.0)
                y1 = min(h, d["ymax"] + 60.0)
            else:
                if q in tops:
                    y0 = max(0.0, tops[q] - LEAD)
                else:
                    y0 = max(0.0, min(prev_bottom, d["ymin"]) - 20.0)
                y1 = min(h, d["ymax"] + PAD)
                # Ne pas mordre sur l'énoncé de la question suivante.
                for nq, _nd in items[idx + 1:]:
                    if nq in tops:
                        y1 = min(y1, max(y0 + 20.0, tops[nq] - LEAD - 4.0))
                        break
            _regions[q] = {"page": page, "x0": 110.0, "y0": y0,
                           "x1": w - 110.0, "y1": y1}
            prev_bottom = d["ymax"] + PAD

    _apply_answerbox_regions(_regions, lay, hints, pages)

    # Blocs `text` : aucune case dans le calage, on les retrouve dans le PDF
    # par le texte (cf. `_apply_text_block_regions`). Indexés par bid.
    try:
        blocks = parse_subject()["blocks"]
        qmap = amc_question_map()
        # bid → clé de région, pour les blocs déjà localisés.
        qcm_num = {q_tex: num for num, q_tex in (qmap.get("qcm") or {}).items()}
        open_num = {bid: num for num, bid in (qmap.get("open") or {}).items()}
        seq = {"n": 0}

        def key_of(b):
            if b.kind == "question_qcm":
                seq["n"] += 1
                return qcm_num.get(seq["n"])
            if b.kind in _OPEN_KINDS:
                return open_num.get(b.bid)
            return None

        _apply_text_block_regions(_regions, lay, hints, pages, blocks, key_of)
    except Exception:
        pass
    return _regions


def _plain_words(tex: str) -> list[str]:
    """Mots visibles d'un fragment LaTeX, pour le retrouver dans le PDF.

    Retire commentaires, commandes et leurs options, puis la ponctuation de
    balisage. `\\section*{Questions}` → `["questions"]`.
    """
    t = re.sub(r"(?<!\\)%.*", " ", tex)
    t = re.sub(r"\\[a-zA-Z@]+\*?", " ", t)          # commandes
    t = re.sub(r"\[[^\]]{0,40}\]", " ", t)            # options [..]
    t = re.sub(r"[{}$&~^_\\|#]", " ", t)
    words = re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]{3,}", t)
    seen, out = set(), []
    for w in words:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            out.append(wl)
    return out


def _apply_text_block_regions(regions, lay, hints, pages, blocks, key_of):
    """Localise les blocs `text` dans le PDF, pour qu'ils aient eux aussi un
    cadre dans l'aperçu.

    Un bloc de texte libre n'a **aucune case** dans le calage : rien ne dit où
    il atterrit. On procède en deux temps :

    1. **Bande de recherche** — le bloc est forcément entre la région du bloc
       localisé qui le précède et celle du bloc localisé qui le suit (ordre du
       document). Ça borne la recherche à quelques centimètres de PDF.
    2. **Appariement par le texte** — on compare les mots du LaTeX aux lignes
       du PDF dans cette bande (`hints['lines']`), et on garde la suite de
       lignes contiguës qui correspond le mieux.

    ⚠ L'étape 2 est nécessaire : la bande du 1er bloc part du haut de la page
    et contient l'en-tête de l'examen. Et elle ne suffit pas seule — pour
    `\\section*{Questions}`, le mot « questions » apparaît aussi dans les
    consignes de l'en-tête. On départage par la **proximité au bloc suivant**
    (un intertitre introduit ce qui vient après).

    Les régions des blocs texte sont indexées par **bid** (chaîne), pas par
    numéro AMC — d'où les clés mixtes int/str de `pdf_regions()`.
    """
    lines_by_page = hints.get("lines") or {}
    if not lines_by_page or not pages:
        return
    page_nums = sorted(pages)
    TOP = 240.0

    def bounds(key):
        r = regions.get(key)
        return None if not r else (r["page"], r["y0"], r["y1"])

    located = [(i, key_of(b)) for i, b in enumerate(blocks)]
    located = [(i, k) for i, k in located if k is not None and k in regions]

    for i, b in enumerate(blocks):
        if b.kind != "text":
            continue
        words = _plain_words(b.data.get("tex", ""))
        if not words:
            continue
        prev = next((bounds(k) for j, k in reversed(located) if j < i), None)
        nxt = next((bounds(k) for j, k in located if j > i), None)
        if prev:
            p0, y0 = prev[0], prev[2]
        else:
            p0, y0 = page_nums[0], TOP
        if nxt:
            p1, y1 = nxt[0], nxt[1]
        else:
            p1 = page_nums[-1]
            y1 = pages.get(p1, (2480.0, 3508.0))[1]

        # Lignes du PDF dans la bande.
        cand = []
        for pg in page_nums:
            if pg < p0 or pg > p1:
                continue
            for ly0, ly1, _sz, txt in lines_by_page.get(pg, []):
                if pg == p0 and ly1 <= y0:
                    continue
                if pg == p1 and ly0 >= y1:
                    continue
                low = txt.lower()
                hit = sum(1 for w in words if w in low)
                cand.append((pg, ly0, ly1, hit))
        if not cand:
            continue

        # Suites de lignes contiguës qui correspondent.
        need = 1 if len(words) <= 2 else max(1, len(words) // 4)
        runs, cur = [], []
        for pg, ly0, ly1, hit in cand:
            if hit >= need:
                if cur and (cur[-1][0] != pg
                            or ly0 - cur[-1][2] > 2.5 * max(1.0, ly1 - ly0)):
                    runs.append(cur); cur = []
                cur.append((pg, ly0, ly1, hit))
            elif cur:
                runs.append(cur); cur = []
        if cur:
            runs.append(cur)
        if not runs:
            continue

        # Départage : proximité au bloc suivant (un intertitre introduit ce qui
        # suit), à défaut au précédent ; à égalité, le plus de mots trouvés.
        if nxt:
            anchor_pg, anchor_y = nxt[0], nxt[1]
        elif prev:
            anchor_pg, anchor_y = prev[0], prev[2]
        else:
            anchor_pg, anchor_y = p1, y1

        def dist(run):
            pg = run[-1][0]
            edge = run[-1][2] if pg <= anchor_pg else run[0][1]
            return (abs(pg - anchor_pg) * 1e6 + abs(edge - anchor_y),
                    -sum(r[3] for r in run))

        best = min(runs, key=dist)
        pg = best[0][0]
        w, h = pages.get(pg, (2480.0, 3508.0))
        top = max(0.0, min(r[1] for r in best) - 12.0)
        bot = min(h, max(r[2] for r in best) + 12.0)
        regions[b.bid] = {"page": pg, "x0": 110.0, "y0": top,
                          "x1": w - 110.0, "y1": bot}


def _apply_answerbox_regions(regions, lay, hints, pages):
    """Recentre la région des blocs `answerbox` sur leur cadre de réponse.

    Un `answerbox` n'a pas de cases là où l'étudiant écrit : ses seules cases
    sont la ligne de barème « Réservé correcteur », sur la feuille de réponses.
    Son numéro AMC pointe donc vers l'évaluation, alors que ce qu'on veut
    montrer c'est le cadre où l'étudiant répond. On le retrouve dans le PDF :
    c'est un rectangle pleine largeur, précédé de sa ligne d'en-tête.

    Appariement rect ↔ bloc par le **titre** présent dans l'en-tête ; repli sur
    l'ordre du document pour les blocs sans titre. Les `answerbox` en
    `placement=end` sont laissés tels quels (leur cadre est sur la feuille de
    réponses, au milieu d'autres encadrés : appariement non fiable).
    """
    if not hints.get("rects"):
        return
    try:
        blocks = [b for b in parse_subject()["blocks"] if b.kind == "answerbox"]
    except Exception:
        return
    inline = [b for b in blocks
              if (b.data.get("placement") or "inline") != "end"]
    if not inline:
        return
    # bid → numéro AMC de son barème (c'est la clé sous laquelle l'UI
    # référence le bloc, via `preview_q`).
    q_of_bid = {}
    for q, name in lay.question_names.items():
        if name.startswith("bareme-"):
            q_of_bid[name[len("bareme-"):]] = q

    asp = lay.answer_sheet_page
    cands = []
    for page in sorted(hints["rects"]):
        if page == asp or page not in pages:
            continue
        for y0, y1, head in hints["rects"][page]:
            cands.append((page, y0, y1, head))
    if not cands:
        return

    used = set()
    assigned = {}
    for b in inline:                       # 1re passe : appariement par titre
        title = str(b.data.get("title") or "").strip()
        if not title:
            continue
        for i, (page, y0, y1, head) in enumerate(cands):
            if i not in used and title and title in head:
                assigned[b.bid] = i
                used.add(i)
                break
    free = [i for i in range(len(cands)) if i not in used]
    for b in inline:                       # 2e passe : ordre du document
        if b.bid in assigned or not free:
            continue
        assigned[b.bid] = free.pop(0)

    for bid, i in assigned.items():
        q = q_of_bid.get(bid)
        if q is None:
            continue
        page, y0, y1, _head = cands[i]
        w, h = pages.get(page, (2480.0, 3508.0))
        # Inclure l'en-tête du cadre : même remontée ligne à ligne que pour un
        # énoncé de QCM (s'arrête au saut de paragraphe, donc à la fin de la
        # question précédente).
        lines = hints["lines"].get(page, [])
        top = _statement_top(lines, hints.get("body", {}).get(page, 0.0), y0)
        if top is None or top > y0:
            top = y0
        top = max(0.0, top - 14.0)
        bot = min(h, y1 + 20.0)
        regions[q] = {"page": page, "x0": 110.0, "y0": top,
                      "x1": w - 110.0, "y1": bot}
        # Les régions voisines ont été calculées sans connaître ce cadre :
        # on les empêche de mordre dessus.
        for oq, r in regions.items():
            if oq != q and r["page"] == page and r["y0"] < top < r["y1"]:
                r["y1"] = max(r["y0"] + 20.0, top - 4.0)


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------

_compile_lock = Lock()


def _error_tail(log, n=50):
    """Extrait la partie pertinente d'un log LaTeX (autour de la 1re erreur)."""
    lines = log.splitlines()
    for idx, ln in enumerate(lines):
        if ln.startswith("!"):
            return "\n".join(lines[max(0, idx - 2):idx + 25])
    return "\n".join(lines[-n:])


def _ffbid_slug(bid: str) -> str:
    """Slug du `bid` utilisé dans le marker visible (sans tirets)."""
    return (bid or "").replace("-", "")


def calibrate_open_zones() -> dict:
    """Localise les cases-réponse de chaque `question_freeform` dans le PDF compilé.

    Appelée après `compile_pdf()` réussi. Pour chaque bloc freeform, cherche son
    marker `ffz<bid>` (rendu en 1pt invisible dans le `question=` de l'`\\AMCOpen`),
    en déduit la bounding box de la zone d'écriture (rectangle large sous le
    marker, hauteur `lines × line_height`).

    Écrit `sujet/open_zones.json` ({bid: {page, xmin, ymin, xmax, ymax, dpi}})
    qui sera consommé au grade time par `cv_grade.grade_image`. Coords PDF
    (dpi=72) — la transformation vers 300dpi se fait au moment du crop via le
    ratio (300/72) puisque mires et page partagent la même géométrie canonique.

    Bloc sans marker trouvé → entrée absente (HTR sautera la question avec
    warning au grade time). Aucun bloc freeform → fichier supprimé.
    """
    import json as _json
    if not SUJET_PDF.exists():
        return {}
    try:
        sub = parse_subject()
    except Exception:
        return {}
    ff_blocks = [b for b in sub["blocks"] if b.kind == "question_freeform"]
    if not ff_blocks:
        if OPEN_ZONES_JSON.exists():
            OPEN_ZONES_JSON.unlink()
        return {}

    import fitz
    PT_PER_LINE = 24.0   # ~0.85cm/ligne, défaut AMC \AMCOpen
    PT_MARGIN_X = 56.0   # ~2cm de marge horizontale
    PT_GAP_BELOW_MARKER = 2.0

    zones: dict = {}
    doc = fitz.open(str(SUJET_PDF))
    try:
        for b in ff_blocks:
            marker = f"ffz{_ffbid_slug(b.bid)}"
            found = None
            for page_idx in range(doc.page_count):
                rects = doc[page_idx].search_for(marker)
                if rects:
                    found = (page_idx, rects[0])
                    break
            if found is None:
                continue
            page_idx, mr = found
            page = doc[page_idx]
            try:
                lines = int(b.data.get("lines") or 2)
            except (TypeError, ValueError):
                lines = 2
            y_top = float(mr.y1) + PT_GAP_BELOW_MARKER
            y_bot = y_top + lines * PT_PER_LINE
            x_left = PT_MARGIN_X
            x_right = float(page.rect.x1) - PT_MARGIN_X
            zones[b.bid] = {
                "page": page_idx + 1,
                "xmin": x_left, "ymin": y_top,
                "xmax": x_right, "ymax": y_bot,
                "dpi": 72,
                "tag": b.data.get("tag", ""),
                "expected_answer": b.data.get("expected_answer", ""),
                "match_mode": b.data.get("match_mode", "exact"),
                "numeric_tol": float(b.data.get("numeric_tol") or 0.01),
                "points": float(b.data.get("points") or 1.0),
                "lines": lines,
            }
    finally:
        doc.close()

    if zones:
        OPEN_ZONES_JSON.write_text(
            _json.dumps(zones, indent=2, ensure_ascii=False), encoding="utf-8")
    elif OPEN_ZONES_JSON.exists():
        OPEN_ZONES_JSON.unlink()
    return zones


def _fmt_signed(x, force_plus=False):
    """Nombre formaté pour le mode mathématique LaTeX, virgule française.

    `force_plus` ajoute un `+` explicite aux valeurs positives (borne haute).
    Ex. -0.25 → "-0{,}25" ; 1 (force_plus) → "+1" ; 0 → "0".
    """
    x = float(x)
    a = abs(x)
    if abs(a - round(a)) < 1e-9:
        s = str(int(round(a)))
    else:
        s = ("%g" % a).replace(".", "{,}")
    if x < 0:
        return "-" + s
    return ("+" if force_plus else "") + s


def _qcm_natural_bounds(data: dict) -> tuple:
    """(min, max) naturels d'un QCM = Σ barème faux (≤0), Σ barème correct (≥0)."""
    hi = sum((_frac(str(a.get("bareme", "0"))) or 0.0)
             for a in data.get("answers", []) if a.get("correct"))
    lo = sum((_frac(str(a.get("bareme", "0"))) or 0.0)
             for a in data.get("answers", []) if not a.get("correct"))
    return round(lo, 6), round(hi, 6)


BAREME_EX_START = "% --- AMCx:bareme-exemples (généré, ne pas éditer cette zone) ---"
BAREME_EX_END = "% --- AMCx:bareme-exemples-fin ---"


def _fr_latex(fr) -> str:
    """Fraction LaTeX : entier → '1' ; fraction → '\\tfrac{a}{b}' (signe devant)."""
    from fractions import Fraction
    fr = Fraction(fr)
    if fr.denominator == 1:
        return str(fr.numerator)
    sign = "-" if fr < 0 else ""
    return f"{sign}\\tfrac{{{abs(fr.numerator)}}}{{{abs(fr.denominator)}}}"


def _fr_dec(fr) -> str:
    """Décimal français à 2 décimales (virgule) : Fraction(1,6) → '0{,}17'."""
    v = round(float(fr), 2)
    s = ("%g" % v) if abs(v - round(v)) > 1e-9 else str(int(round(v)))
    return s.replace(".", "{,}").replace("-", "-")


def render_bareme_examples(subject: dict, floor=None, ceiling=None, n: int = 2) -> str:
    """Génère un bloc LaTeX « Barème » (explication + N exemples chiffrés) dérivé
    des structures de barème réellement présentes dans le sujet, avec scores
    clampés à [floor, ceiling] (cohérent avec `score.score_question`).

    Bloc encadré par `BAREME_EX_START`/`BAREME_EX_END` → ré-génération idempotente.
    """
    from fractions import Fraction
    from collections import Counter

    def F(x):
        try:
            return Fraction(str(x))
        except (ValueError, ZeroDivisionError, TypeError):
            return Fraction(0)

    # Recense les structures (n_bonnes, n_mauvaises, b, m) par fréquence.
    structs = Counter()
    for b in subject.get("blocks", []):
        if b.kind != "question_qcm":
            continue
        ans = b.data.get("answers", [])
        good = [F(a.get("bareme", 0)) for a in ans if a.get("correct")]
        bad = [F(a.get("bareme", 0)) for a in ans if not a.get("correct")]
        if not good or not bad:
            continue
        bval = good[0]                 # barème d'une bonne (b > 0)
        mval = -bad[0]                 # |barème d'une mauvaise| (m > 0)
        structs[(len(good), len(bad), bval, mval)] += 1

    def clamp(x):
        if floor is not None and x < Fraction(F(floor)):
            return Fraction(F(floor)), "min"
        if ceiling is not None and x > Fraction(F(ceiling)):
            return Fraction(F(ceiling)), "max"
        return x, None

    lines = [BAREME_EX_START]
    # Explication générale.
    flo = _fr_latex(F(floor)) if floor is not None else None
    cei = _fr_latex(F(ceiling)) if ceiling is not None else None
    lines.append(r"\textbf{Barème.} Chaque question est à choix multiple. Le score "
                 r"d'une question vaut")
    lines.append(r"\[ \text{score}(Q) = \sum_{\text{cases cochées}} "
                 r"\begin{cases} +b & \text{si bonne réponse,} \\ "
                 r"-m & \text{si mauvaise réponse,} \end{cases} \]")
    lines.append(r"avec $b = 1/(\text{nb. de bonnes réponses})$ et "
                 r"$m = 1/(\text{nb. de mauvaises réponses})$. Une case non cochée "
                 r"ne rapporte ni ne coûte rien.")
    if floor is not None and ceiling is not None:
        lines.append(f"La note d'une question est ensuite ramenée dans "
                     f"l'intervalle $[{flo},\\, {cei}]$ pt "
                     f"(sauf indication contraire à côté de l'énoncé).")
    elif floor is not None:
        lines.append(f"La note d'une question ne peut pas descendre sous ${flo}$ pt.")
    elif ceiling is not None:
        lines.append(f"La note d'une question est plafonnée à ${cei}$ pt.")

    def approx_or_exact(x):
        """'= 1' / '= 0' si simple ; sinon '\\approx 0{,}17'."""
        x = Fraction(x)
        return f"= {x.numerator}" if x.denominator == 1 else f"\\approx {_fr_dec(x)}"

    # N exemples sur les structures les plus fréquentes.
    for k, ((ng, nb, bval, mval), _cnt) in enumerate(structs.most_common(n), 1):
        smax, smax_tag = clamp(ng * bval)               # toutes les bonnes
        smin, smin_tag = clamp(-nb * mval)              # toutes les mauvaises
        smix, _ = clamp(bval - mval)                    # 1 bonne + 1 mauvaise
        lbl_b = f"${ng}$ bonnes" if ng > 1 else "$1$ bonne"
        lbl_m = f"${nb}$ mauvaises" if nb > 1 else "$1$ mauvaise"
        lines.append("")  # ligne vide = saut de paragraphe (pour que \smallskip agisse)
        lines.append(r"\smallskip\noindent\textbf{Exemple " + str(k) + "} — question à "
                     f"{lbl_b} et {lbl_m}, donc "
                     f"$b = {_fr_latex(bval)}$ et $m = {_fr_latex(mval)}$.")
        lines.append(r"\begin{itemize}\setlength\itemsep{-2pt}")
        # toutes les bonnes → maximum
        raw_hi = (f"${ng} \\times {_fr_latex(bval)} = {_fr_latex(ng*bval)}$"
                  if ng > 1 else f"${_fr_latex(ng*bval)}$")
        cocher_b = "Cocher les bonnes" if ng > 1 else "Cocher la bonne"
        if smax_tag == "max":
            lines.append(f"\\item {cocher_b} : {raw_hi}, ramené à "
                         f"${_fr_latex(smax)}$ pt \\textbf{{(maximum)}}.")
        else:
            lines.append(f"\\item {cocher_b} : {raw_hi} pt \\textbf{{(maximum)}}.")
        # 1 bonne + 1 mauvaise
        lines.append(f"\\item Cocher $1$ bonne et $1$ mauvaise : "
                     f"${_fr_latex(bval)} - {_fr_latex(mval)} {approx_or_exact(smix)}$ pt.")
        # toutes les mauvaises → minimum
        raw_lo = (f"$-{nb} \\times {_fr_latex(mval)} = {_fr_latex(-nb*mval)}$"
                  if nb > 1 else f"$-{_fr_latex(mval)}$")
        cocher_m = "Cocher les mauvaises" if nb > 1 else "Cocher la mauvaise"
        if smin_tag == "min":
            lines.append(f"\\item {cocher_m} : {raw_lo}, ramené à "
                         f"${_fr_latex(smin)}$ pt \\textbf{{(minimum)}}.")
        else:
            lines.append(f"\\item {cocher_m} : {raw_lo} pt \\textbf{{(minimum)}}.")
        lines.append(r"\item Ne rien cocher : $0$ pt.")
        lines.append(r"\end{itemize}")

    lines.append(BAREME_EX_END)
    return "\n".join(lines)


def _inject_score_ranges(tex: str, g_floor=None, g_ceiling=None) -> str:
    """Insère sous chaque énoncé QCM une ligne italique « entre LO et HI pt ».

    Bornes par question : override (`data.floor/ceiling`) sinon défaut global
    sinon borne naturelle du barème. Transformation appliquée au compile-time
    uniquement (exam.tex source jamais modifié). No-op hors mode canonique.
    """
    try:
        sub = parse_subject(tex)
    except Exception:
        return tex
    if sub.get("mode") != "canonical":
        return tex
    edits = []
    for b in sub["blocks"]:
        if b.kind != "question_qcm":
            continue
        nat_lo, nat_hi = _qcm_natural_bounds(b.data)
        lo = b.data.get("floor")
        hi = b.data.get("ceiling")
        if lo is None:
            lo = g_floor
        if hi is None:
            hi = g_ceiling
        if lo is None:
            lo = nat_lo
        if hi is None:
            hi = nat_hi
        seg = tex[b._start:b._end]
        m = re.search(r"\\begin\{reponses", seg)
        if not m:
            continue
        pos = b._start + m.start()
        # Petit saut de ligne APRÈS l'énoncé, puis l'indication en italique sur
        # sa propre ligne, EN DESSOUS de la question (avant les réponses).
        note = ("\\par\\smallskip{\\small\\itshape Note de cette question : entre $"
                + _fmt_signed(lo) + "$ et $" + _fmt_signed(hi, force_plus=True)
                + "$ pt.\\par}\\smallskip\n")
        edits.append((pos, pos, note))
    return apply_edits(tex, edits)


def compile_pdf():
    """Compile `exam.tex` → `sujet/DOC-sujet.pdf`. Retourne {'ok': bool, 'log': str}.

    Compilation dans un dossier temporaire (sujet/ reste propre) ; le PDF n'est
    copié qu'en cas de succès — un échec laisse l'ancien DOC-sujet.pdf intact.

    En mode canonique, `exam.tex` est d'abord régénéré depuis le store (seul
    endroit qui écrit le .tex), avec un backup `exam.tex.bak`. En mode legacy
    le .tex est compilé **tel quel** : le régénérer changerait le calage.
    """
    global _regions
    with _compile_lock:
        # Régénère exam.tex depuis le store JSON (source de vérité). C'est le
        # SEUL endroit qui écrit le .tex : « Compiler » = mettre à jour le tex
        # puis produire le PDF. « Sauvegarder » ne touche jamais au .tex.
        subject = parse_subject()
        if subject.get("mode") == "empty" or not subject.get("blocks"):
            if not EXAM_TEX.exists():
                return {"ok": False, "log": "Aucun sujet à compiler (subject.json/exam.tex absents)."}
        elif subject.get("mode") == "legacy":
            # Sujet legacy : on compile le .tex tel quel, sans le régénérer.
            # Le régénérer changerait le calage `.xy` et désalignerait les
            # copies déjà scannées. Pour l'éditer, il faut migrer explicitement
            # (bouton « Migrer vers le format canonique »).
            if not EXAM_TEX.exists():
                return {"ok": False, "log": "sujet/exam.tex introuvable."}
        else:
            try:
                SUJET_DIR.mkdir(parents=True, exist_ok=True)
                new_tex = render_subject(subject)
                # Backup avant réécriture : c'est le seul endroit qui écrase le
                # .tex, et l'utilisateur a pu l'éditer à la main entre-temps.
                if EXAM_TEX.exists():
                    old_tex = EXAM_TEX.read_text(encoding="utf-8")
                    if old_tex != new_tex:
                        EXAM_TEX.with_suffix(".tex.bak").write_text(
                            old_tex, encoding="utf-8")
                EXAM_TEX.write_text(new_tex, encoding="utf-8")
            except OSError as e:
                return {"ok": False, "log": f"Écriture exam.tex impossible : {e}"}
        if not EXAM_TEX.exists():
            return {"ok": False, "log": "sujet/exam.tex introuvable."}
        tmp = Path(tempfile.mkdtemp(prefix="amc_compile_"))
        try:
            # Affichage optionnel de la fourchette de note sous chaque QCM :
            # transformation appliquée à la copie temporaire seulement.
            _show_range = False
            _gf = _gc = None
            try:
                import config as _config
                _cfg = _config.load_config()
                _show_range = bool(_cfg.get("show_score_range"))
                _gf = _cfg.get("question_floor")
                _gc = _cfg.get("question_ceiling")
            except Exception:
                pass
            if _show_range:
                _tex = _inject_score_ranges(EXAM_TEX.read_text(encoding="utf-8"),
                                            g_floor=_gf, g_ceiling=_gc)
                (tmp / "exam.tex").write_text(_tex, encoding="utf-8")
            else:
                shutil.copy(EXAM_TEX, tmp / "exam.tex")
            # `exam-config.tex` est lu par automultiplechoice.sty (\jobname-config) :
            # définir \SujetExterne active le mode « calibration » → pdflatex écrit
            # le fichier de calage exam.xy (positions des cases) en plus du PDF.
            (tmp / "exam-config.tex").write_text(
                "\\def\\SujetExterne{1}\n", encoding="utf-8")
            # Style AMC vendorisé, copié à côté d'exam.tex : pdflatex cherche le
            # répertoire courant en premier, donc cette version fait foi même si
            # le système a une installation AMC. Déterminisme du calage voulu.
            if AMC_STY.exists():
                shutil.copy(AMC_STY, tmp / AMC_STY.name)
            rc, out = 1, ""
            for _ in range(2):
                try:
                    proc = subprocess.run(
                        ["pdflatex", "-interaction=nonstopmode",
                         "-halt-on-error", "exam.tex"],
                        cwd=str(tmp), capture_output=True, text=True, timeout=120)
                except FileNotFoundError:
                    return {"ok": False,
                            "log": "pdflatex introuvable (TeX Live non installé ?)."}
                except subprocess.TimeoutExpired:
                    return {"ok": False, "log": "Délai de compilation dépassé (120 s)."}
                rc, out = proc.returncode, proc.stdout
                if rc != 0:
                    break
            pdf = tmp / "exam.pdf"
            if rc == 0 and pdf.exists():
                shutil.copy(pdf, SUJET_PDF)
                # Le `.xy` (calage) produit par pdflatex devient la source de
                # géométrie du pipeline (cf. layout_store) — on le conserve.
                xy = tmp / "exam.xy"
                if xy.exists():
                    shutil.copy(xy, EXAM_XY)
                    layout_store.invalidate_cache()
                    _invalidate_caches()
                    # Calibre les cases freeform (HTR) en arrière-plan logique :
                    # parse PDF + sujet, écrit sujet/open_zones.json. Pas
                    # bloquant si aucun bloc freeform (no-op rapide).
                    ff_log = ""
                    try:
                        zones = calibrate_open_zones()
                        if zones:
                            ff_log = f" · {len(zones)} case(s) freeform calibrée(s)"
                    except Exception as e:  # noqa: BLE001
                        ff_log = f" · calibration freeform KO: {e}"
                    return {"ok": True,
                            "log": "Compilation réussie — PDF et calage (.xy) mis à jour" + ff_log + "."}
                _regions = None
                return {"ok": True,
                        "log": "Compilation réussie — PDF mis à jour "
                               "(⚠ aucun fichier .xy produit : sujet non-AMC ?)."}
            logf = tmp / "exam.log"
            full = logf.read_text(errors="replace") if logf.exists() else out
            return {"ok": False, "log": _error_tail(full)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sub = parse_subject()
    cfg = sub["config"]
    print(f"Mode : {sub['mode']}")
    print(f"Copies : {cfg.num_copies}   Seed : {cfg.random_seed}   "
          f"shuffle_answers={cfg.shuffle_answers}  shuffle_questions={cfg.shuffle_questions}")
    print(f"{len(sub['blocks'])} blocs parsés depuis {EXAM_TEX}\n")
    qs = parse_tex()
    for q in sorted(qs):
        info = qs[q]
        if info["type"] == "mult":
            extra = "  [" + " ".join("%s=%s" % (a.get("char") or "?", a["points"])
                                     for a in info["answers"]) + "]"
        else:
            extra = "  value=" + info["bareme"].get("value", "1")
        print(f"  Q{q:2d} [{info['type']:6s}] {info['tag']:28s} "
              f"{info['env']:14s} {len(info['answers'])} rép.{extra}")
    print(f"\nTotal barème : {total_max()}")
