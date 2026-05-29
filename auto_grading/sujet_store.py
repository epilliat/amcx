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
import random
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import layout_store

try:                                  # repli structurel — absent sur un projet vierge
    from answer_key import ANSWER_KEY
except Exception:
    ANSWER_KEY = {}

# SUJET_DIR vit dans le projet actif (cf. config.project_root()).
# Calculé à l'import (donc figé au démarrage du process) — un switch de projet
# implique un restart Flask, donc SUJET_DIR sera recalculé proprement.
from config import project_root as _project_root  # noqa: E402

ROOT = _project_root()
SUJET_DIR = ROOT / "sujet"
EXAM_TEX = SUJET_DIR / "exam.tex"
SUJET_PDF = SUJET_DIR / "DOC-sujet.pdf"
EXAM_XY = SUJET_DIR / "exam.xy"
OPEN_ZONES_JSON = SUJET_DIR / "open_zones.json"  # géométrie des cases freeform (HTR)

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
    """Sérialise un subject (résultat de `parse_subject`) en dict JSON."""
    return {
        "config": _config_to_dict(subject["config"]),
        "blocks": [_block_to_dict(b) for b in subject["blocks"]],
        "mode": subject.get("mode", "empty"),
    }


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
            r"(?:\\textbf\{Question~?\\ref\{q-[^}]+\}\}(?:\s*[—-]+\s*)?)?"  # « Question \ref{} » + séparateur
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
    # la feuille de réponses dans les segments text.
    ex_m = re.search(r"\\exemplaire\{\d+\}\{", tex)
    body_start = ex_m.end() if ex_m else 0
    body_end = len(tex)
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


def parse_subject(tex=None):
    """Parse le sujet entier. Retourne `{config: SubjectConfig, blocks: [Block], mode}`.

    `mode` ∈ {'canonical', 'legacy', 'empty'} :
    - **canonical** : marqueurs `%%QCM-…` présents, CRUD libre, round-trip garanti.
    - **legacy** : tex écrit à la main (EXAM_2026), seuls les blocs `question_qcm`
      sont exposés et seul `update_block`/`save_questions` est autorisé.
    - **empty** : fichier inexistant.
    """
    if tex is None:
        if not EXAM_TEX.exists():
            return {"config": SubjectConfig(), "blocks": [], "mode": "empty"}
        tex = EXAM_TEX.read_text(encoding="utf-8")
    if is_canonical(tex):
        return _parse_canonical(tex)
    return _parse_legacy_subject(tex)


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


def _render_answerbox_body(data: dict, bid: str = "") -> str:
    """Régénère un cadre `\\begin{answerbox}` avec titre/instructions optionnels.

    Format identique inline et en fin — c'est `render_subject` qui choisit où
    le placer dans le .tex en fonction de `data['placement']`.

    Si `data['bareme_max'] > 0`, on enregistre une `\\begin{question}` AMC dans
    le groupe `bareme` (via `\\element{bareme}{…}`) avec un `\\label{q-<bid>}`.
    Cette question n'apparaît PAS inline (le groupe `bareme` est inséré en
    fin de feuille de réponses via `\\insertgroup{bareme}` dans
    `render_answer_sheet`). Inline, on affiche « Question \\ref{q-<bid>} »
    pour que le prof matche la zone d'écriture inline avec la ligne barème
    sur la feuille de réponses.
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
    # Inline : « Question N » + titre. Le numéro N est récupéré via `\ref`
    # à un `\label` posé dans la grille barème (résolu en 2 passes pdflatex).
    # Pas de barème → juste le titre.
    if bareme_max > 0 and bid:
        head_parts = [f"\\textbf{{Question~\\ref{{q-{bid}}}}}"]
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
        # `\label{q-<bid>}` après `\begin{question}` capture le numéro AMC
        # de cette question : `\ref{q-<bid>}` inline le restitue (2 passes).
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


def _render_block_body(b: Block, cfg: SubjectConfig | None = None) -> str:
    if b.kind == "text":
        return b.data.get("tex", "")
    if b.kind == "question_qcm":
        return _render_qcm_body(b.data)
    if b.kind == "question_open":
        return _render_open_body(b.data)
    if b.kind == "question_freeform":
        return _render_freeform_body(b.data, bid=b.bid)
    if b.kind == "answerbox":
        return _render_answerbox_body(b.data, bid=b.bid)
    return b.data.get("raw", "")


def render_block(b: Block, cfg: SubjectConfig | None = None) -> str:
    """Sérialise un Block avec ses marqueurs `%%QCM-BLOCK`/`%%QCM-END`.

    Si `cfg.shuffle_questions`, les questions sont enveloppées dans
    `\\element{questions}{...}` (cf. piège A du plan).
    """
    attrs = [f"bid={b.bid}", f"kind={b.kind}"]
    if b.kind == "question_qcm":
        attrs.append(f"qtype={b.data.get('qtype', 'single')}")
        if b.data.get("tag"):
            attrs.append(f"tag={b.data['tag']}")
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
    body = _render_block_body(b, cfg)
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
        parts.append(render_block(b, cfg))
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
            parts.append(render_block(b, cfg))
            parts.append("")
        parts.append(_QCM_ANSWER_END_BLOCKS_END)
    parts.append("}")
    parts.append(_QCM_EXEMPLAIRE_CLOSE)
    parts.append("\\end{document}")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# CRUD canonique (haut niveau, lit/écrit exam.tex)
# --------------------------------------------------------------------------

_io_lock = Lock()


def _save_subject(subject: dict) -> None:
    """Écrit le tex canonique sur disque + invalide les caches."""
    tex = render_subject(subject)
    EXAM_TEX.write_text(tex, encoding="utf-8")
    _invalidate_caches()


def _invalidate_caches() -> None:
    global _charmap_by_copy, _regions
    _cache["mtime"] = None
    _charmap_by_copy = {}
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
              data: dict | None = None) -> str:
    """Insère un bloc après `after_bid` (None = en fin). Retourne le bid créé."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind invalide: {kind}")
    with _io_lock:
        subject = parse_subject()
        _require_canonical(subject)
        bid = _gen_bid(kind)
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

        # 1. Repérer la zone \exemplaire{N}{ … }
        ex_m = re.search(r"\\exemplaire\{(\d+)\}\{", tex)
        if not ex_m:
            return {"ok": False, "log": "`\\exemplaire{N}{` introuvable — "
                    "structure de sujet AMC inattendue."}
        num_copies = int(ex_m.group(1))
        preamble = tex[:ex_m.start()].rstrip()
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
            return {"ok": False, "log": "Accolade fermante d'\\exemplaire "
                    "introuvable."}
        body = tex[body_start:j]
        # `post` (après \exemplaire) attendu = \end{document} — pas conservé,
        # le render_subject le réécrit canoniquement.

        # 2. Séparer le body : contenu des questions vs feuille de réponses.
        # On heuristique le début de la feuille de réponses au dernier `\newpage`
        # qui précède un marqueur AMC (`\formulaire`, `\AMCdebutFormulaire`,
        # `\AMCcodeGridInt`, `\champnom`).
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
        else:
            body_content = body
            answer_sheet_tex = ""

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

    Dérivé de `parse_subject()` pour mutualiser le parsing. En mode legacy le
    résultat est identique à l'ancienne implémentation. En mode canonique seuls
    les blocs `question_qcm` sont exposés (les `text`/`question_open` n'ont pas
    de notion de "q" indexé).
    """
    if not EXAM_TEX.exists():
        return {}
    mt = EXAM_TEX.stat().st_mtime
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
    ak = ANSWER_KEY.get(q, {})
    spec = {"type": ak.get("type", "single"), "options": ak.get("options", ""),
            "correct": ak.get("correct", ""), "tag": ak.get("tag", "")}
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
    spec = effective_spec(q, copy=copy)
    try:
        info = parse_tex().get(q)
    except Exception:
        info = None
    if info is None:
        ak = ANSWER_KEY.get(q, {})
        if spec["type"] == "single":
            return 1.0
        return round(len(spec["correct"]) * ak.get("b", 0.0), 6)
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
    """Réécrit dans `exam.tex` les blocs des questions éditées (mode legacy
    ou patch ciblé canonique).

    `updates` : [{q, tag, type, env, statement, answers:[{text, correct, bareme}], value}].
    """
    if not EXAM_TEX.exists():
        raise FileNotFoundError("sujet/exam.tex")
    with _io_lock:
        tex = EXAM_TEX.read_text(encoding="utf-8")
        # On utilise le parseur "legacy" qui repère les `\\begin{question*}` quel
        # que soit le mode du fichier — il extrait toujours les positions.
        qs = _attach_chars(_parse_legacy_questions(tex), copy=1)
        spans = []
        for upd in updates:
            q = int(upd["q"])
            info = qs.get(q)
            if info is None:
                raise KeyError(q)
            spans.append((info["block"][0], info["block"][1], _render_block(upd)))
        EXAM_TEX.write_text(apply_edits(tex, spans), encoding="utf-8")
        _invalidate_caches()


# --------------------------------------------------------------------------
# Régions PDF (aperçu au survol)
# --------------------------------------------------------------------------

_regions = None


def pdf_regions():
    """{q: {page, x0, y0, x1, y1}} — région de chaque question dans le PDF du
    sujet, en pixels 300 dpi. Déduite des cases (`layout_box` pages 1-7) :
    région = de la fin des cases de la question précédente à la fin des cases
    de la question (englobe énoncé + réponses). Layout d'origine AMC.

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
    TOP, PAD = 240.0, 70.0
    for page, items in by_page.items():
        items.sort(key=lambda it: it[1]["ymin"])
        w, h = pages.get(page, (2480.0, 3508.0))
        prev_bottom = TOP
        for q, d in items:
            if _is_bareme(q):
                # Barème : on ne veut PAS hériter du `prev_bottom` (cas QCM
                # avec énoncé ; ici il n'y a rien d'utile au-dessus). On serre
                # la région au-tour de la ligne du barème elle-même.
                y0 = max(0.0, d["ymin"] - 60.0)
                y1 = min(h, d["ymax"] + 60.0)
            else:
                y0 = max(0.0, min(prev_bottom, d["ymin"]) - 20.0)
                y1 = min(h, d["ymax"] + PAD)
            _regions[q] = {"page": page, "x0": 110.0, "y0": y0,
                           "x1": w - 110.0, "y1": y1}
            prev_bottom = d["ymax"] + PAD
    return _regions


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


def compile_pdf():
    """Compile `exam.tex` → `sujet/DOC-sujet.pdf`. Retourne {'ok': bool, 'log': str}.

    Compilation dans un dossier temporaire (sujet/ reste propre) ; le PDF n'est
    copié qu'en cas de succès — un échec laisse l'ancien DOC-sujet.pdf intact.
    """
    global _regions
    with _compile_lock:
        if not EXAM_TEX.exists():
            return {"ok": False, "log": "sujet/exam.tex introuvable."}
        tmp = Path(tempfile.mkdtemp(prefix="amc_compile_"))
        try:
            shutil.copy(EXAM_TEX, tmp / "exam.tex")
            # `exam-config.tex` est lu par automultiplechoice.sty (\jobname-config) :
            # définir \SujetExterne active le mode « calibration » → pdflatex écrit
            # le fichier de calage exam.xy (positions des cases) en plus du PDF.
            (tmp / "exam-config.tex").write_text(
                "\\def\\SujetExterne{1}\n", encoding="utf-8")
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
