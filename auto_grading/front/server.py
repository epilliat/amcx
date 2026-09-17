"""Flask app pour relire/corriger les copies QCM.

Endpoints:
  GET  /                                    - index tableau de toutes les copies
  GET  /student/<batch>/<page>              - vue d'une copie
  GET  /student/<batch>/<page>/zoom         - grille de zoom case par case
  GET  /api/copy/<batch>/<page>             - JSON brut (lecture)
  POST /api/toggle                          - body {batch, page, q, char} → toggle
  POST /api/save                            - body {batch, page, payload} → sauve JSON
  GET  /img/<batch>/<page>                  - image originale
  GET  /zoom_img/<batch>/<page>/<q>_<char>.jpg  - crop d'une case (cache disk)

Run:
  python server.py [--port 5000]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from threading import Lock

import cv2
import fitz  # PyMuPDF — rendu des aperçus PDF
import numpy as np
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename

# Bootstrap : ajout du dossier d'installation sur sys.path pour importer
# les modules métier (layout_store, sujet_store, …). Le dossier d'installation
# ne change PAS pendant la vie du process — seul le projet actif change, et un
# changement de projet déclenche un redémarrage complet (project_state.execv).
_INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_INSTALL_DIR))

import config
import project_state
import layout_store
import review_state
from cv_grade import (detect_mires, warp_to_canonical, load_layout,
                      compute_per_question_offsets, load_name_field)
from student_list import StudentMatcher
from score import score_copy, score_question
from sujet_store import (parse_tex, save_questions, compile_pdf, SUJET_DIR,
                         compile_publication, PUBLICATION_KINDS,
                         PUBLICATION_PDF, PUBLICATION_LABEL,
                         amc_question_map, check_layout_consistency,
                         pop_store_warnings,
                         effective_spec, pdf_regions, render_bareme_examples,
                         charmap_for_copy, letters_stale, tex_to_amc,
                         version_copy_ranges,
                         region_copies as sujet_region_copies,
                         update_version as sujet_update_version,
                         header_to_raw as sujet_header_to_raw,
                         answer_sheet_to_raw as sujet_answer_sheet_to_raw,
                         HeaderBlock, AnswerSheetConfig,
                         version_total_max as sujet_version_total_max,
                         analyze_header_tex as sujet_analyze_header,
                         add_version as sujet_add_version,
                         delete_version as sujet_delete_version,
                         restore_version as sujet_restore_version,
                         set_block_group as sujet_set_block_group,
                         COMMON_GROUP as sujet_common_group,
                         max_score as sujet_max_score, total_max as sujet_total_max,
                         subject_total_max as sujet_subject_total_max,
                         parse_subject, subject_to_dict,
                         add_block as sujet_add_block,
                         delete_block as sujet_delete_block,
                         move_block as sujet_move_block,
                         update_block as sujet_update_block,
                         duplicate_block as sujet_duplicate_block,
                         update_config as sujet_update_config,
                         update_header as sujet_update_header,
                         update_answer_sheet as sujet_update_answer_sheet,
                         regenerate_seed as sujet_regenerate_seed,
                         migrate_to_canonical as sujet_migrate_to_canonical)
import bank
import bank_online
import bank_auth
import bank_taxonomy as tx
import workspace
from config import load_config, save_config


def _bank():
    """Dispatcher banque locale ↔ Supabase selon le `type` de la banque active.

    Le module retourné expose les mêmes fonctions (load, save, delete,
    list_questions, update_project_stats, from_block, to_block). Switch à
    chaud par l'user via le dropdown Banque (POST /api/banks/<slug>/activate)
    sans redémarrer le serveur.
    """
    return bank_online if config.active_bank_cfg().get("type") == "online" else bank
from grade_imports import (read_table, analyze_table, match_report, set_name_override,
                           jsonable_cell, build_all_series, add_grade_file,
                           remove_grade_file, ensure_imports_dir, IMPORTS_DIR)
# Colonnes de note, rescaling, agrégation : logique pure, sans notion de projet
# actif — le barème lui est passé en paramètre. Un examen en est le cas N = 1
# (cf. `exam_columns`) ; histogrammes, nuage, formule et bornes de curseurs
# décrivent un ENSEMBLE d'examens et ne servent qu'à `/cohorte`.
from grades_view import (SERIES_COLORS, series_stats, compute_aggregate,
                         build_formula, slider_ranges,
                         compute_multi_series_stats, multi_histogram_geometry)
# La table des résultats — une ligne par étudiant, absents compris. Pure, sans
# Flask : c'est elle que lira une vue d'ensemble, par sous-processus.
import exam_results
import cohort

# Paths du projet actif (figés au démarrage du process). Switcher de projet
# = `project_state.restart_server_with_project()` qui exec ce process à neuf.
ROOT = config.project_root()
PAGES_DIR = ROOT / "pages"
RAW_DIR = ROOT / "raw_responses"
ZOOM_CACHE = Path(__file__).resolve().parent / "static" / "zoom_cache"
ZOOM_CACHE.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.context_processor
def _inject_project_context():
    """Variables de la topbar : l'évaluation active et ses voisines.

    ⚠ Le menu ne liste plus les « récents » mais les **évaluations présentes
    dans le dossier de travail** (`workspace.evaluations`). Une liste de
    récents décrit l'historique d'une personne ; ce qu'on veut savoir en
    ouvrant ce menu, c'est ce que contient le dossier qu'on a sous les yeux
    dans l'onglet Fichiers — sinon les deux écrans montrent deux mondes.

    ⚠ Le scan est **borné et silencieux** : un dossier de travail illisible ne
    doit pas faire échouer le rendu de toutes les pages.
    """
    p = config.project_root()
    valid = project_state.is_valid_project(p)
    try:
        evals = workspace.evaluations()
        ws_name = (workspace.root() or Path("")).name
    except Exception:
        evals, ws_name = [], ""
    active = str(p.parent if p.name == "auto_grading" else p)
    for e in evals:
        e["active"] = (e["path"] == active)
    return {
        "project_name": project_state.display_name(p) if valid else "Aucune évaluation",
        "project_path": str(p) if valid else "",
        "project_evaluations": evals,
        "workspace_name": ws_name,
        "app_name": project_state.APP_NAME,
    }


@app.errorhandler(ValueError)
def _bad_request(e):
    """Entrée invalide (nom de batch, numéro non entier…) → 400, pas 500."""
    return jsonify({"error": str(e)}), 400


@app.before_request
def _same_origin_only():
    """Refuse les requêtes mutantes venant d'une autre origine.

    Le serveur écoute en local sans authentification et toutes les routes POST
    acceptent `get_json(force=True)` : une page web tierce ouverte dans le même
    navigateur peut viser http://127.0.0.1:5050 et déclencher n'importe quelle
    mutation. Les navigateurs envoient `Origin` sur les requêtes non-GET ; on
    exige qu'elle corresponde à l'hôte servi. Absence d'`Origin` (curl, tests,
    client non-navigateur) : accepté, ces appels ne sont pas des drive-by.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    from urllib.parse import urlparse
    if urlparse(origin).netloc != urlparse(request.host_url).netloc:
        return jsonify({"error": "origine non autorisée"}), 403
    return None


# Clés de config à ne jamais renvoyer au navigateur (secrets en clair).
_SECRET_CFG_KEYS = ("anthropic_api_key",)
_SECRET_BANK_KEYS = ("user_token", "refresh_token")


def public_config(cfg: dict) -> dict:
    """Copie de la config sans les secrets, pour l'envoi au front.

    `anthropic_api_key` et les jetons Supabase des banques n'ont aucune raison
    d'atteindre le navigateur : `/api/ai/auth-status` expose déjà `has_api_key`
    et `/api/banks` le `logged_in` de chaque banque.
    """
    out = dict(cfg)
    for k in _SECRET_CFG_KEYS:
        if out.get(k):
            out[k] = "***"          # présence signalée, valeur masquée
    banks = out.get("banks")
    if isinstance(banks, dict):
        out["banks"] = {
            slug: {k: v for k, v in (entry or {}).items()
                   if k not in _SECRET_BANK_KEYS}
            for slug, entry in banks.items()
        }
    return out


# Caches
_layout_cache = None
_layout_by_qchar = None
_warp_cache = {}   # (batch, page) -> warped gray
_offsets_cache = {}  # (batch, page) -> {q: (dx, dy)}
_warp_lock = Lock()
_matcher_cache = None
_series_cache = None   # séries de notes importées, jointes aux étudiants


def invalidate_layout_caches() -> None:
    """Vide les caches dérivés du calage.

    À appeler après toute recompilation : `compile_pdf` régénère `exam.xy`, donc
    la géométrie des cases change, mais ces caches (remplis une fois par
    process) continuaient à servir l'ancien calage jusqu'au redémarrage —
    crops et overlays décalés après un « Compiler ».
    """
    global _layout_cache, _layout_by_qchar
    _layout_cache = None
    _layout_by_qchar = None
    _warp_cache.clear()
    _offsets_cache.clear()


def get_layout(copy: int | None = None):
    """Cases de la feuille de réponses **de cette copie**, + index (q, char).

    ⚠ `copy` n'est pas décoratif. Avec des versions du sujet (groupes AMC), la
    copie 1 porte les questions AMC 1-5 et la copie 36 les 10-14 : le calage de
    la copie 1 ne contient aucune des questions de la seconde. Appelé sans
    copie, l'index rendait donc `404` sur toutes les cases des copies de la
    deuxième version — les vignettes de la review rapide et du zoom
    n'apparaissaient pas du tout sur ces copies.
    """
    global _layout_cache, _layout_by_qchar
    key = int(copy) if copy else 1
    if _layout_cache is None:
        _layout_cache, _layout_by_qchar = {}, {}
    if key not in _layout_cache:
        boxes = layout_store.get_layout(copy=key).sheet_boxes()
        _layout_cache[key] = boxes
        _layout_by_qchar[key] = {(b.question, b.char): b for b in boxes}
    return _layout_cache[key], _layout_by_qchar[key]


def copy_sheets(d: dict, batch: str, page: int) -> list:
    """Feuilles scannées d'une copie → `[{batch, page, sheet_page}, …]`.

    Une copie tient d'ordinaire sur une feuille : la liste en compte une, celle
    de son propre JSON. Quand le sujet déborde, `seed_raw_responses` recolle
    plusieurs feuilles dans une copie et note leur provenance dans `_sheets` —
    c'est cette liste qui dit quelle IMAGE montre quelle partie des réponses.
    """
    sheets = d.get("_sheets")
    if sheets:
        return [{"batch": s.get("batch", batch), "page": int(s.get("page", page)),
                 "sheet_page": s.get("sheet_page")} for s in sheets]
    return [{"batch": batch, "page": page, "sheet_page": d.get("_sheet_page")}]


def sheet_of_question(d: dict, batch: str, page: int) -> dict:
    """question → la feuille scannée qui la porte (`{batch, page, sheet_page}`).

    Sans ça, le zoom d'une case de la 2ᵉ feuille irait la chercher dans l'image
    de la 1ʳᵉ, à des coordonnées qui n'y correspondent à rien.
    """
    sheets = copy_sheets(d, batch, page)
    if len(sheets) == 1:
        # ⚠ Le calage DE LA COPIE : une copie de la seconde version d'un sujet
        # ne porte pas les mêmes numéros de question que la copie 1.
        lay = layout_store.get_layout(copy=copy_id_of(d))
        return {q: sheets[0] for q in {b.question for b in lay.sheet_boxes()}}
    lay = layout_store.get_layout(copy=copy_id_of(d))
    out = {}
    for s in sheets:
        sp = s.get("sheet_page")
        if sp is None:
            continue
        for b in lay.sheet_boxes(page=int(sp)):
            out[b.question] = s
    return out


def question_numbers(copy: int = 1) -> tuple[list, list]:
    """(questions QCM notées, colonnes du code étudiant) dérivées du calage.

    S'appuie sur `sujet_store.amc_question_map()` : les cases à lettres d'un
    bloc à cases de notation (`\\AMCOpen`, answerbox) portent aussi des lettres
    et étaient comptées comme un QCM fantôme, noté avec une spec vide.

    ⚠ **Toujours passer la copie** quand on en a une. Sur un sujet à plusieurs
    versions (matin/après-midi), la copie 1 porte les questions AMC 1-5 et la
    copie 2 les 10-14 : itérer celles de la copie 1 sur une feuille de
    l'après-midi n'affiche aucune de ses questions et en invente cinq vides.
    """
    m = amc_question_map(copy)
    return sorted(m["qcm"]), list(m["id"])


def spec_of(q: int, copy: int = 1) -> dict:
    """Spec d'une question désignée par son **numéro AMC** (la clé d'`answers`).

    ⚠ `sujet_store.effective_spec` prend, lui, l'indice d'ordre du document (la
    clé de `parse_tex`). Les deux coïncident sur un sujet simple, plus du tout
    dès qu'il y a des groupes — d'où ce passage obligé par la carte du calage,
    exactement comme le fait `score.score_question`.
    """
    return effective_spec(amc_question_map(copy)["qcm"].get(q, q), copy=copy)


def max_of(q: int, copy: int = 1) -> float:
    """Score maximal d'une question désignée par son numéro AMC (cf. `spec_of`)."""
    return sujet_max_score(amc_question_map(copy)["qcm"].get(q, q), copy=copy)


def id_columns(copy: int = 1) -> list:
    """Numéros AMC des colonnes du code étudiant, dérivés du calage.

    ⚠ Ne JAMAIS coder ces numéros en dur : ils dépendent du sujet (31 QCM →
    [32..35] sur EXAM_2026, mais [3,4,5,6] sur un sujet à 2 QCM et [33..36] sur
    un sujet à 32 QCM), et `id_grid_digits` est configurable — le nombre de
    colonnes n'est pas figé à 4. Cf. piège #8 du CLAUDE.md.
    """
    return question_numbers(copy)[1]


def get_matcher():
    global _matcher_cache
    if _matcher_cache is None:
        _matcher_cache = StudentMatcher()
    return _matcher_cache


def get_series():
    """Séries de notes importées (csv/xlsx), jointes aux étudiants — cache module."""
    global _series_cache
    if _series_cache is None:
        _series_cache = build_all_series(load_config(), get_matcher())
    return _series_cache


def get_warped(batch: str, page: int, copy: int | None = None) -> tuple[np.ndarray, dict]:
    """Retourne (warped_image, offsets_par_question).

    ⚠ `copy` sélectionne le calage des offsets : sur un sujet à versions, une
    copie de la seconde version ne porte aucune des questions de la première,
    et les offsets étaient alors calculés pour des questions absentes de la
    page — donc vides, donc jamais appliqués aux crops.
    """
    key = (batch, page)
    if key in _warp_cache:
        return _warp_cache[key], _offsets_cache[key]
    with _warp_lock:
        if key in _warp_cache:
            return _warp_cache[key], _offsets_cache[key]
        img_path = PAGES_DIR / safe_batch(batch) / f"page_{page:03d}.jpg"
        img = cv2.imread(str(img_path))
        if img is None:
            raise FileNotFoundError(img_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lay = layout_store.get_layout()
        canon_mires = np.asarray(lay.mires, dtype=np.float32)
        canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
        # Même recalage que le grade : les mires sont cherchées autour de
        # leur position canonique. Sans le calage, l'image affichée pourrait
        # être warpée autrement que celle qui a servi à lire les cases.
        mires = detect_mires(gray, layout=lay)
        if mires is None or len(canon_mires) != 4:
            warped = cv2.resize(gray, (canon_w, canon_h))
        else:
            warped = warp_to_canonical(gray, mires, canon_mires, canon_w, canon_h)
        layout, _ = get_layout(copy)
        offsets = compute_per_question_offsets(warped, layout)
        if len(_warp_cache) > 20:
            old = next(iter(_warp_cache))
            _warp_cache.pop(old)
            _offsets_cache.pop(old, None)
        _warp_cache[key] = warped
        _offsets_cache[key] = offsets
        return warped, offsets


# Un nom de batch vient d'un dossier de `pages/` — donc d'un nom de PDF scanné.
# Il arrive aussi par le corps JSON d'une requête : sans validation,
# `batch="../../.."` sort du projet (lecture ET écriture, `save_copy_json` crée
# les dossiers manquants). Validé au plus près du disque plutôt que dans chaque
# route, pour qu'aucun nouvel appelant ne puisse l'oublier.
_BATCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\- ]*$")


def required(body, *keys):
    """Extrait des champs obligatoires d'un corps JSON.

    Lève ValueError (→ 400 via l'errorhandler) plutôt que de laisser un KeyError
    remonter en 500. Volontairement explicite plutôt qu'un errorhandler global
    sur KeyError, qui masquerait aussi les vrais bugs internes.
    """
    body = body or {}
    missing = [k for k in keys if k not in body]
    if missing:
        raise ValueError("champ(s) manquant(s) : " + ", ".join(missing))
    return [body[k] for k in keys]


def safe_batch(batch) -> str:
    """Valide un nom de batch. Lève ValueError si suspect."""
    b = str(batch or "")
    if not _BATCH_RE.match(b) or ".." in b:
        raise ValueError(f"nom de batch invalide : {b!r}")
    return b


# Le cache de crops vit dans l'installation, partagée par tous les projets :
# sans espace de noms, deux projets ayant un batch `scan1` se marchent dessus
# et affichent les cases du mauvais examen après un changement de projet.
_ZOOM_NS = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:10]


def zoom_cache_dir(batch: str, page: int) -> Path:
    """Dossier de cache des crops d'une copie (isolé par projet, batch validé)."""
    return ZOOM_CACHE / _ZOOM_NS / f"{safe_batch(batch)}_page_{page:03d}"


def load_copy_json(batch: str, page: int) -> dict | None:
    p = RAW_DIR / safe_batch(batch) / f"page_{page:03d}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_copy_json(batch: str, page: int, data: dict):
    """Écrit un JSON de `raw_responses/` — la source de vérité de la relecture.

    Écriture atomique : un crash (ou le `os._exit(0)` du changement de projet)
    en plein dump laisserait sinon un fichier tronqué.
    """
    config.write_json_atomic(
        RAW_DIR / safe_batch(batch) / f"page_{page:03d}.json", data)


def copy_id_of(d: dict) -> int:
    """Numéro de copie d'un JSON `raw_responses/*.json`.

    `_copy_id` est écrit par `cv_grade.detect_copy_id` quand le sujet a
    `\\exemplaire{N>1}`. Pour tout JSON legacy (EXAM_2026, sujet sans
    randomisation, etc.), on retombe sur la copie #1.
    """
    try:
        return int(d.get("_copy_id", 1))
    except (TypeError, ValueError):
        return 1


def resolve_student(d: dict, matcher) -> dict:
    """Comme matcher.resolve(), mais honore `_student_override` (posé à la main).

    ⚠ Si l'override ne désigne plus personne — la liste a changé depuis —, on
    **s'arrête là**. L'ancienne version retombait sans un mot sur la lecture de
    la grille : une copie explicitement attribuée à ABIDELLI s'affichait alors
    au nom d'ADJEBA MBA, toujours estampillée « assignée à la main », sans
    aucun drapeau. Une décision humaine ne doit pas être remplacée en silence
    par une lecture machine ; la copie redevient à traiter.
    """
    override = d.get("_student_override")
    if override:
        s = matcher.by_full_id(str(override))
        if s is not None:
            return {"matched": s, "method": "override", "score": 1.0, "flag": ""}
        return {"matched": None, "method": "override_lost", "score": 0.0,
                "flag": f"l'étudiant {override} assigné à la main ne figure plus "
                        f"dans la liste"}
    return matcher.resolve(d.get("student_id", ""), d.get("student_name", ""))


def list_all_copies():
    """Énumère les 174 copies, enrichies (nom canonique, score, flags, diff)."""
    matcher = get_matcher()
    out = []
    if not RAW_DIR.exists():
        return out
    for batch_dir in sorted(RAW_DIR.iterdir()):
        if not batch_dir.is_dir():
            continue
        for jp in sorted(batch_dir.glob("page_*.json")):
            with open(jp, encoding="utf-8") as f:
                d = json.load(f)
            batch = batch_dir.name
            page = int(jp.stem.split("_")[1])
            answers_int = {int(k): v for k, v in d.get("answers", {}).items()}
            scores = score_copy(answers_int, copy=copy_id_of(d))
            sid = d.get("student_id", "????")
            name = d.get("student_name", "")
            match = resolve_student(d, matcher)
            canon = match["matched"]
            diff = d.get("_cv_amc_diff", [])
            has_amc = "_amc_answers" in d
            rev = copy_review(d)
            out.append({
                "n_open": rev["n_open"],
                "n_flagged": rev["n_flagged"],
                "id_issue": bool(id_state(d, match)),
                "batch": batch,
                "page": page,
                "student_id": sid,
                "student_name": name,
                "canonical_name": f"{canon.nom} {canon.prenom}" if canon else "?",
                "canonical_id": canon.id if canon else "",
                # "" tant que la copie n'est reliée à personne, ou que la liste
                # ne porte pas de colonne courriel : jamais deviné.
                "canonical_email": canon.email if canon else "",
                "match_method": match["method"],
                "score": scores["total"],
                "source": d.get("_source", "?"),
                "flags": d.get("_flags", []),
                "validated": "validated" in d.get("_flags", []),
                "cv_amc_diff_count": len(diff) if has_amc else None,
                "amc_validated_cells": d.get("_amc_validated_cells"),
                "amc_copy": d.get("_amc_copy"),
            })
    return out


def list_ordered_keys():
    """Retourne la liste ordonnée des (batch, page) en alphabétique."""
    keys = []
    if not RAW_DIR.exists():
        return keys
    for batch_dir in sorted(RAW_DIR.iterdir()):
        if not batch_dir.is_dir():
            continue
        for jp in sorted(batch_dir.glob("page_*.json")):
            keys.append((batch_dir.name, int(jp.stem.split("_")[1])))
    return keys


def neighbors(batch: str, page: int) -> dict:
    """Retourne {prev: (batch, page) ou None, next: (batch, page) ou None}."""
    keys = list_ordered_keys()
    try:
        i = keys.index((batch, page))
    except ValueError:
        return {"prev": None, "next": None}
    return {
        "prev": keys[i - 1] if i > 0 else None,
        "next": keys[i + 1] if i < len(keys) - 1 else None,
    }


def diff_set(d: dict) -> set:
    """Retourne l'ensemble des (q, char) où CV diff AMC."""
    return {(item["q"], item["char"]) for item in d.get("_cv_amc_diff", [])}


def doubt_set(d: dict) -> set:
    """Retourne l'ensemble des (q, char) flaggées « douteuses » par le levier 2
    (cf. cv_grade.grade_image — convergence d'estimateurs masqué/seuil/GBM)."""
    return {(a["q"], a["char"]) for a in d.get("_ambiguous_cells", [])}


def copy_review(d: dict) -> dict:
    """État de relecture d'une copie : items groupés par question, restant à voir.

    Unique point d'entrée — la vue `/flagged`, l'aperçu de la copie, le compteur
    du tableau de bord et l'entraînement doivent compter la MÊME chose, sinon
    l'un dit « terminé » pendant qu'un autre dit « 107 à revoir ».
    """
    copy = copy_id_of(d)
    return review_state.copy_review(
        d, lambda q: spec_of(q, copy), question_numbers(copy)[0])


def copy_open_count(d: dict, matcher=None) -> int:
    """Ce qu'il reste à traiter sur une copie, identité comprise.

    ⚠ `copy_review()["n_open"]` ne compte QUE les réponses. La file `/flagged`,
    elle, ajoute l'identité douteuse — d'où deux nombres différents pour la même
    copie : la page affichait « 9 à traiter » et le premier clic la faisait
    tomber à 7, l'identité disparaissant du compte en même temps que la case
    traitée. Une seule implémentation, servie à la page comme aux routes.
    """
    matcher = matcher or get_matcher()
    n = copy_review(d)["n_open"]
    return n + (1 if id_state(d, resolve_student(d, matcher)) else 0)


def review_open_cells(d: dict) -> set:
    """(q, char) des cases signalées ENCORE à traiter — pour l'aperçu de la copie."""
    out = set()
    for it in copy_review(d)["items"]:
        for c in it["cells"]:
            if c["flagged"] and not c["reviewed"]:
                out.add((it["q"], c["char"]))
    return out




def id_state(d: dict, match: dict) -> str | None:
    """L'identité de cette copie demande-t-elle un coup d'œil, et de quelle sorte ?

    - `"unresolved"` : aucun étudiant ne correspond. Rien n'est attribuable.
    - `"weak"` : la grille du numéro est illisible et le rattachement ne tient
      qu'à un nom manuscrit reconnu de façon approchée. Ça se confirme en deux
      secondes, et l'erreur qu'on évite — donner la note d'un étudiant à un
      autre — est la plus coûteuse du système.
    - `None` : le numéro est lu, ou l'identité a été posée à la main.

    ⚠ Un rattachement par `override` (glissé dans /identites) ou `id_corrige`
    n'est PAS douteux : quelqu'un l'a décidé. Les compter aurait rempli la file
    de copies déjà tranchées.
    """
    if d.get("_reviewed_id"):
        return None
    if match["method"] == "override_lost":
        return "lost"
    if match["matched"] is None:
        return "unresolved"
    if match["method"] == "override" or "id_corrige" in (d.get("_flags") or []):
        return None
    return "weak" if "?" in (d.get("student_id") or "") else None


def absent_students(copies: list) -> list:
    """Étudiants de la liste qu'aucune copie ne réclame.

    ⚠ Ils ne comptent **ni dans les histogrammes ni dans les statistiques** :
    ceux-ci décrivent les copies corrigées. Ils n'apparaissent que dans les
    exports, où l'absence d'une ligne serait prise pour un oubli.
    """
    seen = {c["canonical_id"] for c in copies if c.get("canonical_id")}
    return [st for st in get_matcher().students if st.id not in seen]


def subject_total_max() -> float:
    """Barème maximal du sujet (cf. `sujet_store.subject_total_max`)."""
    return sujet_subject_total_max()


def exam_columns() -> list:
    """L'unique colonne de note d'un examen : le QCM, sur son propre barème.

    ⚠ L'évaluation d'UN examen n'a plus aucun réglage : la note affichée,
    exportée et envoyée est le **score brut sur le barème du sujet**. Seuiller
    et ramener sur une autre échelle ne sert qu'à comparer ou agréger cet
    examen avec autre chose — ça appartient au niveau au-dessus, pas ici.

    On garde pour autant la forme « liste de colonnes » que consomme
    `compute_aggregate` : un examen en est le cas N = 1, et le rescaling s'y
    réduit à l'identité. Une seule implémentation de la note, deux appelants.
    """
    b = subject_total_max() or 20.0
    return [{"key": "qcm", "label": "QCM", "color": SERIES_COLORS[0],
             "seuil": b, "auto_seuil": True, "max": b, "agg_weight": 1.0}]


def exam_threshold() -> float:
    """Plafond de la note d'un examen : son barème — `min(brut, barème) = brut`."""
    return subject_total_max() or 20.0


def legacy_grade_settings(cfg: dict) -> list[str]:
    """Réglages de note que l'échelle d'un examen n'applique plus.

    ⚠ Rien n'est effacé : ces clés restent dans `config.json` et remonteront au
    niveau qui les rend utiles. Mais les taire serait un changement de note
    sans un mot — un projet réglé « ramené sur 20 » exporte désormais un score
    brut sur le barème, et ce fichier part à la scolarité.

    ⚠ Le critère est **la note, pas la présence d'une clé**. L'ancienne note
    valait `min(brut × max ∕ normalisation, plafond)` : elle est identique à la
    nouvelle tant que `max = normalisation` et que le plafond ne mord pas sur
    le barème. Un projet réglé « 5 sur 5, plafond 20 » sur un sujet qui vaut 5
    n'a donc rien à signaler — et c'est le cas courant. Signaler quand même
    aurait fait de ce bandeau une alarme qu'on apprend à ignorer.
    """
    b = subject_total_max()
    out = []
    if b:
        seuil = float(cfg.get("qcm_seuil") or b)
        mx = float(cfg.get("qcm_max", 20.0))
        thr = float(cfg.get("final_threshold", 20.0))
        if abs(mx - seuil) > 1e-9:
            out.append(f"note ramenée sur {mx:g} à partir de {seuil:g}")
        if thr < b - 1e-9:
            out.append(f"plafond à {thr:g}")
    n_cols = sum(len(fc.get("grade_cols") or [])
                 for fc in (cfg.get("grade_files") or []))
    if n_cols:
        out.append(f"{n_cols} colonne(s) de notes importées, plus agrégées ici")
    return out


def build_grade_files_info(cfg: dict, imported: list) -> list:
    """Pour la modale : fichiers configurés + diagnostic de jointure par fichier."""
    by_key = {(s["file"], s["idx"]): s for s in imported}
    matcher = get_matcher()
    out = []
    for fc in cfg.get("grade_files", []):
        cols = []
        for gc in fc.get("grade_cols", []):
            s = by_key.get((fc["path"], gc.get("idx")))
            cols.append({"idx": gc.get("idx"), "label": gc.get("label", ""),
                         "n_matched": len(s["values"]) if s else 0})
        out.append({
            "path": fc["path"], "filename": Path(fc["path"]).name,
            "join_mode": fc.get("join_mode", "name"),
            "sheet": fc.get("sheet", ""),
            "join_col": fc.get("join_col", 0),
            "data_start": fc.get("data_start", 0),
            "grade_cols": cols,
            "report": match_report(fc, matcher),
        })
    return out


def build_student_card(batch: str, page: int) -> dict | None:
    """Contexte de la fiche étudiant (panneau droit du dashboard)."""
    d = load_copy_json(batch, page)
    if d is None:
        return None
    matcher = get_matcher()
    match = resolve_student(d, matcher)
    canon = match["matched"]
    answers_int = {int(k): v for k, v in d.get("answers", {}).items()}
    copy = copy_id_of(d)
    scores = score_copy(answers_int, copy=copy)
    diff_questions = {q for (q, _) in diff_set(d)}
    per_question = []
    for q in question_numbers(copy)[0]:
        spec = spec_of(q, copy)
        sel = sorted(answers_int.get(q, []))
        per_question.append({
            "q": q, "tag": spec["tag"], "type": spec["type"],
            "correct": "".join(sorted(spec["correct"])),
            "selected": "".join(sel) or "—",
            "score": scores["per_question"][q],
            "max": max_of(q, copy),
            "has_diff": q in diff_questions,
        })
    return {
        "batch": batch, "page": page,
        "canonical_name": f"{canon.nom} {canon.prenom}" if canon else "?",
        "canonical_id": canon.id if canon else "",
        "student_id": d.get("student_id", "????"),
        "score": round(scores["total"], 2),
        "max_score": sujet_total_max(copy=copy),
        "validated": "validated" in d.get("_flags", []),
        "flags": d.get("_flags", []),
        "per_question": per_question,
    }


def _project_files_info() -> dict:
    """Récap des fichiers du projet actif pour la zone « Fichiers du projet » du dashboard.

    Retourne `{amc_dir, scan_pdfs: [{name, path, pages, mtime}], n_extracted,
    n_corrected, n_validated, student_xlsx, student_xlsx_name, n_students}`.
    """
    cfg = load_config()
    proj_root = config.project_root()
    info: dict = {
        "amc_dir":      str(config.amc_dir()),
        "scan_pdfs":    [],
        "scan_pdfs_hidden": [],   # retirés de la liste AMCx (visibles dans `<details>`)
        "n_extracted":  0,
        "n_corrected":  0,
        "n_validated":  0,
        "student_xlsx": (cfg.get("student_xlsx") or "").strip(),
        "student_xlsx_name": "",
        "student_xlsx_exists": False,
        "student_sheet": "",
        "n_students":   0,
        "student_warnings": [],
    }
    excluded = set(cfg.get("scan_pdfs_excluded") or [])
    # PDFs : auto-découvert ou liste explicite (`scan_pdfs`).
    amc = config.amc_dir()
    if amc.is_dir():
        explicit = cfg.get("scan_pdfs") or []
        if explicit:
            paths = [Path(p) if Path(p).is_absolute() else (amc / p) for p in explicit]
        else:
            # Liste filtrée par extract_pages (artefacts + retirés exclus).
            import extract_pages
            paths = extract_pages.discover_pdfs()
        # Détecte les PDFs retirés pour affichage dans `<details>`.
        from datetime import datetime as _dt
        from re import match as _m
        # Tous les PDFs trouvables (avant filtrage UI)
        all_in_dir = [p for p in sorted(amc.glob("*.pdf"))]
        for p in all_in_dir:
            if p.name in excluded:
                info["scan_pdfs_hidden"].append({"name": p.name, "path": str(p)})
        for p in paths:
            try:
                doc = fitz.open(str(p))
                n_pages = doc.page_count
                doc.close()
            except Exception:
                n_pages = 0
            try:
                mtime = _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                size_mb = p.stat().st_size / 1e6
            except Exception:
                mtime, size_mb = "", 0
            info["scan_pdfs"].append({
                "name":   p.name,
                "path":   str(p),
                "pages":  n_pages,
                "mtime":  mtime,
                "size_mb": round(size_mb, 2),
            })

    # Pages extraites
    pages_dir = proj_root / "pages"
    if pages_dir.is_dir():
        info["n_extracted"] = sum(1 for d in pages_dir.iterdir() if d.is_dir()
                                  for _ in d.glob("page_*.jpg"))

    # raw_responses
    raw_dir = proj_root / "raw_responses"
    if raw_dir.is_dir():
        rr = list(raw_dir.rglob("page_*.json"))
        info["n_corrected"] = len(rr)
        n_val = 0
        for jp in rr:
            try:
                with open(jp, encoding="utf-8") as f:
                    d = json.load(f)
                if "validated" in (d.get("_flags") or []):
                    n_val += 1
            except Exception:
                pass
        info["n_validated"] = n_val

    # Liste étudiants xlsx
    if info["student_xlsx"]:
        xpath = Path(info["student_xlsx"])
        if not xpath.is_absolute():
            xpath = (proj_root / xpath).resolve()
        info["student_xlsx_name"] = xpath.name
        info["student_xlsx_exists"] = xpath.exists()
        # L'onglet fait partie de l'identité de la liste : deux onglets d'un
        # même classeur sont deux promos différentes.
        info["student_sheet"] = (cfg.get("xlsx_sheet") or "").strip()
        if xpath.exists():
            try:
                m = get_matcher()
                info["n_students"] = len(m.students)
                # Homonymes, collisions de numéros, colonne disparue : ça se
                # voit sur la carte, plus seulement sur `stdout`.
                info["student_warnings"] = m.warnings(len(id_columns() or []))
            except Exception as e:                      # noqa: BLE001
                info["n_students"] = 0
                info["student_warnings"] = [str(e)]
    return info


@app.route("/")
def index():
    """Onglet **Évaluation** : un examen, ses copies, ses moyennes par question.

    ⚠ Aucun réglage de note ici, volontairement. La note d'un examen est son
    score brut sur le barème du sujet ; seuiller et ramener sur une autre
    échelle ne sert qu'à le comparer ou l'agréger avec autre chose, ce qui est
    le travail du niveau au-dessus. Les histogrammes, le nuage de points, la
    formule et les fichiers de notes importés ont suivi le même raisonnement :
    ils décrivent le projet qui contient cette évaluation, pas elle.
    """
    # Pas de projet actif valide → page d'accueil (onboarding).
    p = config.project_root()
    if not project_state.is_valid_project(p):
        return render_template("onboarding.html",
                               default_root=str(project_state.DEFAULT_PROJECTS_ROOT),
                               active="onboarding")

    copies = list_all_copies()
    cfg = load_config()
    # Statistiques de la note BRUTE — les absents n'y entrent pas : elles
    # décrivent les copies corrigées (cf. `absent_students`).
    stats = series_stats([c["score"] for c in copies])
    try:
        questions = question_stats()["questions"]
    except Exception as e:                              # noqa: BLE001
        questions, q_error = [], str(e)
    else:
        q_error = ""

    return render_template("evaluation.html", copies=copies, total=len(copies),
                           stats=stats, bareme=subject_total_max(),
                           questions=questions, q_error=q_error,
                           legacy=legacy_grade_settings(cfg),
                           project_files=_project_files_info(),
                           active="evaluation")


@app.route("/api/student-card/<batch>/<int:page>")
def api_student_card(batch, page):
    card = build_student_card(batch, page)
    if card is None:
        abort(404)
    return render_template("_student_card.html", c=card)


def exam_rows() -> list[dict]:
    """La table des résultats de l'examen : une ligne par étudiant, absents
    compris, triée par nom.

    ⚠ **Unique construction**, servie à `/export.csv`, au
    `compte_rendu/notes.csv` que lisent les courriels, et à la ligne de
    commande `exam_results.py`. Les deux premiers la bâtissaient chacun de leur
    côté : deux fichiers censés dire la même chose, écrits par deux codes.
    """
    copies = list_all_copies()
    cols, thr = exam_columns(), exam_threshold()
    return exam_results.student_rows(
        copies, absent_students(copies),
        note_of=lambda c: compute_aggregate(c, cols, [], thr))


@app.route("/export.csv")
def export_csv():
    """CSV récap des notes (1 ligne / étudiant), régénéré à la volée."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(exam_results.EXPORT_HEADER)
    w.writerows(exam_results.export_csv_rows(exam_rows()))
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qcm_notes.csv"},
    )


def _pos_float(body, key, allow_zero=False):
    """Float strictement positif (ou ≥0 si allow_zero). Lève ValueError sinon."""
    v = float(body[key])
    if v < 0 or (v == 0 and not allow_zero):
        raise ValueError(key)
    return v


def _opt_signed_float(body, key):
    """Float signé optionnel : "" / None → None ; sinon float (négatif autorisé)."""
    v = body.get(key)
    if v is None or v == "":
        return None
    return float(v)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """GET → config courante ; POST → granularité + normalisation/max/poids."""
    if request.method == "POST":
        body = request.get_json(force=True)
        updates = {}
        try:
            if "hist_granularity" in body:
                g = float(body["hist_granularity"])
                if not (0.1 <= g <= 50):
                    return jsonify({"error": "granularité hors bornes (0,1–50)"}), 400
                updates["hist_granularity"] = g
            if "qcm_seuil" in body:
                # null / "" = auto → le barème du sujet (cf. qcm_normalisation).
                v = body["qcm_seuil"]
                updates["qcm_seuil"] = (None if v in (None, "")
                                        else _pos_float(body, "qcm_seuil"))
            if "qcm_max" in body:
                updates["qcm_max"] = _pos_float(body, "qcm_max")
            if "qcm_agg_weight" in body:
                updates["qcm_agg_weight"] = _pos_float(body, "qcm_agg_weight",
                                                       allow_zero=True)
            if "final_threshold" in body:
                updates["final_threshold"] = _pos_float(body, "final_threshold")
            if "pass_mark" in body:
                updates["pass_mark"] = _pos_float(body, "pass_mark", allow_zero=True)
            if "question_floor" in body:
                updates["question_floor"] = _opt_signed_float(body, "question_floor")
            if "question_ceiling" in body:
                updates["question_ceiling"] = _opt_signed_float(body, "question_ceiling")
            if "total_floor" in body:
                updates["total_floor"] = _opt_signed_float(body, "total_floor")
            if "show_score_range" in body:
                updates["show_score_range"] = bool(body["show_score_range"])
            if "anthropic_api_key" in body:
                updates["anthropic_api_key"] = str(body["anthropic_api_key"] or "").strip()
            if "ai_model" in body:
                m = str(body["ai_model"] or "").strip()
                allowed = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
                if m and m not in allowed:
                    return jsonify({"error": f"Modèle inconnu : {m}"}), 400
                updates["ai_model"] = m or "claude-sonnet-4-6"
            # Banque : la config (mode + credentials Supabase) vit dans `banks[active]`.
            # Pour modifier ces champs, utiliser /api/banks (POST/DELETE/activate)
            # et /api/bank/auth/* (login). Aucune écriture flat ici.
            col_updates = body.get("columns", [])
            if col_updates:
                files = load_config().get("grade_files", [])
                by_path = {f["path"]: f for f in files}
                for cu in col_updates:
                    fc = by_path.get(cu.get("path"))
                    if not fc:
                        continue
                    for gc in fc.get("grade_cols", []):
                        if gc.get("idx") != cu.get("idx"):
                            continue
                        if "seuil" in cu:
                            gc["seuil"] = _pos_float(cu, "seuil")
                        if "max" in cu:
                            gc["max"] = _pos_float(cu, "max")
                        if "agg_weight" in cu:
                            gc["agg_weight"] = _pos_float(cu, "agg_weight",
                                                          allow_zero=True)
                updates["grade_files"] = files
        except (TypeError, ValueError, KeyError):
            return jsonify({"error": "paramètre invalide"}), 400
        cfg = save_config(updates)
        return jsonify({"ok": True, "config": public_config(cfg)})
    return jsonify(public_config(load_config()))


@app.route("/api/save-report", methods=["POST"])
def api_save_report():
    """Crée/met à jour le dossier `compte_rendu/` : notes.csv + graphiques SVG.

    N'écrit JAMAIS dans raw_responses/ — uniquement compte_rendu/."""
    body = request.get_json(force=True)
    report_dir = ROOT / "compte_rendu"
    report_dir.mkdir(exist_ok=True)

    # notes.csv : la note de l'examen, deux fois (`QCM_brut_sur_32` et
    # `note_finale` portent la même valeur depuis qu'elle est le score brut).
    # Les colonnes de notes importées n'y sont plus : elles décrivent un
    # ensemble d'examens, et la page le signale (`legacy_grade_settings`).
    rows = exam_results.report_csv_rows(exam_rows())
    with open(report_dir / "notes.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(exam_results.REPORT_HEADER)
        w.writerows(rows)
    n_rows = len(rows)

    # graphiques : SVG fournis par le client (histogrammes + nuage de points)
    n_svg = 0
    for svg in body.get("svgs", []):
        name = secure_filename(str(svg.get("filename", "")))
        content = str(svg.get("content", ""))
        if not name.endswith(".svg") or "<svg" not in content:
            continue
        (report_dir / name).write_text(content, encoding="utf-8")
        n_svg += 1
    return jsonify({"ok": True, "dir": "compte_rendu",
                    "n_rows": n_rows, "n_svg": n_svg})


ROSTER_EXT = (".xlsx", ".xlsm", ".csv")


def _roster_pending() -> Path | None:
    """Le fichier de liste en attente de validation, s'il y en a un."""
    d = ROOT / "imports"
    for ext in ROSTER_EXT:
        p = d / f"roster_pending{ext}"
        if p.exists():
            return p
    return None


def _drop_roster_pending() -> None:
    for ext in ROSTER_EXT:
        (ROOT / "imports" / f"roster_pending{ext}").unlink(missing_ok=True)


@app.route("/api/upload-xlsx", methods=["POST"])
def api_upload_xlsx():
    """Reçoit une liste (.xlsx/.csv) et rend son ANALYSE, pas juste son en-tête.

    La modale a besoin de trois choses pour que l'utilisateur puisse trancher :
    un aperçu des premières lignes, une proposition de colonnes déduite du
    CONTENU, et la position de la 1re ligne de données. L'ancienne version ne
    rendait que la ligne 1 du fichier — donc un export commençant par un titre
    n'offrait qu'une seule « colonne », et les colonnes étaient présélectionnées
    positionnellement, sans regarder ce qu'elles contiennent.
    """
    f = request.files.get("file")
    ext = Path(f.filename or "").suffix.lower() if f is not None else ""
    if f is None or ext not in ROSTER_EXT:
        return jsonify({"error": "fichier .xlsx ou .csv attendu"}), 400
    (ROOT / "imports").mkdir(parents=True, exist_ok=True)
    _drop_roster_pending()          # jamais deux pending de formats différents
    pending = ROOT / "imports" / f"roster_pending{ext}"
    f.save(pending)
    try:
        from student_list import analyze_roster, sheet_summaries
        sheets = sheet_summaries(pending)
        # ⚠ Plusieurs onglets ⇒ on n'en analyse AUCUN : c'est à l'utilisateur de
        # dire lequel est sa promo. Analyser d'office l'onglet actif (celui
        # sélectionné au dernier enregistrement du classeur) présentait une
        # liste plausible et fausse — sur le classeur d'origine, les 165
        # étudiants du groupe FR au lieu des 39 du groupe EN, sans un mot.
        if len(sheets) > 1:
            return jsonify({"ok": True, "filename": f.filename,
                            "sheets": sheets, "needs_sheet": True})
        # ⚠ Un classeur à UN onglet n'est pas épinglé (`sheet=None`, donc
        # `xlsx_sheet=""`) : il n'y a rien à désambiguïser, et retenir le nom
        # rendrait fatal un simple renommage de l'onglet. On ne contraint que
        # là où l'ambiguïté existe.
        analysis = analyze_roster(pending, None)
    except Exception as e:          # noqa: BLE001
        _drop_roster_pending()
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    return jsonify({"ok": True, "filename": f.filename, "sheets": sheets,
                    "needs_sheet": False, **analysis})


@app.route("/api/student-list/analyze", methods=["POST"])
def api_student_list_analyze():
    """Analyse le fichier en attente sur l'onglet demandé, sans rien enregistrer.

    Permet de changer d'onglet sans re-téléverser : la détection des colonnes
    et l'aperçu portent sur l'onglet choisi.
    """
    body = request.get_json(force=True, silent=True) or {}
    pending = _roster_pending()
    if pending is None:
        return jsonify({"error": "aucun fichier envoyé"}), 400
    from student_list import analyze_roster, sheet_summaries
    try:
        analysis = analyze_roster(pending, body.get("sheet") or None)
    except Exception as e:          # noqa: BLE001
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    return jsonify({"ok": True, "sheets": sheet_summaries(pending),
                    "needs_sheet": False, **analysis})


@app.route("/api/student-list/preview", methods=["POST"])
def api_student_list_preview():
    """Ce que donnerait CE mapping de colonnes, sans rien enregistrer."""
    body = request.get_json(force=True, silent=True) or {}
    pending = _roster_pending()
    if pending is None:
        return jsonify({"error": "aucun fichier envoyé"}), 400
    from student_list import RosterError, roster_report, students_from_file
    try:
        students = students_from_file(pending, body)
    except RosterError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True,
                    **roster_report(students, len(id_columns() or []), body)})


@app.route("/api/student-list/cancel", methods=["POST"])
def api_student_list_cancel():
    """Abandon de l'import : le fichier en attente ne doit pas rester sur le disque."""
    _drop_roster_pending()
    return jsonify({"ok": True})


@app.route("/api/student-list", methods=["POST"])
def api_student_list():
    """Valide le fichier en attente + le mapping de colonnes → config.

    ⚠ Le contrôle a lieu AVANT d'écrire quoi que ce soit : une liste
    inexploitable est refusée au lieu d'être enregistrée puis annoncée « ✓ N
    étudiants » — le compte portait sur des lignes lues, pas sur des étudiants.
    """
    global _matcher_cache, _series_cache
    body = request.get_json(force=True, silent=True) or {}
    pending = _roster_pending()
    if pending is None:
        return jsonify({"error": "aucun fichier envoyé"}), 400
    from student_list import RosterError, roster_report, students_from_file
    try:
        students = students_from_file(pending, body)
    except RosterError as e:
        return jsonify({"error": str(e)}), 400
    report = roster_report(students, len(id_columns() or []), body)
    if not students:
        return jsonify({"error": "aucun étudiant lisible avec ces colonnes — "
                                 "vérifie la colonne du numéro et la 1re ligne "
                                 "de données."}), 400

    # La liste remplacée est conservée : un import raté ne doit pas détruire
    # celle qui marchait. ⚠ Y compris quand elle a une AUTRE extension — un
    # csv qui remplace un xlsx effaçait le xlsx sans en garder de copie.
    for ext in ROSTER_EXT:
        old = ROOT / f"student_list{ext}"
        if old.exists():
            old.replace(ROOT / f"student_list.prev{ext}")
    final = ROOT / f"student_list{pending.suffix}"
    pending.replace(final)
    save_config({
        "student_xlsx": final.name,
        "xlsx_id_idx": int(body.get("id_idx", -1)),
        "xlsx_nom_idx": int(body.get("nom_idx", -1)),
        "xlsx_prenom_idx": int(body.get("prenom_idx", -1)),
        "xlsx_mail_idx": int(body.get("mail_idx", -1)),
        "xlsx_data_start": int(body.get("data_start", 1)),
        # L'onglet fait partie du mapping : sans lui, la relecture repartirait
        # sur l'onglet actif du classeur, donc potentiellement une autre promo.
        "xlsx_sheet": str(body.get("sheet") or ""),
        # Les intitulés ne servent plus qu'à relire une config antérieure :
        # on les vide pour qu'ils ne puissent pas reprendre la main.
        "xlsx_id_col": "", "xlsx_nom_col": "", "xlsx_prenom_col": "",
    })
    _matcher_cache = None  # forcer le rechargement
    _series_cache = None   # la jointure des notes importées dépend du matcher
    m = get_matcher()
    return jsonify({"ok": True, "n_students": len(m.students),
                    "warnings": m.warnings(len(id_columns() or [])),
                    **report})


@app.route("/api/upload-grade-file", methods=["POST"])
def api_upload_grade_file():
    """Reçoit un .csv/.xlsx de notes, le stocke dans imports/, renvoie l'analyse."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    name = secure_filename(f.filename)
    if Path(name).suffix.lower() not in (".csv", ".xlsx", ".xlsm"):
        return jsonify({"error": "fichier .csv ou .xlsx attendu"}), 400
    ensure_imports_dir()
    dest = IMPORTS_DIR / name
    stem, suffix, k = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = IMPORTS_DIR / f"{stem}_{k}{suffix}"
        k += 1
    f.save(dest)
    # Même piège que pour la liste étudiants : sur un classeur à plusieurs
    # onglets, lire l'onglet actif rend des notes plausibles et fausses.
    sheet = (request.form.get("sheet") or "").strip() or None
    try:
        from grade_imports import list_sheets
        sheets = list_sheets(dest)
        if sheet is None and len(sheets) > 1:
            return jsonify({"ok": True, "path": f"imports/{dest.name}",
                            "sheets": [{"name": n} for n in sheets],
                            "needs_sheet": True})
        # Un seul onglet : pas d'épinglage (cf. `api_upload_xlsx`).
        rows = read_table(dest, sheet)
        analysis = analyze_table(rows, get_matcher())
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    if not analysis["ncol"]:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "fichier vide"}), 400
    analysis["rows_preview"] = [[jsonable_cell(c) for c in r] for r in rows[:14]]
    return jsonify({"ok": True, "path": f"imports/{dest.name}", "sheet": sheet,
                    "sheets": [{"name": n} for n in sheets],
                    "needs_sheet": False, "analysis": analysis})


@app.route("/api/grade-file/analyze", methods=["POST"])
def api_grade_file_analyze():
    """Ré-analyse un fichier de notes déjà déposé, sur l'onglet demandé."""
    body = request.get_json(force=True, silent=True) or {}
    path = str(body.get("path", ""))
    if not path.startswith("imports/") or ".." in path:
        return jsonify({"error": "chemin invalide"}), 400
    full = ROOT / path
    if not full.exists():
        return jsonify({"error": "fichier introuvable"}), 404
    sheet = (body.get("sheet") or "").strip() or None
    try:
        from grade_imports import list_sheets
        rows = read_table(full, sheet)
        analysis = analyze_table(rows, get_matcher())
    except Exception as e:                              # noqa: BLE001
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    analysis["rows_preview"] = [[jsonable_cell(c) for c in r] for r in rows[:14]]
    return jsonify({"ok": True, "path": path, "sheet": sheet,
                    "sheets": [{"name": n} for n in list_sheets(full)],
                    "needs_sheet": False, "analysis": analysis})


@app.route("/api/grade-file", methods=["POST"])
def api_grade_file():
    """Configure un fichier de notes : jointure (id/nom) + colonnes de notes."""
    global _series_cache
    body = request.get_json(force=True)
    path = str(body.get("path", ""))
    if not path.startswith("imports/") or ".." in path:
        return jsonify({"error": "chemin invalide"}), 400
    full = ROOT / path
    if not full.exists():
        return jsonify({"error": "fichier introuvable"}), 404
    sheet = (body.get("sheet") or "").strip() or None
    try:
        rows = read_table(full, sheet)
    except Exception as e:
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    ncol = max((len(r) for r in rows), default=0)
    nrow = len(rows)
    join_mode = "id" if body.get("join_mode") == "id" else "name"
    try:
        join_col = int(body.get("join_col"))
        data_start = int(body.get("data_start"))
    except (TypeError, ValueError):
        return jsonify({"error": "colonne ou ligne de début invalide"}), 400
    if not (0 <= join_col < ncol):
        return jsonify({"error": "colonne de jointure invalide"}), 400
    if not (0 <= data_start < nrow):
        return jsonify({"error": "ligne de début invalide"}), 400
    grade_cols, seen = [], set()
    for gc in body.get("grade_cols", []):
        try:
            idx = int(gc.get("idx"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < ncol) or idx in seen or idx == join_col:
            continue
        seen.add(idx)
        try:
            weight = float(gc.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        label = str(gc.get("label", "")).strip() or f"Colonne {idx + 1}"
        grade_cols.append({"idx": idx, "label": label, "weight": weight})
    if not grade_cols:
        return jsonify({"error": "choisis au moins une colonne de notes"}), 400
    entry = add_grade_file({"path": path, "join_mode": join_mode,
                            "sheet": sheet or "",
                            "join_col": join_col, "data_start": data_start,
                            "grade_cols": grade_cols})
    _series_cache = None
    return jsonify({"ok": True, "report": match_report(entry, get_matcher())})


@app.route("/api/grade-file/resolve", methods=["POST"])
def api_grade_file_resolve():
    """Résolution manuelle d'un nom : force un étudiant, ou ignore la ligne."""
    global _series_cache
    body = request.get_json(force=True)
    path = str(body.get("path", ""))
    raw_name = str(body.get("raw_name", ""))
    choice = body.get("student_id", "")
    if choice == "__ignore__":
        value = None
    elif choice in ("", None):
        value = ""                       # retire l'override → match auto
    else:
        if get_matcher().by_full_id(str(choice)) is None:
            return jsonify({"error": "étudiant inconnu"}), 400
        value = str(choice)
    if not set_name_override(path, raw_name, value):
        return jsonify({"error": "fichier introuvable"}), 404
    _series_cache = None
    cfg = load_config()
    fc = next((f for f in cfg.get("grade_files", []) if f.get("path") == path), None)
    return jsonify({"ok": True,
                    "report": match_report(fc, get_matcher()) if fc else {}})


@app.route("/api/grade-file/remove", methods=["POST"])
def api_grade_file_remove():
    """Retire un fichier de notes de la config (et le supprime du disque)."""
    global _series_cache
    body = request.get_json(force=True)
    ok = remove_grade_file(str(body.get("path", "")))
    _series_cache = None
    return jsonify({"ok": ok})


@app.route("/identites")
def identites():
    """Review finale : TOUTES les copies à gauche (avec leur statut d'assignation
    embarqué dans la carte) + pool des étudiants à droite (non-assignés en
    premier, puis ordre alphabétique).

    Click sur un chip vert embarqué dans une carte gauche → désassigne.
    Click sur un chip orange du pool → assigne à la carte sélectionnée.
    """
    matcher = get_matcher()
    # passe 1 : résoudre toutes les copies + capture la méthode de résolution
    # (id = grille ID auto, override = posé manuellement, name_fuzzy = HTR + fuzzy,
    # none = pas résolu)
    resolved = []
    for batch, page in list_ordered_keys():
        d = load_copy_json(batch, page)
        if d is None:
            continue
        mi = resolve_student(d, matcher)
        resolved.append((batch, page, d, mi["matched"], mi["method"]))
    # détecter les doublons : étudiants réclamés par >= 2 copies
    from collections import Counter
    claims = Counter(mt.id for (_, _, _, mt, _) in resolved if mt is not None)
    dup_ids = {sid for sid, n in claims.items() if n > 1}
    # Map id → (batch, page) pour les chips du pool droit
    assigned_to: dict[str, tuple[str, int]] = {}
    copies = []
    for batch, page, d, mt, method in resolved:
        is_dup = mt is not None and mt.id in dup_ids
        is_assigned = mt is not None and not is_dup
        if is_assigned:
            assigned_to[mt.id] = (batch, page)
        # 4 groupes UI. ⚠ « id » et « name_fuzzy » étaient réunis sous un même
        # libellé « Auto-détectés par la grille ID » — faux pour les seconds, et
        # trompeur : un rapprochement de nom manuscrit à 70 % de similarité n'a
        # pas la solidité d'un numéro lu, et c'est justement celui qu'il faut
        # regarder.
        if not is_assigned:
            group = "unresolved"
        elif method == "override":
            group = "manual"
        elif method == "id":
            group = "auto_id"
        else:                              # "name_fuzzy"
            group = "auto_name"
        copies.append({
            "batch": batch, "page": page,
            "student_id": d.get("student_id", "????"),
            "flags": d.get("_flags", []),
            "dup_of": f"{mt.nom} {mt.prenom}" if is_dup else None,
            "assigned": ({"sid": mt.id, "full": mt.full} if is_assigned else None),
            "group": group,
            "method": method,
        })
    # Pool droit : ordre = non-assignés (alpha) PUIS assignés (alpha).
    unassigned_students = []
    assigned_students = []
    for s in sorted(matcher.students, key=lambda x: (x.nom, x.prenom)):
        a = assigned_to.get(s.id)
        item = {"id": s.id, "nom": s.nom, "prenom": s.prenom, "full": s.full,
                "assigned_to": ({"batch": a[0], "page": a[1]} if a else None)}
        (assigned_students if a else unassigned_students).append(item)
    students = unassigned_students + assigned_students
    # Ordre 3 groupes : unresolved (orange) → manual (bleu) → auto (vert).
    group_order = {"unresolved": 0, "auto_name": 1, "manual": 2, "auto_id": 3}
    copies.sort(key=lambda c: (group_order[c["group"]], c["batch"], c["page"]))
    counts = {g: sum(1 for c in copies if c["group"] == g)
              for g in ("unresolved", "auto_name", "manual", "auto_id")}
    return render_template("identites.html",
                           copies=copies,
                           students=students,
                           total=counts["unresolved"],
                           n_assigned=len(assigned_to),
                           n_total=len(students),
                           n_copies=len(copies),
                           counts=counts,
                           active="identites")


@app.route("/api/set-id-digit", methods=["POST"])
def api_set_id_digit():
    """Fixe un chiffre du numéro étudiant — single-select par colonne, persisté.

    Les colonnes sont celles du calage (`id_columns()`), pas les Q32-35 figées
    d'EXAM_2026 : sur un autre sujet ce sont p.ex. [3,4,5,6].
    """
    body = request.get_json(force=True, silent=True)
    batch, page, raw_q = required(body, "batch", "page", "q")
    page = int(page)
    try:
        q = int(raw_q)
    except (TypeError, ValueError):
        return jsonify({"error": "paramètres invalides"}), 400
    char = str(body["char"])
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "copie introuvable"}), 404
    # Colonnes du calage de CETTE copie : rien ne garantit que deux versions
    # d'un sujet posent la grille aux mêmes numéros de question.
    cols = id_columns(copy_id_of(d))
    # "?" = effacer le chiffre (colonne marquée comme non-lue)
    if q not in cols or (char not in "0123456789" and char != "?"):
        return jsonify({"error": "paramètres invalides"}), 400
    n = len(cols)
    cur = d.get("student_id", "") or ""
    if "_cv_student_id" not in d:        # garder la lecture CV originale (immuable)
        d["_cv_student_id"] = cur or "?" * n
    digits = list((cur + "?" * n)[:n])
    digits[cols.index(q)] = char
    d["student_id"] = "".join(digits)
    if "manually_edited" not in d.get("_flags", []):
        d.setdefault("_flags", []).append("manually_edited")
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "student_id": d["student_id"]})


@app.route("/name_img/<batch>/<int:page>.jpg")
def name_img(batch, page):
    """Crop serré sur le nom/prénom manuscrit (zone AMC `__n`) de l'image warpée."""
    try:
        warped, _ = get_warped(batch, page)
    except FileNotFoundError:
        abort(404)
    xmin, xmax, ymin, ymax = load_name_field()
    m = 18  # marge
    x1 = max(0, int(xmin) - m); x2 = min(warped.shape[1], int(xmax) + m)
    y1 = max(0, int(ymin) - m); y2 = min(warped.shape[0], int(ymax) + m)
    crop = warped[y1:y2, x1:x2]
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        abort(500)
    return app.response_class(buf.tobytes(), mimetype="image/jpeg")


@app.route("/api/open-answer/<batch>/<int:page>/<q>.jpg")
def api_open_answer_crop(batch, page, q):
    """Crop image de la zone-réponse d'une question freeform — pour l'UI zoom.

    `q` = ordinal du bloc freeform (1-based, tel que stocké dans
    `open_answers[q]`). Zones lues dans `sujet/open_zones.json` (généré par
    `sujet_store.calibrate_open_zones`). Conversion coords PDF (72dpi) →
    canonique (300dpi) via le ratio identique à `cv_grade._grade_freeform_*`.
    """
    from sujet_store import OPEN_ZONES_JSON
    if not OPEN_ZONES_JSON.exists():
        abort(404)
    try:
        with open(OPEN_ZONES_JSON, encoding="utf-8") as f:
            zones = json.load(f)
    except Exception:
        abort(404)
    # Ordonner les bids comme cv_grade (page, ymin) puis prendre q-1 ; on
    # accepte aussi un bid direct passé en `q` pour stabilité long-terme.
    bid = None
    if q in zones:
        bid = q
    else:
        try:
            q_ord = int(q)
            sorted_bids = sorted(zones.keys(), key=lambda b: (
                zones[b].get("page", 0), zones[b].get("ymin", 0)))
            if 1 <= q_ord <= len(sorted_bids):
                bid = sorted_bids[q_ord - 1]
        except ValueError:
            pass
    if bid is None or bid not in zones:
        abort(404)
    z = zones[bid]
    try:
        warped, _ = get_warped(batch, page)
    except FileNotFoundError:
        abort(404)
    SCALE = 300.0 / 72.0
    h, w = warped.shape[:2]
    x1 = max(0, int(z["xmin"] * SCALE))
    y1 = max(0, int(z["ymin"] * SCALE))
    x2 = min(w, int(z["xmax"] * SCALE))
    y2 = min(h, int(z["ymax"] * SCALE))
    if x2 <= x1 or y2 <= y1:
        abort(404)
    crop = warped[y1:y2, x1:x2]
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        abort(500)
    return app.response_class(buf.tobytes(), mimetype="image/jpeg")


@app.route("/api/open-answer-override", methods=["POST"])
def api_open_answer_override():
    """Override la transcription HTR d'une question libre + re-match.

    Body : `{batch, page, q, raw_text}`. Le serveur recharge `expected_answer`
    / `match_mode` / `numeric_tol` / `points` depuis `open_answers[q]` (qui
    les a déjà au moment du grade), recalcule `score`, persiste, et renvoie
    `{ok, score, points}` pour MAJ live de l'UI.

    L'override remplace l'entrée HTR — la baseline immuable reste dans
    `_cv_open_answers` (cf. `seed_raw_responses`).
    """
    body = request.get_json(force=True)
    batch = body.get("batch")
    try:
        page = int(body.get("page"))
    except (TypeError, ValueError):
        return jsonify({"error": "batch/page invalides"}), 400
    q = str(body.get("q", ""))
    raw_text = str(body.get("raw_text", ""))
    d = load_copy_json(batch, page)
    if d is None or q not in d.get("open_answers", {}):
        return jsonify({"error": "open_answer introuvable"}), 404
    oa = d["open_answers"][q]
    try:
        import htr
        ok = htr.match_answer(raw_text,
                              oa.get("expected", ""),
                              mode=oa.get("match_mode", "exact"),
                              numeric_tol=float(oa.get("numeric_tol") or 0.01))
    except Exception:
        # Si htr indisponible : fallback minimal exact match
        ok = (str(raw_text).strip().lower()
              == str(oa.get("expected", "")).strip().lower())
    points = float(oa.get("points", 1.0))
    new_score = points if ok else 0.0
    oa["raw_text"] = raw_text
    oa["score"] = new_score
    oa["manually_edited"] = True
    flags = d.setdefault("_flags", [])
    if "open_answer_edited" not in flags:
        flags.append("open_answer_edited")
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "score": new_score, "points": points,
                    "match": bool(ok)})


@app.route("/api/htr/status")
def api_htr_status():
    """Status du module HTR (auto-id par nom + lecture cases libres).

    `available=false` si l'extra `[htr]` n'est pas installé → l'UI désactive
    les boutons et affiche `install_hint`.
    """
    import htr
    return jsonify(htr.status())


def _build_htr_candidates(matcher, student_id: str | None) -> list[dict]:
    """Pré-filtre la liste de 174 étudiants pour le prompt Claude (stratégie
    smart top-K).

    - Si `student_id` partial contient ≥2 digits non-`?`, narrow par préfixe :
      tous les étudiants dont les 4 derniers chiffres matchent (avec `?`
      comme wildcard). Typiquement ≤20 candidats.
    - Sinon : liste complète (~174). Coût Claude négligeable (~0.1¢ avec Haiku).
    """
    sid = (student_id or "????").strip()
    digits_known = [c for c in sid[-4:] if c.isdigit()]
    if len(digits_known) >= 2:
        pattern = sid[-4:].rjust(4, "?")
        narrowed = []
        for s in matcher.students:
            last4 = s.id[-4:]
            if all(p == "?" or p == c for p, c in zip(pattern, last4)):
                narrowed.append(s)
        if narrowed:
            return [{"id": s.id, "full": s.full} for s in sorted(
                narrowed, key=lambda x: (x.nom, x.prenom))]
    # Liste complète (toujours triée nom/prénom).
    return [{"id": s.id, "full": s.full}
            for s in sorted(matcher.students, key=lambda x: (x.nom, x.prenom))]


def _htr_recognize_one(batch: str, page: int, student_id: str | None = None,
                       wide: bool = False) -> dict:
    """Crop la zone nom + Claude vision pick + record usage.

    `wide=True` → bypass le smart top-K (utilise la liste complète des 174
    candidats). Utile en repli quand Claude répond « 0 » sur la liste réduite
    (l'étudiant n'était pas dans le narrow).

    Retourne `{best_id, best_full, confidence, raw_text, n_candidates,
    used_full_list}`. Si Claude ne sait pas (réponse 0 ou hors-range),
    `best_id=None`.
    """
    import htr
    if not htr.is_available():
        raise RuntimeError(f"htr indisponible — {htr.INSTALL_HINT}")
    warped, _ = get_warped(batch, page)
    zone = load_name_field()
    crop = htr.crop_zone(warped, zone)
    matcher = get_matcher()
    if wide:
        cands = [{"id": s.id, "full": s.full}
                 for s in sorted(matcher.students, key=lambda x: (x.nom, x.prenom))]
        used_full = True
    else:
        cands = _build_htr_candidates(matcher, student_id)
        used_full = (len(cands) == len(matcher.students))
    out = htr.recognize_name(crop, cands)
    # Compteur de tokens partagé avec l'édition assistée (dashboard widget).
    usage = out.get("usage") or {}
    if usage:
        try:
            from htr import _model_id as _htr_model
            model = _htr_model()
            cost = _api_cost_estimate(model,
                                      int(usage.get("input_tokens") or 0),
                                      int(usage.get("output_tokens") or 0))
            _record_ai_usage("api", model, usage, cost)
        except Exception:
            pass
    return {
        "best_id":        out.get("best_id"),
        "best_full":      out.get("best_full"),
        "confidence":     out.get("confidence", 0.0),
        "raw_text":       out.get("raw_text", ""),
        "n_candidates":   len(cands),
        "used_full_list": used_full,
    }


@app.route("/api/htr/recognize-name", methods=["POST"])
def api_htr_recognize_name():
    """Lit le nom manuscrit d'une copie via Claude vision + match contre une
    liste fermée de candidats étudiants (smart top-K).

    Body : `{batch, page}`. Réponse : `{ok, best_id, best_full, raw_text,
    confidence, n_candidates}`. L'utilisateur clique la suggestion pour
    valider via `/api/assign-student` (route existante, inchangée).
    """
    import htr
    if not htr.is_available():
        return jsonify({"error": "htr indisponible", "install_hint": htr.INSTALL_HINT}), 503
    body = request.get_json(force=True)
    batch = body.get("batch")
    try:
        page = int(body.get("page"))
    except (TypeError, ValueError):
        return jsonify({"error": "batch/page requis"}), 400
    wide = bool(body.get("wide"))
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "copie introuvable"}), 404
    try:
        r = _htr_recognize_one(batch, page, d.get("student_id"), wide=wide)
        return jsonify({"ok": True, **r})
    except FileNotFoundError:
        return jsonify({"error": "page introuvable"}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# Tasks HTR batch — état en mémoire (même pattern que _PIPE_TASKS).
_HTR_TASKS: dict = {}


def _htr_task_set(task_id: str, **kw):
    if task_id in _HTR_TASKS:
        _HTR_TASKS[task_id].update(kw)


def _run_htr_names(task_id: str, targets: list):
    """Worker thread : itère les copies cibles et stocke les prédictions Claude.

    `targets` : liste de `(batch, page, sid_display)`. Le résultat vit dans
    `_HTR_TASKS[task_id]["suggestions"]` : `{batch:page → {best_id, best_full,
    confidence, raw_text, student_id_read}}`.
    """
    from datetime import datetime as _dt
    try:
        total = len(targets)
        for i, (batch, page, sid_display) in enumerate(targets):
            try:
                r = _htr_recognize_one(batch, page, sid_display)
                key = f"{batch}:{page}"
                _HTR_TASKS[task_id]["suggestions"][key] = {
                    "batch": batch, "page": page,
                    "student_id_read": sid_display,
                    **r,
                }
            except Exception as e:  # noqa: BLE001
                _HTR_TASKS[task_id]["errors"].append(
                    f"{batch}/page_{page:03d}: {e}")
            _htr_task_set(task_id, done=i + 1,
                          progress=round(100 * (i + 1) / max(total, 1)))
        _htr_task_set(task_id, status="done",
                      finished_at=_dt.now().replace(microsecond=0).isoformat())
    except Exception as e:  # noqa: BLE001
        _htr_task_set(task_id, status="error", error=str(e))


@app.route("/api/htr/recognize-names-all", methods=["POST"])
def api_htr_recognize_names_all():
    """Lance le HTR sur toutes les copies sans match auto-résolu.

    Ciblage : copies dont `resolve_student()` retourne `matched=None`
    (étudiant pas trouvé via id ni via nom déjà rempli) ET sans
    `_student_override` posé manuellement.
    """
    import htr
    if not htr.is_available():
        return jsonify({"error": "htr indisponible", "install_hint": htr.INSTALL_HINT}), 503
    matcher = get_matcher()
    targets = []
    if RAW_DIR.exists():
        for batch_dir in sorted(RAW_DIR.iterdir()):
            if not batch_dir.is_dir():
                continue
            for jp in sorted(batch_dir.glob("page_*.json")):
                with open(jp, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("_student_override"):
                    continue
                if resolve_student(d, matcher)["matched"] is not None:
                    continue
                batch = batch_dir.name
                page = int(jp.stem.split("_")[1])
                targets.append((batch, page, d.get("student_id", "????")))
    import threading as _th
    import uuid as _uu
    from datetime import datetime as _dt
    task_id = _uu.uuid4().hex[:8]
    _HTR_TASKS[task_id] = {
        "status": "running",
        "started_at": _dt.now().replace(microsecond=0).isoformat(),
        "total": len(targets),
        "done": 0,
        "progress": 0,
        "suggestions": {},  # "batch:page" -> {ocr_text, confidence, candidates}
        "errors": [],
    }
    _th.Thread(target=_run_htr_names, args=(task_id, targets),
               daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "total": len(targets)})


@app.route("/api/htr/recognize-names-all/<task_id>")
def api_htr_recognize_names_all_status(task_id):
    """Polling de la task batch. Renvoie progress + suggestions accumulées."""
    t = _HTR_TASKS.get(task_id)
    if not t:
        return jsonify({"error": "task inconnue"}), 404
    return jsonify(t)


# --- Verify all : re-vérifie aussi les copies déjà assignées --------------

_HTR_VERIFY_TASKS: dict = {}


def _run_htr_verify(task_id: str, targets: list):
    """Worker thread : envoie le crop nom de TOUTES les copies à Claude (liste
    complète des 174), compare avec l'identité couramment assignée.

    `targets` : `[(batch, page, current_id, current_full), …]`. Les copies
    où Claude diverge → `mismatches`. Celles où Claude répond « 0 » →
    `unknowns` (probable problème de lecture, pas un vrai désaccord).
    """
    from datetime import datetime as _dt
    try:
        total = len(targets)
        for i, (batch, page, current_id, current_full) in enumerate(targets):
            try:
                # `wide=True` → liste complète, peu importe le student_id partial.
                r = _htr_recognize_one(batch, page, current_id, wide=True)
                key = f"{batch}:{page}"
                claude_id = r.get("best_id")
                if claude_id is None:
                    _HTR_VERIFY_TASKS[task_id]["unknowns"].append({
                        "batch": batch, "page": page,
                        "current_id": current_id, "current_full": current_full,
                        "raw_text": r.get("raw_text", ""),
                    })
                elif current_id and claude_id != current_id:
                    _HTR_VERIFY_TASKS[task_id]["mismatches"].append({
                        "batch": batch, "page": page,
                        "current_id": current_id, "current_full": current_full,
                        "claude_id": claude_id, "claude_full": r.get("best_full"),
                        "raw_text": r.get("raw_text", ""),
                    })
                elif not current_id:
                    # Cas où la copie n'avait pas de match courant — Claude
                    # propose : c'est une suggestion d'assignation.
                    _HTR_VERIFY_TASKS[task_id]["mismatches"].append({
                        "batch": batch, "page": page,
                        "current_id": None, "current_full": None,
                        "claude_id": claude_id, "claude_full": r.get("best_full"),
                        "raw_text": r.get("raw_text", ""),
                    })
            except Exception as e:  # noqa: BLE001
                _HTR_VERIFY_TASKS[task_id]["errors"].append(
                    f"{batch}/page_{page:03d}: {e}")
            _HTR_VERIFY_TASKS[task_id]["done"] = i + 1
            _HTR_VERIFY_TASKS[task_id]["progress"] = round(
                100 * (i + 1) / max(total, 1))
        _HTR_VERIFY_TASKS[task_id]["status"] = "done"
        _HTR_VERIFY_TASKS[task_id]["finished_at"] = (
            _dt.now().replace(microsecond=0).isoformat())
    except Exception as e:  # noqa: BLE001
        _HTR_VERIFY_TASKS[task_id]["status"] = "error"
        _HTR_VERIFY_TASKS[task_id]["error"] = str(e)


@app.route("/api/htr/verify-all", methods=["POST"])
def api_htr_verify_all():
    """Vérifie via Claude vision l'identité de TOUTES les copies (y compris
    celles déjà assignées). Retourne `{task_id, total}`. Polling via
    `GET /api/htr/verify-all/<task_id>`.

    Cas d'usage : on suspecte qu'une assignation auto via grille ID est
    fausse (digit mal lu), ou on veut un sanity check global avant export.
    """
    import htr
    if not htr.is_available():
        return jsonify({"error": "htr indisponible", "install_hint": htr.INSTALL_HINT}), 503
    matcher = get_matcher()
    targets = []
    if RAW_DIR.exists():
        for batch_dir in sorted(RAW_DIR.iterdir()):
            if not batch_dir.is_dir():
                continue
            for jp in sorted(batch_dir.glob("page_*.json")):
                with open(jp, encoding="utf-8") as f:
                    d = json.load(f)
                mt = resolve_student(d, matcher)["matched"]
                cur_id = mt.id if mt else None
                cur_full = mt.full if mt else None
                batch = batch_dir.name
                page = int(jp.stem.split("_")[1])
                targets.append((batch, page, cur_id, cur_full))
    import threading as _th
    import uuid as _uu
    from datetime import datetime as _dt
    task_id = _uu.uuid4().hex[:8]
    _HTR_VERIFY_TASKS[task_id] = {
        "status": "running",
        "started_at": _dt.now().replace(microsecond=0).isoformat(),
        "total": len(targets),
        "done": 0,
        "progress": 0,
        "mismatches": [],  # Claude diverge OU propose pour une copie sans match
        "unknowns":   [],  # Claude répond « 0 » (pas dans la liste)
        "errors":     [],
    }
    _th.Thread(target=_run_htr_verify, args=(task_id, targets),
               daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "total": len(targets)})


@app.route("/api/htr/verify-all/<task_id>")
def api_htr_verify_all_status(task_id):
    """Polling de la task verify. Renvoie progress + mismatches accumulés."""
    t = _HTR_VERIFY_TASKS.get(task_id)
    if not t:
        return jsonify({"error": "task inconnue"}), 404
    return jsonify(t)


@app.route("/api/assign-student", methods=["POST"])
def api_assign_student():
    """Assigne (ou retire si student_id vide) un étudiant à une copie — override d'identité."""
    body = request.get_json(force=True, silent=True)
    batch, page = required(body, "batch", "page")
    page = int(page)
    sid = str((body or {}).get("student_id", "")).strip()
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "copie introuvable"}), 404
    # student_id vide → désassignation
    if not sid:
        d.pop("_student_override", None)
        d["student_name"] = ""
        if "id_corrige" in d.get("_flags", []):
            d["_flags"].remove("id_corrige")
        save_copy_json(batch, page, d)
        return jsonify({"ok": True, "cleared": True})
    matcher = get_matcher()
    s = matcher.by_full_id(sid)
    if s is None:
        return jsonify({"error": f"étudiant {sid!r} absent de la liste"}), 400
    d["_student_override"] = s.id
    d["student_name"] = s.full
    if "id_corrige" not in d.get("_flags", []):
        d.setdefault("_flags", []).append("id_corrige")
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "name": s.full, "id": s.id})


@app.route("/student/<batch>/<int:page>")
def student(batch, page):
    d = load_copy_json(batch, page)
    if d is None:
        abort(404)
    matcher = get_matcher()
    match = resolve_student(d, matcher)
    answers_int = {int(k): v for k, v in d.get("answers", {}).items()}
    amc_answers = {int(k): v for k, v in d.get("_amc_answers", {}).items()} if d.get("_amc_answers") else None
    copy = copy_id_of(d)
    scores = score_copy(answers_int, copy=copy)
    diff_pairs = diff_set(d)
    # questions diff: {q: True si au moins 1 case en diff}
    diff_questions = {q for (q, _) in diff_pairs}
    questions = []
    for q in question_numbers(copy)[0]:
        spec = spec_of(q, copy)
        sel = sorted(answers_int.get(q, []))
        amc_sel = sorted(amc_answers.get(q, [])) if amc_answers else None
        correct = sorted(spec["correct"])
        questions.append({
            "q": q,
            "tag": spec["tag"],
            "type": spec["type"],
            "options": spec["options"],
            "correct": correct,
            "selected": sel,
            "amc_selected": amc_sel,
            "score": scores["per_question"][q],
            "has_diff": q in diff_questions,
        })
    nav = neighbors(batch, page)

    # Overlay ronds magenta : 1 cercle par case-réponse, en coords canoniques
    # (mêmes que l'image servie par /img_canon/). Click → toggle l'état.
    # On centre chaque rond sur la position CORRIGÉE de la case (canonique +
    # offset par-question calculé par compute_per_question_offsets) — sinon
    # les ronds dérivent légèrement quand le scan a une distorsion non-planaire.
    # Layout de la copie de cette feuille (mapping case↔lettre per-copy).
    lay = layout_store.get_layout(copy=copy)
    canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
    qcm_qs, id_qs = question_numbers(copy)
    qcm_set, id_set = set(qcm_qs), set(id_qs)
    # Une image = UNE feuille : n'y poser que les ronds des cases qu'elle porte,
    # et les mesurer sur CETTE image. Avec plusieurs feuilles, les autres cases
    # tomberaient à des positions qui ne correspondent à rien ici. `?sheet=N`
    # choisit la feuille affichée (0 = la première).
    sheets = copy_sheets(d, batch, page)
    idx = request.args.get("sheet", type=int) or 0
    shown = sheets[idx] if 0 <= idx < len(sheets) else sheets[0]
    try:
        _warped, offsets = get_warped(shown["batch"], shown["page"], copy)
    except Exception:
        offsets = {}
    sid = (d.get("student_id") or "?" * len(id_qs))
    # ⚠ Sur l'image, le magenta plein veut dire « lu comme coché ». Une case
    # SIGNALÉE mais non cochée — 78 % des signalements sur EXAM_2026 — n'avait
    # donc aucune marque : elle n'existait que dans la grille de zoom, où le
    # même magenta veut dire « à relire ». D'où le halo pointillé, distinct du
    # rond plein, qui disparaît dès que la case est traitée.
    open_cells = review_open_cells(d)
    cells = []
    for b in lay.sheet_boxes(page=shown.get("sheet_page")):
        dx, dy = offsets.get(b.question, (0, 0))
        common = {
            "q": b.question, "char": b.char,
            "cx": (b.xmin + b.xmax) / 2.0 + dx,
            "cy": (b.ymin + b.ymax) / 2.0 + dy,
            "r": 0.45 * min(b.xmax - b.xmin, b.ymax - b.ymin),
        }
        if b.question in qcm_set:
            cells.append({**common, "kind": "qcm",
                          "doubt": (b.question, b.char) in open_cells,
                          "selected": b.char in answers_int.get(b.question, [])})
        elif b.question in id_set:
            col_idx = id_qs.index(b.question)   # position dans les colonnes du calage
            cur = sid[col_idx] if 0 <= col_idx < len(sid) else "?"
            cells.append({**common, "kind": "id",
                          "selected": b.char == cur})

    # Grille zoom (cases recadrées) embarquée dans le panel droit
    zoom_questions = build_zoom_questions(d)
    review = copy_review(d)

    # Liste de TOUTES les copies pour la sidebar (mêmes données que le dashboard)
    sidebar_copies = list_all_copies()

    return render_template("student.html",
                           batch=batch, page=page, data=d,
                           match=match, scores=scores, questions=questions, nav=nav,
                           cells=cells, canon_w=canon_w, canon_h=canon_h,
                           zoom_questions=zoom_questions, review=review,
                           sidebar_copies=sidebar_copies,
                           sheets=sheets, shown_sheet=shown,
                           active="")


@app.route("/api/order")
def api_order():
    return jsonify([{"batch": b, "page": p} for (b, p) in list_ordered_keys()])


@app.route("/flagged")
def flagged():
    """Review rapide : les doutes, du plus ambigu au moins ambigu.

    - **Un bloc = une question, toutes ses cases sur une ligne.** Le liseré
      magenta dit la décision courante, le `?` orange le doute de l'algorithme
      (cf. *Code couleur*). Un découpage cochées / non cochées en deux colonnes
      a été essayé puis retiré : la colonne de droite était le plus souvent
      vide, et la question à se poser se lit déjà sur la case elle-même.
    - **Aucune notion de « traité ».** Elle a été retirée sur retour d'usage :
      elle ajoutait un second état à suivre (traité / pas traité) par-dessus le
      seul qui compte ici (douteux / pas douteux), et un bouton « tout traiter »
      dont l'effet — vider la file sans rien décider — n'était pas lisible.
      Corriger une case reste enregistré comme une décision humaine
      (`_reviewed_cells` via `/api/toggle`), ce dont `build_dataset` a besoin.
      ⚠ Conséquence assumée : la file ne se vide pas toute seule. Un doute
      légitime qu'on choisit de laisser tel quel y reste.
    - **Trié par ambiguïté décroissante** (`sort=amb`, défaut ; `scan` pour
      l'ordre de scan) : le maximum sur les cases signalées, pas la somme —
      cinq doutes tièdes ne doivent pas passer devant un vrai doute.

    ⚠ **Une copie dont seule l'identité pose question n'entre pas dans l'onglet
    Réponses**, elle n'a rien à y montrer — c'est ce qui remplissait la liste de
    copies sans une seule case à regarder. Elle est dans l'onglet Identité, qui
    existe pour ça, et le bandeau d'identité reste affiché sur les copies qui
    figurent dans les deux.
    """
    matcher = get_matcher()
    students = []
    for batch, page in list_ordered_keys():
        d = load_copy_json(batch, page)
        if d is None:
            continue
        rev = copy_review(d)
        match = resolve_student(d, matcher)
        canon = match["matched"]
        id_st = id_state(d, match)
        id_bad = id_st is not None
        if not rev["items"] and not id_bad:
            continue
        validated = "validated" in d.get("_flags", [])
        n_open = rev["n_open"] + (1 if id_bad else 0)   # == copy_open_count(d)
        students.append({
            "batch": batch, "page": page,
            "student_id": d.get("student_id", "????"),
            "canonical_name": f"{canon.nom} {canon.prenom}" if canon else "?",
            "canonical_id": canon.id if canon else "",
            # ⚠ pas la clé « items » : dans un template Jinja, `s.items` résout
            # la MÉTHODE du dict avant la clé, et la boucle explose en
            # « 'builtin_function_or_method' object is not iterable ».
            "questions": rev["items"],   # déjà triées par ambiguïté décroissante
            "ambiguity": rev["ambiguity"],
            # ⚠ Deux comptes distincts : celui des réponses seules (affiché
            # à côté du nombre de copies à regarder — les mélanger annonçait
            # « 42 signalements » pour 4 questions et 38 identités) et celui
            # qui pilote la file, identité comprise.
            "n_cell_flags": rev["n_flagged"],
            "n_flagged": rev["n_flagged"] + (1 if id_bad else 0),
            "n_open": n_open,
            "id_issue": id_bad, "id_state": id_st,
            "risk": rev["risk"] + (1.0 if id_bad else 0.0),
            "validated": validated,
            # ⚠ `validated` ne suffit pas : « relue en entier » parle des
            # réponses, pas de l'identité. Une copie relue dont le numéro reste
            # illisible a encore quelque chose à traiter — c'est justement le
            # cas qui n'apparaissait nulle part.
            "done": n_open == 0,
            "flags": d.get("_flags", []),
            "id_questions": build_id_questions(d),
        })

    sort_mode = request.args.get("sort", "amb")
    if sort_mode == "amb":
        students.sort(key=lambda s: (-s["ambiguity"], -s["risk"],
                                     s["batch"], s["page"]))
    totals = {
        "flagged": sum(s["n_cell_flags"] for s in students),
        "answers": sum(1 for s in students if s["questions"]),
        "id": sum(1 for s in students if s["id_issue"]),
    }
    return render_template("flagged.html", students=students, totals=totals,
                           sort_mode=sort_mode, active="flagged")


def build_zoom_questions(d: dict) -> list:
    """Construit la liste `questions` (cases en 2 zones) pour la grille de zoom."""
    answers_int = {int(k): set(v) for k, v in d.get("answers", {}).items()}
    diff_pairs = diff_set(d)
    doubt_pairs = doubt_set(d)
    seen = review_state.reviewed_cells(d)
    copy = copy_id_of(d)
    questions = []
    for q in question_numbers(copy)[0]:
        spec = spec_of(q, copy)
        sel = answers_int.get(q, set())
        cases = []
        for ch in spec["options"]:
            cases.append({
                "char": ch,
                "selected": ch in sel,
                "correct": ch in spec["correct"],
                "diff": (q, ch) in diff_pairs,
                "doubtful": (q, ch) in doubt_pairs,
                "reviewed": review_state.cell_key(q, ch) in seen,
            })
        questions.append({
            "q": q,
            "tag": spec["tag"],
            "type": spec["type"],
            "correct": "".join(spec["correct"]),
            "cases": cases,
        })
    return questions


def build_id_questions(d: dict) -> list:
    """Colonnes du numéro étudiant (cf. `id_columns()`) : 10 cases-chiffres
    par colonne, le chiffre lu surligné."""
    sid = d.get("student_id", "") or ""
    cols = []
    for i, q in enumerate(question_numbers(copy_id_of(d))[1]):
        read = sid[i] if i < len(sid) else "?"
        cases = [{"char": str(dg), "selected": str(dg) == read} for dg in range(10)]
        cols.append({"q": q, "pos": i + 1, "read": read, "cases": cases})
    return cols


@app.route("/student/<batch>/<int:page>/zoom")
def zoom(batch, page):
    d = load_copy_json(batch, page)
    if d is None:
        abort(404)
    questions = build_zoom_questions(d)
    id_questions = build_id_questions(d)
    nav = neighbors(batch, page)
    return render_template("zoom.html",
                           batch=batch, page=page, data=d, questions=questions,
                           id_questions=id_questions, nav=nav, active="")


@app.route("/api/copy/<batch>/<int:page>")
def api_copy(batch, page):
    d = load_copy_json(batch, page)
    if d is None:
        abort(404)
    return jsonify(d)


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    body = request.get_json(force=True, silent=True)
    batch, page, q, char = required(body, "batch", "page", "q", "char")
    page, q = int(page), str(q)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    # Une question hors du calage écrirait une entrée fantôme dans
    # `answers` — c'est la source de vérité de la relecture. Le contrôle porte
    # sur le calage de CETTE copie : les questions valides diffèrent d'une
    # version du sujet à l'autre.
    if not q.isdigit() or int(q) not in set(question_numbers(copy_id_of(d))[0]):
        return jsonify({"error": f"question inconnue : {q}"}), 400
    if char not in spec_of(int(q), copy_id_of(d))["options"]:
        return jsonify({"error": f"lettre hors options : {char}"}), 400
    ans = d.get("answers", {}).get(q, [])
    if char in ans:
        ans = [c for c in ans if c != char]
    else:
        # respecter l'ordre des options de cette copie
        opts = spec_of(int(q), copy_id_of(d))["options"]
        ans = [c for c in opts if c in (set(ans) | {char})]
    d.setdefault("answers", {})[q] = ans
    # marquer comme modifié manuellement
    if "manually_edited" not in d.get("_flags", []):
        d.setdefault("_flags", []).append("manually_edited")
    # Basculer une case EST la décision du relecteur : elle sort de la file
    # sans clic supplémentaire. (Marqué explicitement plutôt que déduit de
    # `answers` ≠ `_cv_answers` : un aller-retour reviendrait à l'état CV et
    # ferait réapparaître une case qu'on vient pourtant d'examiner.)
    _mark_reviewed_cells(d, [(int(q), char)])
    save_copy_json(batch, page, d)
    rev = copy_review(d)
    return jsonify({"ok": True, "answers": d["answers"],
                    "n_open": copy_open_count(d)})


def _mark_reviewed_cells(d: dict, pairs, reviewed: bool = True) -> None:
    """Ajoute (ou retire) des cases de `_reviewed_cells`, en place."""
    cur = [str(k) for k in (d.get("_reviewed_cells") or [])]
    keys = [review_state.cell_key(q, ch) for q, ch in pairs]
    if reviewed:
        cur += [k for k in keys if k not in cur]
    else:
        cur = [k for k in cur if k not in set(keys)]
    d["_reviewed_cells"] = cur


def _mark_reviewed_questions(d: dict, qs, reviewed: bool = True) -> None:
    cur = [int(q) for q in (d.get("_reviewed_questions") or [])]
    qs = [int(q) for q in qs]
    if reviewed:
        cur += [q for q in qs if q not in cur]
    else:
        cur = [q for q in cur if q not in set(qs)]
    d["_reviewed_questions"] = cur


def _check_qcm_cell(d: dict, q, char=None):
    """Valide (q, char) contre le calage. Lève ValueError sinon.

    Une question hors calage écrirait un état de relecture fantôme, qui
    survivrait à toutes les recorrections sans jamais correspondre à rien.
    """
    if not str(q).isdigit() or int(q) not in set(question_numbers(copy_id_of(d))[0]):
        raise ValueError(f"question inconnue : {q}")
    q = int(q)
    if char is not None:
        if char not in spec_of(q, copy_id_of(d))["options"]:
            raise ValueError(f"lettre hors options : {char}")
    return q


@app.route("/api/review-cell", methods=["POST"])
def api_review_cell():
    """Marque une case signalée comme traitée — sans toucher à la réponse.

    C'est l'action qui manquait : « j'ai regardé, la lecture est bonne ». Sans
    elle, seule une correction faisait sortir une case de la file, donc
    confirmer le CV était impossible et la file ne décroissait jamais.
    """
    body = request.get_json(force=True, silent=True)
    batch, page, q, char = required(body, "batch", "page", "q", "char")
    page = int(page)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    q = _check_qcm_cell(d, q, char)
    reviewed = bool(body.get("reviewed", True))
    _mark_reviewed_cells(d, [(q, char)], reviewed)
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "reviewed": reviewed,
                    "n_open": copy_open_count(d)})


@app.route("/api/review-question", methods=["POST"])
def api_review_question():
    """Marque le signalement de structure d'une question comme traité."""
    body = request.get_json(force=True, silent=True)
    batch, page, q = required(body, "batch", "page", "q")
    page = int(page)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    q = _check_qcm_cell(d, q)
    reviewed = bool(body.get("reviewed", True))
    _mark_reviewed_questions(d, [q], reviewed)
    # Traiter la question, c'est aussi répondre pour les cases qu'elle signale.
    rev = copy_review(d)
    for it in rev["items"]:
        if it["q"] == q:
            _mark_reviewed_cells(d, [(q, c["char"]) for c in it["cells"]
                                     if c["flagged"]], reviewed)
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "reviewed": reviewed,
                    "n_open": copy_open_count(d)})


@app.route("/api/review-copy", methods=["POST"])
def api_review_copy():
    """Marque TOUT ce qui reste signalé sur une copie comme traité.

    ⚠ Ce n'est PAS `validated` : ça dit « les cases signalées sont traitées »,
    pas « j'ai relu la copie entière ». La confusion des deux avait un coût
    concret — `build_dataset` prend une copie `validated` comme vérité terrain
    sur ses ~150 cases, y compris celles que personne n'a ouvertes.
    """
    body = request.get_json(force=True, silent=True)
    batch, page = required(body, "batch", "page")
    page = int(page)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    rev = copy_review(d)
    _mark_reviewed_cells(d, [(it["q"], c["char"]) for it in rev["items"]
                             for c in it["cells"] if c["flagged"]])
    _mark_reviewed_questions(d, [it["q"] for it in rev["items"]
                                 if it["reason"] is not None])
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "n_open": copy_open_count(d)})


@app.route("/api/review-identity", methods=["POST"])
def api_review_identity():
    """Confirme l'identité d'une copie dont le numéro est illisible.

    Ne change rien à l'attribution : elle dit seulement que quelqu'un a regardé
    le nom manuscrit et l'a jugé conforme. Sans ce geste, la copie resterait
    indéfiniment dans la file — ou, comme avant, n'y entrerait jamais.
    """
    body = request.get_json(force=True, silent=True)
    batch, page = required(body, "batch", "page")
    page = int(page)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    reviewed = bool(body.get("reviewed", True))
    if reviewed:
        d["_reviewed_id"] = True
    else:
        d.pop("_reviewed_id", None)
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "reviewed": reviewed,
                    "n_open": copy_open_count(d)})


@app.route("/api/mark_validated", methods=["POST"])
def api_mark_validated():
    """Pose (ou retire) `validated` = « copie relue en entier ».

    ⚠ `value: false` existe parce qu'un clic de trop était irréparable depuis
    l'interface : le drapeau ne s'ajoutait que, et le bouton se désactivait —
    il fallait éditer le JSON à la main.
    """
    body = request.get_json(force=True, silent=True)
    batch, page = required(body, "batch", "page")
    page = int(page)
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    value = body.get("value", True)
    flags = [f for f in d.get("_flags", []) if f != "validated"]
    if value:
        flags.append("validated")
    d["_flags"] = flags
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "validated": bool(value)})


@app.route("/img/<batch>/<int:page>.jpg")
def img(batch, page):
    p = PAGES_DIR / safe_batch(batch) / f"page_{page:03d}.jpg"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/jpeg")


@app.route("/img_canon/<batch>/<int:page>.jpg")
def img_canon(batch, page):
    """Image warpée dans l'espace canonique (mires alignées, mêmes coords que `layout_store.Box`).

    Sert pour l'overlay SVG de la vue copie (ronds magenta sur les cases cochées).
    Cache disque, invalidé au mtime du JPEG source."""
    src = PAGES_DIR / safe_batch(batch) / f"page_{page:03d}.jpg"
    if not src.exists():
        abort(404)
    cache_dir = zoom_cache_dir(batch, page)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "canon.jpg"
    if cache_path.exists() and cache_path.stat().st_mtime >= src.stat().st_mtime:
        return send_file(cache_path, mimetype="image/jpeg")
    warped, _ = get_warped(batch, page)
    cv2.imwrite(str(cache_path), warped, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return send_file(cache_path, mimetype="image/jpeg")


@app.route("/zoom_img/<batch>/<int:page>/<q>_<char>.jpg")
def zoom_img(batch, page, q, char):
    # `q` et `char` entrent dans un nom de fichier : les valider AVANT de
    # construire le chemin (sinon `char="../.."` sort du cache).
    if not str(q).isdigit() or not (len(str(char)) == 1 and str(char).isalnum()):
        abort(404)
    src = PAGES_DIR / safe_batch(batch) / f"page_{page:03d}.jpg"
    cache_dir = zoom_cache_dir(batch, page)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"Q{q}_{char}.jpg"
    # Invalidation au mtime de la page source : une ré-extraction des PDF doit
    # produire de nouveaux crops, pas resservir les anciens.
    if (cache_path.exists() and src.exists()
            and cache_path.stat().st_mtime >= src.stat().st_mtime):
        return send_file(cache_path, mimetype="image/jpeg")

    # ⚠ Le calage dépend de la COPIE : avec des versions du sujet, la case
    # `10_A` n'existe pas dans le calage de la copie 1. Sans ça, toutes les
    # vignettes des copies de la seconde version répondaient 404 — les cases
    # s'affichaient vides dans la review rapide et le zoom.
    d = load_copy_json(batch, page)
    copy = copy_id_of(d) if d is not None else 1
    layout, by_qchar = get_layout(copy)
    key = (int(q), char)
    if key not in by_qchar:
        abort(404)
    b = by_qchar[key]
    # Copie à plusieurs feuilles : la case peut être sur une AUTRE image que
    # celle du JSON de la copie. On résout l'image qui la porte réellement.
    if d is not None:
        s = sheet_of_question(d, batch, page).get(int(q))
        if s is not None and (s["batch"], s["page"]) != (batch, page):
            batch, page = s["batch"], s["page"]
            src = PAGES_DIR / safe_batch(batch) / f"page_{page:03d}.jpg"
            cache_dir = zoom_cache_dir(batch, page)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"Q{q}_{char}.jpg"
            if (cache_path.exists() and src.exists()
                    and cache_path.stat().st_mtime >= src.stat().st_mtime):
                return send_file(cache_path, mimetype="image/jpeg")
    warped, offsets = get_warped(batch, page, copy)
    dx, dy = offsets.get(int(q), (0, 0))
    pad = 12
    x1 = max(0, int(b.xmin) + dx - pad)
    x2 = min(warped.shape[1], int(b.xmax) + dx + pad)
    y1 = max(0, int(b.ymin) + dy - pad)
    y2 = min(warped.shape[0], int(b.ymax) + dy + pad)

    crop = warped[y1:y2, x1:x2]
    # upscale 2x pour lisibilité
    h, w = crop.shape[:2]
    crop = cv2.resize(crop, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    cv2.imwrite(str(cache_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return send_file(cache_path, mimetype="image/jpeg")


# --------------------------------------------------------------------------
# Onglet « Sujet » : édition LaTeX d'exam.tex + compilation
# --------------------------------------------------------------------------

@app.route("/banque")
def banque_page():
    """Onglet Banque : gestion (local/online, login OTP, profil, sync stats)
    + browse compact des questions de la banque active.

    Le browse complet avec import reste dans la modale 📚 Banque de l'onglet
    Sujet (workflow rapide pendant l'édition).
    """
    cfg = load_config()
    return render_template(
        "banque.html",
        cfg=cfg,
        active="banque",
        app_name="AMCx",
        active_project_name=project_state.display_name(config.project_root()),
    )


@app.route("/sujet")
def sujet_page():
    """Onglet Sujet : édition LaTeX d'exam.tex + bandeau global de config.

    Le template est piloté par le **Subject canonique** (`parse_subject()`) :
    chaque bloc (text / question_qcm / question_open) a un `bid` stable.
    En mode `legacy` (sujet écrit à la main, EXAM_2026) seuls les blocs
    `question_qcm` sont exposés et le CRUD est restreint à l'édition.
    """
    sub = parse_subject()
    cfg = sub["config"]
    # Enrichissement de chaque bloc : mapping lettre AMC + max_score.
    # Indexation par ordre des blocs `question_qcm` (q=1,2,…) — cohérent avec
    # parse_tex(). `preview_q` (clé de région) vient de `block_preview_keys()`.
    qs_by_order = parse_tex()
    # Clé de région par bloc — MÊME helper que `/sujet/regions.json`, pour que
    # `data-preview-q` et le champ `bid` des régions ne puissent pas diverger.
    preview_keys = block_preview_keys(sub["blocks"])
    q_seq = 0          # numéro de QCM (1,2,…) pour le mapping lettre/barème
    # Rang du QCM **dans sa version** : c'est ce que l'étudiant voit sur sa
    # copie. Le rang global (`q`) reste la clé de `parse_tex`/du barème, mais
    # afficher « Q6 » sur la 1re question de l'après-midi n'a aucun sens — cette
    # question est imprimée « Question 1 » sur le sujet de l'après-midi.
    q_in_group = qcm_rank_in_version(sub["blocks"])
    enriched = []
    for b in sub["blocks"]:
        item = {"bid": b.bid, "kind": b.kind, "data": b.data, "group": b.group}
        if b.kind == "question_qcm":
            q_seq += 1
            info = qs_by_order.get(q_seq, {})
            item["q"] = q_seq
            item["q_in_version"] = q_in_group[b.bid]
            item["answers_with_char"] = info.get("answers", [])
            item["max"] = sujet_max_score(q_seq)
        elif b.kind in ("question_open", "question_freeform"):
            item["max"] = float(b.data.get("points") or 0.0)
        if b.bid in preview_keys:
            item["preview_q"] = preview_keys[b.bid]
        enriched.append(item)

    pdf = SUJET_DIR / "DOC-sujet.pdf"
    try:
        available_copies = list(layout_store.get_available_copies())
    except Exception:
        available_copies = []
    _cfg = load_config()
    # Plancher/plafond du barème : ils changent le SCORE (`score.py` les
    # applique), et c'est le bandeau du sujet qui les commande depuis que
    # l'évaluation n'a plus de réglage.
    score_defaults = {
        "floor": _cfg.get("question_floor"),
        "ceiling": _cfg.get("question_ceiling"),
        "total_floor": _cfg.get("total_floor"),
        "show_range": bool(_cfg.get("show_score_range")),
    }
    # Versions (sujet à groupes) : chacune avec sa plage de numéros de copie et
    # son propre total de barème. Le total du sujet entier n'aurait aucun sens
    # ici — une copie du matin ne porte que les questions du matin.
    # Une seule implémentation, partagée avec les routes de version : deux
    # calculs séparés finiraient par diverger sur le sort des blocs communs.
    versions = _versions_payload()

    return render_template(
        "sujet.html",
        blocks=enriched,
        versions=versions,
        version_names={v["group"]: (v["name"] or v["group"]) for v in versions},
        common_group=sujet_common_group,
        config=cfg,                                  # SubjectConfig (num_copies, seed, shuffle_*)
        # ⚠ Avec des versions, `cfg.header` n'est JAMAIS rendu : chaque
        # `\exemplaire` imprime le sien (`_render_multi_version_subject`). Le
        # formulaire éditait donc un en-tête qui n'apparaissait sur aucune
        # copie. On lui donne celui de la première version, et le sélecteur
        # ci-dessous laisse passer d'une version à l'autre.
        header=(cfg.versions[0].header if cfg.versions else cfg.header),
        version_headers={v.vid: v.header.__dict__.copy() for v in cfg.versions},
        answer_sheet=cfg.answer_sheet,               # AnswerSheetConfig
        answer_sheet_tex=cfg.answer_sheet_tex,       # LaTeX brut (prime sur les champs si non vide)
        mode=sub["mode"],                            # 'canonical' | 'legacy' | 'empty'
        available_copies=available_copies,
        total_max=sujet_total_max(),
        score_defaults=score_defaults,               # défauts globaux plancher/plafond (placeholders)
        has_pdf=pdf.exists(),
        pdf_mtime=int(pdf.stat().st_mtime) if pdf.exists() else 0,
        active="sujet",
    )


@app.route("/sujet/pdf")
def sujet_pdf():
    """PDF du sujet (recompilable), affiché *inline* dans un onglet à part."""
    p = SUJET_DIR / "DOC-sujet.pdf"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf")


@app.route("/sujet/publication/<kind>.pdf")
def sujet_publication_pdf(kind):
    """Sujet vierge / corrigé à publier. Produit par `POST /api/sujet/publication`.

    Servi *inline* : on ouvre le document pour le relire avant de le diffuser.
    """
    if kind not in PUBLICATION_KINDS:
        abort(404)
    p = PUBLICATION_PDF[kind]
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf",
                     download_name=f"{kind}.pdf")


@app.route("/api/sujet/publication", methods=["POST"])
def api_sujet_publication():
    """Produit le PDF de publication demandé. Renvoie {ok, log, n_pages, url}.

    ⚠ On recompile à chaque demande plutôt que de servir le dernier fichier :
    un corrigé périmé part chez les étudiants sans que rien ne le signale, et
    quelques secondes de pdflatex coûtent moins cher que ça.
    """
    kind = (request.get_json(force=True) or {}).get("kind")
    if kind not in PUBLICATION_KINDS:
        return jsonify({"error": f"type de publication inconnu : {kind!r}"}), 400
    result = compile_publication(kind)
    result["kind"] = kind
    result["label"] = PUBLICATION_LABEL[kind]
    p = PUBLICATION_PDF[kind]
    if result.get("ok") and p.exists():
        result["url"] = (f"/sujet/publication/{kind}.pdf"
                         f"?v={int(p.stat().st_mtime)}")
    return jsonify(result)


@app.route("/api/sujet/save", methods=["POST"])
def api_sujet_save():
    """Réécrit dans exam.tex les blocs des questions éditées (LaTeX brut)."""
    body = request.get_json(force=True)
    updates = body.get("questions")
    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "rien à sauvegarder"}), 400
    clean = []
    for u in updates:
        try:
            answers = []
            for a in u.get("answers", []):
                a = a if isinstance(a, dict) else {"text": a}
                answers.append({"text": str(a.get("text", "")),
                                "correct": bool(a.get("correct")),
                                "bareme": str(a.get("bareme", ""))})
            tag = str(u.get("tag", "")).strip()
            if not tag:
                return jsonify({"error": "identifiant (tag) de question vide"}), 400
            clean.append({
                "q": int(u["q"]),
                "tag": tag,
                "type": "mult" if u.get("type") == "mult" else "single",
                "env": "reponseshoriz" if u.get("env") == "reponseshoriz" else "reponses",
                "statement": str(u.get("statement", "")),
                "answers": answers,
                "value": str(u.get("value", "")),
            })
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "requête invalide"}), 400
    try:
        save_questions(clean)
    except KeyError as e:
        return jsonify({"error": f"question inconnue : {e}"}), 404
    except Exception as e:
        return jsonify({"error": f"échec de la sauvegarde : {e}"}), 400
    return jsonify({"ok": True, "total_max": sujet_total_max(),
                    "max": {q: sujet_max_score(q) for q in parse_tex()}})


@app.route("/api/sujet/bareme-examples", methods=["POST"])
def api_sujet_bareme_examples():
    """Génère le bloc LaTeX « Barème » (explication + exemples chiffrés) à partir
    des structures de barème du sujet, clampé au plancher/plafond globaux.

    Renvoie `{ok, tex}` — l'UI insère le tex (éditable) dans les instructions."""
    cfg = load_config()
    floor = cfg.get("question_floor")
    ceiling = cfg.get("question_ceiling")
    try:
        sub = parse_subject()
        tex = render_bareme_examples(sub, floor=floor, ceiling=ceiling, n=2)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Génération impossible : {e}"}), 500
    return jsonify({"ok": True, "tex": tex})


@app.route("/api/sujet/compile", methods=["POST"])
def api_sujet_compile():
    """Recompile exam.tex → sujet/DOC-sujet.pdf. Renvoie {ok, log, pdf_mtime}."""
    result = compile_pdf()
    if result.get("ok"):
        # Nouveau calage → les caches de géométrie et les images warpées
        # deviennent obsolètes.
        invalidate_layout_caches()
    pdf = SUJET_DIR / "DOC-sujet.pdf"
    result["pdf_mtime"] = int(pdf.stat().st_mtime) if pdf.exists() else 0
    return jsonify(result)


# --------------------------------------------------------------------------
# CRUD canonique du sujet (Phase 3) — lit/écrit `sujet/exam.tex`
#
# Toutes les routes /api/sujet/blocks/* exigent le mode canonique
# (`sujet_store.is_canonical(tex)`) ; en mode legacy elles renvoient HTTP 409.
# Les routes /api/sujet/config et /api/sujet/regenerate-seed marchent même
# en mode legacy (patch regex sur \exemplaire et \AMCrandomseed).
# --------------------------------------------------------------------------

def _json_body() -> dict:
    body = request.get_json(silent=True, force=True) or {}
    if not isinstance(body, dict):
        return {}
    return body


def _crud_error(e: Exception, code_default: int = 400):
    """Mappe les exceptions sujet_store → HTTP : PermissionError=409, KeyError=404."""
    if isinstance(e, PermissionError):
        return jsonify({"error": str(e), "mode": "legacy"}), 409
    if isinstance(e, KeyError):
        return jsonify({"error": f"bid inconnu : {e}"}), 404
    if isinstance(e, ValueError):
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": str(e)}), code_default


@app.route("/api/sujet")
def api_sujet():
    """État complet du sujet : {config, blocks, mode, available_copies}."""
    try:
        sub = parse_subject()
        out = subject_to_dict(sub)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        out["available_copies"] = list(layout_store.get_available_copies())
    except Exception:
        out["available_copies"] = []
    try:
        out["total_max"] = sujet_total_max()
        out["max"] = {q: sujet_max_score(q) for q in parse_tex()}
    except Exception:
        out["total_max"] = 0.0
        out["max"] = {}
    # Anomalies à remonter à l'utilisateur : store illisible (édition perdue)
    # et sujet désaccordé du calage (notes potentiellement fausses).
    warnings = pop_store_warnings()
    try:
        warnings += check_layout_consistency(verbose=False)
    except Exception:
        pass
    out["warnings"] = warnings
    return jsonify(out)


@app.route("/api/sujet/config", methods=["POST"])
def api_sujet_config():
    """Patch SubjectConfig (num_copies, random_seed, shuffle_*)."""
    try:
        sujet_update_config(_json_body())
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/header", methods=["POST"])
def api_sujet_header():
    """Patch HeaderBlock — refusé en mode legacy."""
    try:
        sujet_update_header(_json_body())
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/versions/update", methods=["POST"])
def api_sujet_version_update():
    """Patch d'une version : `{vid, name?, num_copies?, header?}`.

    Le groupe AMC n'est pas modifiable ici — cf. `sujet_store.update_version`.
    """
    body = _json_body()
    vid = str(body.get("vid") or "")
    if not vid:
        return jsonify({"error": "vid manquant"}), 400
    try:
        v = sujet_update_version(vid, body)
        rows = _versions_payload()
        row = next((r for r in rows if r["vid"] == vid), None) or {}
        return jsonify({"ok": True, "version": v,
                        "first_copy": row.get("first_copy"),
                        "last_copy": row.get("last_copy"),
                        "total_max": row.get("total_max"),
                        "versions": rows,
                        "ranges": [{"vid": r["vid"],
                                    "first_copy": r["first_copy"],
                                    "last_copy": r["last_copy"]} for r in rows]})
    except Exception as e:
        return _crud_error(e)


def _versions_payload():
    """Plages de copies + barème par version, recalculés pour TOUTES les versions.

    Changer une version décale les numéros imprimés de toutes les suivantes
    (AMC numérote en continu) : le front ne peut pas les deviner.
    """
    sub = parse_subject()
    cfg = sub["config"]
    ranges = version_copy_ranges(cfg)
    # ⚠ Un bloc sans groupe est **commun** : il est imprimé sur chaque version.
    # Il compte donc dans le nombre de QCM de toutes, pas d'aucune.
    n_by_group: dict = {}
    for b in sub["blocks"]:
        if b.kind == "question_qcm":
            n_by_group[b.group] = n_by_group.get(b.group, 0) + 1
    n_common = n_by_group.get("", 0)
    # ⚠ Le barème d'une version se calcule depuis SES QUESTIONS, jamais depuis
    # le numéro de sa première copie. Le lire par `total_max(first_copy)` le
    # faisait disparaître de l'après-midi dès qu'on changeait le nombre de
    # copies du matin : le décalage sortait sa première copie du calage
    # compilé. Il ne dépend pas non plus d'une compilation — une version tout
    # juste créée affiche son barème.
    return [{"vid": v.vid, "name": v.name, "group": v.group,
             "num_copies": v.num_copies, "first_copy": a, "last_copy": z,
             "n_qcm": n_by_group.get(v.group, 0) + n_common,
             "header_raw": v.header.raw_tex,
             "total_max": sujet_version_total_max(sub, v.group)}
            for v, (a, z) in zip(cfg.versions, ranges)]


@app.route("/api/sujet/versions/add", methods=["POST"])
def api_sujet_version_add():
    """Ajoute une version. Body: `{name?, group?, num_copies?, vid?, index?, header?}`.

    `vid` / `group` / `index` servent à l'annulation d'une suppression ; ils
    remettent la version à sa place avec son identité d'origine.
    ⚠ Sur un sujet à une seule version, l'appel en crée **deux** (cf.
    `sujet_store.add_version`) : le front doit relire la liste rendue.
    """
    body = _json_body()
    try:
        v = sujet_add_version(name=body.get("name") or "",
                              group=body.get("group") or None,
                              num_copies=body.get("num_copies") or 1,
                              vid=body.get("vid") or None,
                              index=body.get("index"),
                              header=body.get("header") or None)
        return jsonify({"ok": True, "version": v, "versions": _versions_payload()})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/versions/delete", methods=["POST"])
def api_sujet_version_delete():
    """Supprime une version. Body: `{vid, mode?}` → `{ok, undo, versions}`.

    `mode` : `reparent` (défaut — les questions deviennent communes, aucune
    n'est supprimée) ou `delete_blocks`. `undo` est à renvoyer tel quel à
    `/api/sujet/versions/restore`.
    """
    body = _json_body()
    vid = str(body.get("vid") or "")
    if not vid:
        return jsonify({"error": "vid manquant"}), 400
    try:
        undo = sujet_delete_version(vid, mode=str(body.get("mode") or "reparent"))
        return jsonify({"ok": True, "undo": undo, "versions": _versions_payload()})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/versions/restore", methods=["POST"])
def api_sujet_version_restore():
    """Annule une suppression de version : `{undo}` tel que rendu par delete."""
    body = _json_body()
    try:
        v = sujet_restore_version(body.get("undo") or {})
        return jsonify({"ok": True, "version": v, "versions": _versions_payload()})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/blocks/set-group", methods=["POST"])
def api_sujet_block_set_group():
    """Affecte un bloc à une version : `{bid, group}` → `{ok, previous}`.

    `group` vide = bloc commun (imprimé sur toutes les versions). `previous`
    permet au front d'empiler l'annulation.
    """
    body = _json_body()
    bid = str(body.get("bid") or "")
    if not bid:
        return jsonify({"error": "bid manquant"}), 400
    try:
        prev = sujet_set_block_group(bid, body.get("group") or "")
        return jsonify({"ok": True, "previous": prev,
                        "versions": _versions_payload()})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/header/analyze", methods=["POST"])
def api_sujet_header_analyze():
    """Propose une décomposition d'un en-tête brut : `{raw_tex}` →
    `{ok, fields, leftovers}`.

    ⚠ **N'écrit rien.** Le LaTeX vient du champ de saisie, pas du store, pour
    que l'analyse porte sur ce que l'utilisateur a sous les yeux — modifications
    non enregistrées comprises. C'est lui qui applique, après avoir vu.
    """
    body = _json_body()
    try:
        r = sujet_analyze_header(str(body.get("raw_tex") or ""))
        # ⚠ Le verdict de l'analyse s'appelle `complete`, pas `ok` : `ok` dit
        # déjà que la requête a abouti, et les confondre ferait passer « je n'ai
        # rien su décomposer » pour une panne de serveur.
        return jsonify({"ok": True, "complete": r["ok"],
                        "fields": r["fields"], "leftovers": r["leftovers"]})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/header/to-raw", methods=["POST"])
def api_sujet_header_to_raw():
    """Rend en LaTeX ce que les champs structurés produiraient : `{fields}` →
    `{raw_tex}`. **N'écrit rien** — c'est le front qui applique, pour que
    l'opération passe par la même écriture que le reste et soit annulable.

    Les champs viennent du formulaire (éditions non enregistrées comprises),
    pas du store : l'utilisateur fige ce qu'il a sous les yeux.
    """
    body = _json_body()
    try:
        fields = {k: v for k, v in (body.get("fields") or {}).items()
                  if k in HeaderBlock.__dataclass_fields__}
        fields["raw_tex"] = ""          # on veut le rendu des CHAMPS
        return jsonify({"ok": True,
                        "raw_tex": sujet_header_to_raw(HeaderBlock(**fields))})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/answer-sheet/to-raw", methods=["POST"])
def api_sujet_answer_sheet_to_raw():
    """Idem pour la feuille de réponses : `{fields, num_copies?}` → `{tex}`.

    ⚠ `num_copies` par défaut = celui du sujet, pas 1 : le rendu en dépend
    (grille de numéro de copie), et figer celui d'un tirage à une copie
    donnerait une feuille qui ne correspond pas.
    """
    body = _json_body()
    try:
        cfg = parse_subject()["config"]
        fields = {k: v for k, v in (body.get("fields") or {}).items()
                  if k in AnswerSheetConfig.__dataclass_fields__}
        base = {**cfg.answer_sheet.__dict__, **fields}
        n = body.get("num_copies")
        n = int(n) if n else max(1, int(cfg.num_copies or 1))
        return jsonify({"ok": True,
                        "tex": sujet_answer_sheet_to_raw(AnswerSheetConfig(**base),
                                                         num_copies=n)})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/answer-sheet", methods=["POST"])
def api_sujet_answer_sheet():
    """Patch AnswerSheetConfig — refusé en mode legacy."""
    try:
        sujet_update_answer_sheet(_json_body())
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/regenerate-seed", methods=["POST"])
def api_sujet_regenerate_seed():
    """Pose un nouveau seed aléatoire. Marche aussi en mode legacy."""
    try:
        seed = sujet_regenerate_seed()
        return jsonify({"ok": True, "seed": seed})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/blocks/add", methods=["POST"])
def api_sujet_blocks_add():
    """Ajoute un bloc. Body: {kind, after_bid?, at_start?, data?, bid?, group?, restore?} → {bid}.

    `bid` sert à l'annulation d'une suppression : réutiliser l'identifiant
    d'origine garde le lien avec le calage compilé (cf. `add_block`).
    `group` restaure la version d'appartenance — sans lui, un bloc restauré
    serait imprimé dans TOUTES les versions."""
    body = _json_body()
    kind = body.get("kind", "")
    after_bid = body.get("after_bid")
    data = body.get("data") or None
    want_bid = body.get("bid") or None
    group = body.get("group") or None
    # `at_start` : réinsertion en tête (cf. `add_block`, conventions opposées de
    # `after_bid`). `restore` : réinsertion d'un bloc supprimé, y compris d'un
    # kind désactivé — sinon la suppression serait irréversible.
    at_start = bool(body.get("at_start"))
    restore = bool(body.get("restore"))
    try:
        bid = sujet_add_block(kind, after_bid=after_bid, data=data, bid=want_bid,
                              group=group, at_start=at_start, restore=restore)
        return jsonify({"ok": True, "bid": bid})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/import-tex", methods=["POST"])
def api_sujet_import_tex():
    """Importe les questions d'un fichier `.tex` (legacy ou canonique) dans le
    sujet du projet actif. Append à la fin — les blocs existants sont intacts.

    Multipart : `file=<exam.tex>`. Renvoie `{ok, added, skipped, skipped_reasons}`.
    409 si le sujet actif est en mode legacy (faut migrer d'abord)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Fichier .tex manquant."}), 400
    try:
        raw = f.read()
        content = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return jsonify({"error": f"Lecture du fichier : {e}"}), 400
    if not content.strip():
        return jsonify({"error": ".tex vide ou non reconnu."}), 400
    try:
        parsed = parse_subject(tex=content)
    except Exception as e:
        return jsonify({"error": f"Parse .tex : {e}"}), 400
    if parsed.get("mode") == "empty":
        return jsonify({"error": ".tex vide ou non reconnu."}), 400

    # `question_freeform` exclu : sa création est désactivée
    # (cf. sujet_store.DISABLED_KINDS). Les blocs de ce type d'un .tex
    # importé sont comptés dans `skipped`, pas rejetés en erreur.
    IMPORTABLE = {"question_qcm", "question_open"}
    added, skipped = 0, 0
    skipped_kinds: dict[str, int] = {}
    for b in parsed.get("blocks", []):
        if b.kind not in IMPORTABLE:
            skipped += 1
            skipped_kinds[b.kind] = skipped_kinds.get(b.kind, 0) + 1
            continue
        try:
            sujet_add_block(b.kind, after_bid=None, data=b.data)
            added += 1
        except PermissionError:
            return jsonify({
                "error": ("Le sujet de ce projet est en mode legacy. "
                          "Migre-le d'abord vers le format canonique "
                          "(onglet Sujet → bouton 🔥 Migrer)."),
                "legacy": True,
            }), 409
        except Exception as e:
            return jsonify({
                "error": f"Échec ajout d'un bloc {b.kind} : {e}",
                "added_before_error": added,
            }), 500
    return jsonify({"ok": True, "added": added, "skipped": skipped,
                    "skipped_kinds": skipped_kinds,
                    "source_mode": parsed.get("mode", "")})


@app.route("/api/sujet/blocks/delete", methods=["POST"])
def api_sujet_blocks_delete():
    """Supprime un bloc. Body: {bid}."""
    bid = _json_body().get("bid", "")
    try:
        sujet_delete_block(bid)
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/blocks/move", methods=["POST"])
def api_sujet_blocks_move():
    """Déplace un bloc. Body: {bid, after_bid|null}."""
    body = _json_body()
    try:
        sujet_move_block(body.get("bid", ""), body.get("after_bid"))
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/blocks/update", methods=["POST"])
def api_sujet_blocks_update():
    """Met à jour le `data` d'un bloc. Body: {bid, data}.

    Autorisé en mode legacy pour les blocs `question_qcm` (délégué à
    `save_questions`)."""
    body = _json_body()
    try:
        sujet_update_block(body.get("bid", ""), body.get("data") or {})
        return jsonify({"ok": True})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/blocks/duplicate", methods=["POST"])
def api_sujet_blocks_duplicate():
    """Duplique un bloc juste après lui. Body: {bid} → {new_bid}."""
    bid = _json_body().get("bid", "")
    try:
        new_bid = sujet_duplicate_block(bid)
        return jsonify({"ok": True, "bid": new_bid})
    except Exception as e:
        return _crud_error(e)


@app.route("/api/sujet/migrate-to-canonical", methods=["POST"])
def api_sujet_migrate():
    """Migre un sujet legacy vers le format canonique.

    Crée un backup `sujet/exam.tex.legacy-backup`. **Opération non destructive
    mais irréversible côté UI** : pour rollback, l'utilisateur doit copier
    le `.legacy-backup` à la main par-dessus `exam.tex`.

    ⚠ Si le sujet a déjà des copies scannées et corrigées (comme EXAM_2026),
    la migration peut désaligner les positions de cases du fait de l'ajout des
    blocs `text`. À utiliser SEULEMENT sur un sujet en cours d'élaboration.
    """
    try:
        r = sujet_migrate_to_canonical()
        if not r.get("ok"):
            return jsonify({"error": r.get("log", "erreur inconnue")}), 400
        return jsonify(r)
    except Exception as e:
        return _crud_error(e)


# --------------------------------------------------------------------------
# Banque de questions partageable (Phase 1 MVP)
#
# Stockage local sous `~/Documents/AMCx-banque/` (override env `AMCX_BANK_DIR`).
# 1 fichier JSON = 1 question. Routes : list/load/save/delete/import.
# Voir auto_grading/bank.py pour le modèle et les conversions Block↔question.
# --------------------------------------------------------------------------

def _bank_author_default() -> str:
    """Auteur par défaut : email connecté à la banque active (online), sinon
    header.author du sujet courant, sinon vide."""
    entry = config.active_bank_cfg()
    if entry.get("type") == "online" and entry.get("user_email"):
        return entry["user_email"]
    try:
        sub = parse_subject()
        return (sub["config"].header.author or "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Multi-banques : CRUD sur le dict `banks` + switch d'active_bank
# --------------------------------------------------------------------------

def _slugify_bank(name: str) -> str:
    """Kebab-case ascii, max 40 chars. Pour clé `banks[<slug>]`."""
    import re as _re
    import unicodedata as _ud
    s = _ud.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    s = _re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s or "banque")[:40]


def _bank_summary(slug: str, entry: dict) -> dict:
    """Vue allégée d'une banque (pour la liste UI). Pas de tokens, pas de clé."""
    typ = entry.get("type", "local")
    out = {
        "slug":   slug,
        "name":   entry.get("name") or slug,
        "type":   typ,
    }
    if typ == "local":
        out["path"] = entry.get("path") or ""
    else:
        out["supabase_url"] = entry.get("supabase_url") or ""
        out["user_email"]   = entry.get("user_email") or ""
        out["logged_in"]    = bool(entry.get("user_token"))
    return out


@app.route("/api/banks")
def api_banks_list():
    """Liste les banques configurées + la banque active."""
    cfg = load_config()
    banks = cfg.get("banks") or {}
    items = [_bank_summary(slug, e) for slug, e in banks.items()]
    items.sort(key=lambda b: b["name"].lower())
    return jsonify({
        "ok":     True,
        "active": config.active_bank_slug(),
        "banks":  items,
    })


@app.route("/api/banks", methods=["POST"])
def api_banks_create():
    """Crée une banque. Body : {name, type:'local'|'online', path? |
    supabase_url?, supabase_anon_key?}.

    Refuse si un slug équivalent existe déjà. Online : entry créée sans
    tokens (l'user doit ensuite login via /api/bank/auth/*)."""
    body = _json_body()
    name = (body.get("name") or "").strip()
    typ  = (body.get("type") or "local").strip()
    if not name:
        return jsonify({"error": "Nom requis."}), 400
    if typ not in ("local", "online"):
        return jsonify({"error": f"type invalide : {typ}"}), 400

    entry: dict = {"name": name, "type": typ}
    if typ == "local":
        path = (body.get("path") or "").strip()
        if not path:
            return jsonify({"error": "Chemin du dossier requis pour une banque locale."}), 400
        entry["path"] = str(Path(path).expanduser())
    else:
        url = (body.get("supabase_url") or "").strip().rstrip("/")
        anon = (body.get("supabase_anon_key") or "").strip()
        if not (url and anon):
            return jsonify({"error": "URL Supabase + clé anon requis pour une banque en ligne."}), 400
        entry["supabase_url"] = url
        entry["supabase_anon_key"] = anon

    cfg = load_config()
    banks = dict(cfg.get("banks") or {})
    slug = _slugify_bank(name)
    base_slug = slug
    n = 2
    while slug in banks:
        slug = f"{base_slug}-{n}"
        n += 1
    banks[slug] = entry
    save_config({"banks": banks})
    return jsonify({"ok": True, "slug": slug, "bank": _bank_summary(slug, entry)})


@app.route("/api/banks/<slug>", methods=["DELETE"])
def api_banks_delete(slug):
    """Supprime une banque. Le contenu sur disque/Supabase n'est PAS touché —
    seule la référence dans AMCx disparaît. Si c'était la banque active,
    repointe vers la 1ère banque restante (ou recrée un default vide)."""
    cfg = load_config()
    banks = dict(cfg.get("banks") or {})
    if slug not in banks:
        return jsonify({"error": f"banque inconnue : {slug}"}), 404
    del banks[slug]
    updates: dict = {"banks": banks}
    if cfg.get("active_bank") == slug:
        new_active = next(iter(banks), "default")
        if new_active not in banks:
            # Plus aucune banque : recrée le default minimal local
            banks[new_active] = {
                "name": "Banque par défaut", "type": "local",
                "path": str(Path.home() / "Documents" / "AMCx-banque"),
            }
            updates["banks"] = banks
        updates["active_bank"] = new_active
    save_config(updates)
    return jsonify({"ok": True, "active": updates.get("active_bank", cfg.get("active_bank"))})


@app.route("/api/banks/<slug>/activate", methods=["POST"])
def api_banks_activate(slug):
    """Switche la banque active. Le serveur reste up — au prochain request
    le dispatcher `_bank()` lira la nouvelle banque."""
    cfg = load_config()
    banks = cfg.get("banks") or {}
    if slug not in banks:
        return jsonify({"error": f"banque inconnue : {slug}"}), 404
    save_config({"active_bank": slug})
    return jsonify({"ok": True, "active": slug, "bank": _bank_summary(slug, banks[slug])})


@app.route("/api/bank")
def api_bank_list():
    """Liste les questions de la banque.

    Query : `?kind=&q=&tags=t1,t2&author=&category=<uuid>&descendants=0|1
    &uncategorized=1`. `descendants` vaut 1 par défaut : cliquer sur un
    chapitre montre tout son sous-arbre.
    """
    args = request.args
    tags = [t.strip() for t in (args.get("tags") or "").split(",") if t.strip()]
    filters = {
        "kind":   args.get("kind", ""),
        "q":      args.get("q", ""),
        "tags":   tags,
        "author": args.get("author", ""),
        # Catégories (les deux backends)
        "category":      (args.get("category") or "").strip(),
        "descendants":   args.get("descendants", "1") not in ("0", "false", "no"),
        "uncategorized": args.get("uncategorized") in ("1", "true", "yes"),
        # Phase B (online only — ignored by bank.py local)
        "mes_favoris": args.get("mes_favoris") in ("1", "true", "yes"),
        "mon_tag":     args.get("mon_tag", ""),
        "status":      args.get("status", ""),
        # Variantes : repliées par défaut (`heads`), `all` rend la liste à plat.
        "variants":    (args.get("variants") or "heads").strip(),
    }
    try:
        report: dict = {}
        items = _bank().list_questions(filters, report=report)
        # ⚠ Cette route ne renvoie PLUS `all_tags`. Le calculer exigeait un
        # second parcours complet de la banque à CHAQUE frappe dans la
        # recherche — deux requêtes HTTP complètes en banque en ligne. Les
        # facettes (tags + arbre) sont servies une fois par `/api/bank/facets`,
        # à l'ouverture de la modale.
        #
        # ⚠ `truncated` est rendu même quand il vaut False : une liste
        # incomplète qui se présente comme complète est pire qu'une erreur.
        return jsonify({"ok": True, "items": items,
                        "truncated": bool(report.get("truncated"))})
    except tx.TaxonomyError as e:
        return jsonify({"error": str(e)}), 400
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>")
def api_bank_load(bank_id):
    """Charge une question complète (avec data)."""
    try:
        return jsonify({"ok": True, "question": _bank().load(bank_id)})
    except KeyError:
        return jsonify({"error": f"question inconnue : {bank_id}"}), 404
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------
# Édition d'une question de banque : rendu identique à un bloc Sujet
# --------------------------------------------------------------------------

def _frac(s) -> float:
    s = str(s or "").strip()
    if not s:
        return 0.0
    if "/" in s:
        a, b = s.split("/", 1)
        try: return float(a) / float(b)
        except (ValueError, ZeroDivisionError): return 0.0
    try: return float(s)
    except ValueError: return 0.0


def _bank_question_max(kind: str, data: dict) -> float:
    """Calcule le `max` d'une question banque (sans dépendre du sujet/AMC).

    Reproduit la logique de score.py / sujet_store.max_score() :
    - single QCM    → `value` (float, défaut 1)
    - mult   QCM    → Σ bareme des réponses correctes (≥ 0)
    - open / freeform → `points`
    - text / answerbox → 0
    """
    data = data or {}
    if kind == "question_qcm":
        if data.get("qtype") == "mult":
            return sum(_frac(a.get("bareme")) for a in (data.get("answers") or [])
                       if a.get("correct"))
        return _frac(data.get("value") or 1)
    if kind in ("question_open", "question_freeform"):
        try: return float(data.get("points") or 0)
        except (TypeError, ValueError): return 0.0
    return 0.0


def _bank_to_sujet_block(question: dict) -> dict:
    """Convertit une question de banque → dict `b` au format attendu par
    `_sujet_block.html` (mêmes champs que les blocs renvoyés par
    `parse_subject()`). Pas de `q` (numéro), pas de `preview_q`, pas de
    `answers_with_char` (les lettres AMC ne sont définies que dans le
    contexte d'un sujet).
    """
    kind = question.get("kind", "")
    data = question.get("data") or {}
    return {
        "bid":  question.get("bank_id", ""),
        "kind": kind,
        "data": data,
        "q":    None,
        "max":  _bank_question_max(kind, data),
        "answers_with_char": [],
        "preview_q": None,
    }


@app.route("/api/bank/<bank_id>/block-html")
def api_bank_block_html(bank_id):
    """Retourne le HTML d'un bloc sujet pour une question de banque.

    Render exact du partiel `_sujet_block.html` avec `mode='canonical'` →
    l'édition (textareas, badges bonne/mauvaise, ans-add/remove, etc.) est
    disponible côté UI. Le DOM est strictement identique à un bloc Sujet.
    """
    try:
        q = _bank().load(bank_id)
    except KeyError:
        return jsonify({"error": f"question inconnue : {bank_id}"}), 404
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    b = _bank_to_sujet_block(q)
    html = render_template("_sujet_block.html", b=b, mode="canonical")
    return Response(html, mimetype="text/html")


@app.route("/api/bank/<bank_id>/save-data", methods=["POST"])
def api_bank_save_data(bank_id):
    """Met à jour le `data` d'une question de banque (édition depuis l'UI).

    Body : `{data, title?, tags?}`. Préserve les autres champs (auteur,
    created_at, source_project, stats…).
    """
    body = _json_body()
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return jsonify({"error": "data doit être un objet"}), 400
    title = body.get("title")
    tags = body.get("tags")
    b = _bank()
    try:
        if b is bank_online:
            updated = b.update_question_content(bank_id, data,
                                                 title=title, tags=tags,
                                                 bump_version=True)
        else:
            # bank.py local : update via load+save (le module ne fournit
            # pas d'update dédié — on patche les champs en place).
            q = b.load(bank_id)
            q["data"] = data
            if title is not None: q["title"] = title.strip()
            if tags is not None:  q["tags"]  = [t.strip() for t in tags if t and t.strip()]
            q["modified_at"] = __import__("datetime").datetime.now().replace(
                microsecond=0).isoformat()
            q["version"] = int(q.get("version", 1)) + 1
            b.save(q)
            updated = q
        new_max = _bank_question_max(updated.get("kind", ""), updated.get("data") or {})
        return jsonify({"ok": True, "bank_id": bank_id,
                        "version": updated.get("version"),
                        "max": round(new_max, 4)})
    except KeyError:
        return jsonify({"error": f"question inconnue : {bank_id}"}), 404
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank", methods=["POST"])
def api_bank_save():
    """Sauve un bloc du sujet courant dans la banque.

    Body: {bid, title, tags:[str], author?, categories:[uuid]}. Le bloc est lu
    dans le sujet actif, ses champs internes sont strippés, un bank_id frais
    est généré.
    """
    body = _json_body()
    bid = (body.get("bid") or "").strip()
    title = (body.get("title") or "").strip()
    tags = body.get("tags") or []
    author = (body.get("author") or "").strip() or _bank_author_default()
    categories = body.get("categories") or []
    if not bid:
        return jsonify({"error": "bid manquant"}), 400
    try:
        sub = parse_subject()
        block = next((b for b in sub["blocks"] if b.bid == bid), None)
        if block is None:
            return jsonify({"error": f"bloc introuvable : {bid}"}), 404
        proj = project_state.display_name(config.project_root())
        b = _bank()
        q = b.from_block(block, project_name=proj, title=title,
                         tags=tags, author=author,
                         categories=_checked_categories(b, categories))
        saved = b.save(q)
        # save() local retourne un Path, online retourne la question. On
        # uniformise : prend l'id depuis ce qui est disponible.
        bid = (saved or {}).get("bank_id") if isinstance(saved, dict) else q.get("bank_id")
        return jsonify({"ok": True, "bank_id": bid, "title": q["title"]})
    except (tx.TaxonomyError, KeyError, NotImplementedError) as e:
        # Une catégorie inconnue doit dire 404 avec son id, pas un 400 opaque.
        return _cat_error(e)
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/bank/<bank_id>", methods=["DELETE"])
def api_bank_delete(bank_id):
    """Supprime définitivement une question de la banque."""
    try:
        _bank().delete(bank_id)
        return jsonify({"ok": True})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------
# Catégories de la banque (arbre partagé) — voir BANK_CATEGORIES_PLAN.md
#
# Contrairement aux routes Phase B (ratings, tags persos), les catégories ne
# sont PAS réservées au backend en ligne : classer ses questions est un besoin
# de base, qui doit marcher sur une banque locale, sans compte ni réseau.
# --------------------------------------------------------------------------

def _cat_backend():
    """Backend actif, s'il sait gérer les catégories. Un backend qui ne les
    implémente pas encore répond 501 plutôt que de lever un AttributeError
    opaque."""
    b = _bank()
    if not hasattr(b, "list_categories"):
        raise NotImplementedError(
            "Les catégories ne sont pas encore disponibles sur ce type de banque.")
    return b


def _cat_error(e: Exception):
    """Exceptions de l'arbre → HTTP. Conflit d'invariant (cycle, profondeur,
    doublon entre frères, suppression d'un nœud non vide) = 409 : la requête
    est bien formée, c'est l'état de l'arbre qui la refuse."""
    if isinstance(e, tx.TaxonomyConflict):
        return jsonify({"error": str(e), "conflict": True}), 409
    if isinstance(e, tx.TaxonomyError):
        return jsonify({"error": str(e)}), 400
    if isinstance(e, KeyError):
        return jsonify({"error": f"identifiant inconnu : {e}"}), 404
    if isinstance(e, NotImplementedError):
        return jsonify({"error": str(e)}), 501
    if isinstance(e, bank_online.BankAuthError):
        return jsonify({"error": str(e), "auth_required": True}), 401
    if isinstance(e, ValueError):
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": str(e)}), 500


def _checked_categories(b, cat_ids) -> list:
    """Valide une liste d'affectations contre l'arbre du backend `b`.

    Un id mal formé est refusé (400) et un id absent de l'arbre aussi (404) :
    accepter silencieusement produirait une question classée nulle part, que
    l'utilisateur croirait rangée.
    """
    ids = [str(c or "") for c in (cat_ids or [])]
    if not ids:
        return []
    if not hasattr(b, "list_categories"):
        raise NotImplementedError(
            "Les catégories ne sont pas encore disponibles sur ce type de banque.")
    known = {n["id"] for n in b.list_categories()}
    out: list = []
    for c in ids:
        if not tx.is_valid_cat_id(c):
            raise tx.TaxonomyError(f"identifiant de catégorie invalide : {c!r}")
        if c not in known:
            raise KeyError(c)
        if c not in out:
            out.append(c)
    return out


@app.route("/api/bank/categories")
def api_bank_categories_list():
    """Arbre aplati en ordre préfixe : `{nodes, max_depth, can_edit}`.

    Chaque nœud porte `depth`, `path`, `n_direct`, `n_total` — l'UI n'a aucun
    calcul d'arbre à refaire.
    """
    try:
        b = _cat_backend()
        return jsonify({
            "ok":        True,
            "nodes":     b.list_categories(),
            "max_depth": tx.MAX_DEPTH,
            "can_edit":  (config.active_bank_cfg().get("type") != "online"
                          or bank_online.is_logged_in()),
        })
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/categories", methods=["POST"])
def api_bank_categories_create():
    """Crée un nœud. Body : `{name, parent_id?, position?}`."""
    body = _json_body()
    try:
        node = _cat_backend().create_category(
            body.get("name"),
            (body.get("parent_id") or None),
            body.get("position"))
        return jsonify({"ok": True, "node": node})
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/categories/<cat_id>", methods=["PATCH"])
def api_bank_categories_update(cat_id):
    """Renomme / déplace / réordonne. Body : `{name?, parent_id?, position?}`.

    ⚠ `parent_id` **absent** = ne pas toucher au parent ; `parent_id: null` =
    remonter à la racine. D'où la sentinelle plutôt qu'un simple `.get()`.
    """
    body = _json_body()
    try:
        kwargs = {}
        if "name" in body:
            kwargs["name"] = body.get("name")
        if "parent_id" in body:
            kwargs["parent_id"] = body.get("parent_id") or None
        if body.get("position") is not None:
            kwargs["position"] = int(body["position"])
        node = _cat_backend().update_category(cat_id, **kwargs)
        return jsonify({"ok": True, "node": node})
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/categories/<cat_id>", methods=["DELETE"])
def api_bank_categories_delete(cat_id):
    """Supprime un nœud. `?mode=refuse` (défaut) renvoie 409 s'il n'est pas
    vide ; `?mode=reparent` remonte enfants et questions au parent. Aucune
    question n'est jamais supprimée."""
    mode = (request.args.get("mode") or "refuse").strip()
    try:
        return jsonify({"ok": True, **_cat_backend().delete_category(cat_id, mode)})
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/categories/<cat_id>/assign", methods=["POST"])
def api_bank_categories_assign(cat_id):
    """Affecte un lot de questions à une catégorie.

    Body : `{bank_ids:[…]}` — ou `{tag:"proba"}` pour reprendre toutes les
    questions portant ce tag public (promotion **opt-in** d'un tag en
    catégorie ; les tags ne sont jamais convertis automatiquement, sinon
    l'arbre se remplirait d'étiquettes de niveau et de difficulté).
    `{remove: true}` retire au lieu d'ajouter. Idempotent.
    """
    body = _json_body()
    try:
        b = _cat_backend()
        ids = body.get("bank_ids")
        tag = (body.get("tag") or "").strip()
        if ids is None and tag:
            ids = [q.get("bank_id") for q in b.list_questions({"tags": [tag]})]
        n = b.assign_category(cat_id, ids or [], remove=bool(body.get("remove")))
        return jsonify({"ok": True, "n": n})
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/<bank_id>/categories", methods=["GET", "PUT"])
def api_bank_question_categories(bank_id):
    """GET → catégories vivantes d'une question ; PUT `{categories:[uuid]}` →
    remplace (comme les tags persos).

    Ne passe **pas** par `/api/bank/<id>/save-data`, qui incrémente `version` :
    classer une question n'est pas la modifier.
    """
    try:
        b = _cat_backend()
        if request.method == "PUT":
            cats = b.set_question_categories(
                bank_id, _checked_categories(b, _json_body().get("categories")))
        else:
            cats = b.get_question_categories(bank_id)
        return jsonify({"ok": True, "categories": cats})
    except Exception as e:
        return _cat_error(e)


def _var_backend():
    """Le backend, s'il sait gérer les variantes — sinon 501, pas un
    `AttributeError` opaque (même contrat que `_cat_backend`)."""
    b = _bank()
    if not hasattr(b, "list_variants"):
        raise NotImplementedError(
            "Les variantes ne sont pas encore disponibles sur ce type de banque.")
    return b


@app.route("/api/bank/<bank_id>/variants", methods=["GET", "POST"])
def api_bank_variants(bank_id):
    """GET → le groupe de variantes de cette question ; POST `{head_id}` →
    l'y range, `{head_id: null}` → l'en sort.

    La réponse rend **l'état réel du groupe après écriture**, jamais ce qui a
    été demandé : attacher une question qui avait elle-même des variantes
    fusionne les deux groupes, et la page doit afficher ce qu'elle a obtenu.
    """
    try:
        b = _var_backend()
        if request.method == "POST":
            body = _json_body()
            head = body.get("head_id")
            grp = b.set_variant_of(bank_id, None if head in (None, "") else str(head))
        else:
            grp = b.list_variants(bank_id)
        return jsonify({"ok": True, **grp})
    except Exception as e:
        return _cat_error(e)


@app.route("/api/bank/facets")
def api_bank_facets():
    """Facettes de navigation : `{all_tags, nodes, max_depth, can_edit}`.

    Chargée à l'ouverture et après chaque mutation de l'arbre, elle évite de
    reparcourir toute la banque à chaque frappe dans la recherche.
    """
    try:
        b = _bank()
        # ⚠ `variants: "all"` : la liste par défaut replie les variantes sous
        # leur chef, donc un tag porté par la seule variante disparaîtrait des
        # facettes — et la case à cocher qui le retrouverait n'existerait pas.
        all_tags = {t for q in b.list_questions({"variants": "all"})
                    for t in (q.get("tags") or [])}
        nodes = b.list_categories() if hasattr(b, "list_categories") else []
        return jsonify({
            "ok":        True,
            "all_tags":  sorted(all_tags),
            "nodes":     nodes,
            "max_depth": tx.MAX_DEPTH,
            "can_edit":  (config.active_bank_cfg().get("type") != "online"
                          or bank_online.is_logged_in()),
        })
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _sync_bank_stats() -> dict:
    """Recalcule depuis raw_responses/ les stats des questions de la banque
    importées dans le sujet courant. Idempotent (remplace l'entrée par projet,
    pas d'incrément). Skip les blocs question_open / answerbox (pas de note
    auto). Renvoie un résumé `{updated, skipped, total_copies, project}`.
    """
    sub = parse_subject()
    if sub.get("mode") != "canonical":
        # On peut quand même tenter avec parse_tex (legacy), mais on n'a pas
        # de moyen de retrouver `_bank_id` car en legacy `data` n'a pas ce
        # champ. Donc rien à sync.
        return {"updated": 0, "skipped": 0, "total_copies": 0,
                "msg": "Sujet en mode legacy : aucune trace de banque."}

    # Numéro de question = position du bloc parmi les QCM en ordre document.
    # `parse_tex()` numérote dans le même ordre (1..N), donc cette indexation
    # par-bloc coïncide avec les clés `answers["1".."N"]` des raw_responses.
    # Une question importée plusieurs fois dans le même sujet est traitée
    # comme plusieurs instances dont les stats sont sommées.
    instances: list[tuple[str, int]] = []
    qcm_idx = 0
    for b in sub["blocks"]:
        if b.kind != "question_qcm":
            continue
        qcm_idx += 1
        bid_bank = (b.data or {}).get("_bank_id")
        if bid_bank:
            instances.append((bid_bank, qcm_idx))
    if not instances:
        return {"updated": 0, "skipped": 0, "total_copies": 0,
                "msg": "Aucune question importée depuis la banque dans ce sujet."}

    # Itère toutes les copies, accumule par bank_id (somme sur instances).
    bank_ids_in_play = {bid for bid, _ in instances}
    accum: dict[str, dict] = {bid: {"n_eval": 0, "sum_normalized": 0.0,
                                     "n_perfect": 0, "max_score": 0.0}
                              for bid in bank_ids_in_play}
    total_copies = 0
    if RAW_DIR.exists():
        for batch_dir in sorted(RAW_DIR.iterdir()):
            if not batch_dir.is_dir():
                continue
            for jp in sorted(batch_dir.glob("page_*.json")):
                try:
                    with open(jp, encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                total_copies += 1
                copy_id = int(d.get("_copy_id", 1))
                ans = {int(k): v for k, v in (d.get("answers") or {}).items()}
                # `q_num` est un indice d'ordre du document ; `answers` et le
                # barème sont indexés par numéro AMC. Sur un sujet à plusieurs
                # versions, une question absente de cette copie n'a pas de
                # numéro : elle appartient à l'autre version, on la saute.
                t2a = tex_to_amc(copy_id)
                for bid_bank, q_num in instances:
                    q_amc = t2a.get(q_num) if t2a else q_num
                    if q_amc is None:
                        continue
                    sel = ans.get(q_amc) or []
                    try:
                        sc = score_question(q_amc, sel, copy=copy_id)
                        mx = max_of(q_amc, copy_id)
                    except Exception:
                        continue
                    if mx <= 0:
                        continue
                    a = accum[bid_bank]
                    a["n_eval"] += 1
                    a["sum_normalized"] += sc / mx
                    if sc >= mx - 1e-9:
                        a["n_perfect"] += 1
                    a["max_score"] = mx

    # Persiste dans la banque (local ou online selon bank_mode).
    project_name = project_state.display_name(config.project_root())
    b = _bank()
    updated, skipped = 0, 0
    for bid_bank, a in accum.items():
        try:
            b.update_project_stats(
                bid_bank, project_name,
                n_eval=a["n_eval"], sum_normalized=a["sum_normalized"],
                n_perfect=a["n_perfect"], max_score_at_sync=a["max_score"])
            updated += 1
        except KeyError:
            # Question supprimée de la banque entre-temps.
            skipped += 1
        except Exception:
            skipped += 1
    return {
        "updated":      updated,
        "skipped":      skipped,
        "total_copies": total_copies,
        "project":      project_name,
    }


@app.route("/api/bank/sync", methods=["POST"])
def api_bank_sync():
    """Met à jour les stats de la banque depuis les copies du projet actif.

    Idempotent : recalcule depuis raw_responses/ et remplace l'entrée
    `stats.by_project[<project_name>]` pour chaque question banque importée
    dans le sujet courant. Skip les blocs sans note auto (open/answerbox)."""
    try:
        return jsonify({"ok": True, **_sync_bank_stats()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>/import", methods=["POST"])
def api_bank_import(bank_id):
    """Insère une question de la banque dans le sujet courant (en fin)."""
    b = _bank()
    try:
        q = b.load(bank_id)
    except KeyError:
        return jsonify({"error": f"question inconnue : {bank_id}"}), 404
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    try:
        # Le bloc à insérer porte un bid frais + data._bank_id (trace d'origine).
        new = b.to_block(q)
        new_bid = sujet_add_block(new.kind, after_bid=None, data=new.data)
        return jsonify({"ok": True, "bid": new_bid})
    except Exception as e:
        return _crud_error(e)


# --------------------------------------------------------------------------
# Auth banque en ligne (Supabase OTP code à 6 chiffres par email)
# --------------------------------------------------------------------------

@app.route("/api/bank/auth-status")
def api_bank_auth_status():
    """Retourne {mode, configured, logged_in, user_id, email, ...} pour la
    banque active. `mode` = type de la banque active (`local` ou `online`)."""
    entry = config.active_bank_cfg()
    st = bank_auth.auth_status()
    st["mode"] = entry.get("type", "local")
    st["slug"] = config.active_bank_slug()
    st["name"] = entry.get("name", "")
    if st["mode"] == "online":
        st["supabase_url"] = entry.get("supabase_url", "")
    else:
        st["path"] = entry.get("path", "")
    return jsonify({"ok": True, **st})


@app.route("/api/bank/auth/send-otp", methods=["POST"])
def api_bank_send_otp():
    """Envoie un code à 6 chiffres par email. Body: {email}."""
    body = _json_body()
    email = (body.get("email") or "").strip()
    try:
        bank_auth.send_otp(email)
        return jsonify({"ok": True, "msg": f"Code envoyé à {email}"})
    except bank_auth.BankAuthError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/bank/auth/verify-otp", methods=["POST"])
def api_bank_verify_otp():
    """Vérifie le code OTP et persiste les tokens. Body: {email, code}."""
    body = _json_body()
    try:
        res = bank_auth.verify_otp(body.get("email", ""), body.get("code", ""))
        return jsonify({"ok": True, **res})
    except bank_auth.BankAuthError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/bank/auth/logout", methods=["POST"])
def api_bank_logout():
    """Efface les tokens locaux. (Ne révoque pas côté Supabase — JWT expire à 1h.)"""
    bank_auth.logout()
    return jsonify({"ok": True})


@app.route("/api/bank/profile", methods=["GET", "PATCH"])
def api_bank_profile():
    """GET → mon profil ; PATCH `{display_name, institution}` → update."""
    try:
        if request.method == "PATCH":
            body = _json_body()
            prof = bank_online.update_my_profile(body)
        else:
            prof = bank_online.get_my_profile()
        return jsonify({"ok": True, "profile": prof})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------
# Phase B — ratings, favoris, tags persos, stats agrégées, publication
# Toutes ces routes ne sont utilisables qu'en mode online. En local, retournent
# 400 avec un message clair (la banque locale n'a pas de ratings).
# --------------------------------------------------------------------------

def _require_online() -> None:
    if config.active_bank_cfg().get("type") != "online":
        raise RuntimeError("Cette fonctionnalité nécessite une banque en ligne "
                           "active (dropdown Banque).")


@app.route("/api/bank/<bank_id>/rating", methods=["GET", "POST", "DELETE"])
def api_bank_rating(bank_id):
    """GET → mon rating ; POST {stars?, favorite?, comment?} → upsert ;
    DELETE → supprime mon rating."""
    try:
        _require_online()
        if request.method == "POST":
            body = _json_body()
            r = bank_online.rate(bank_id,
                                  stars=body.get("stars"),
                                  favorite=body.get("favorite"),
                                  comment=body.get("comment"))
        elif request.method == "DELETE":
            bank_online.delete_my_rating(bank_id)
            r = {"stars": None, "favorite": False, "comment": ""}
        else:
            r = bank_online.get_my_rating(bank_id)
        return jsonify({"ok": True, "rating": r})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>/personal-tags", methods=["GET", "POST"])
def api_bank_personal_tags(bank_id):
    """GET → mes tags persos ; POST {tags: [str]} → remplace."""
    try:
        _require_online()
        if request.method == "POST":
            body = _json_body()
            t = bank_online.set_personal_tags(bank_id, body.get("tags") or [])
        else:
            t = bank_online.get_my_personal_tags(bank_id)
        return jsonify({"ok": True, "tags": t})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>/global-stats")
def api_bank_global_stats(bank_id):
    """Stats agrégées d'une question à travers tous les users (RPC + ratings)."""
    try:
        _require_online()
        return jsonify({"ok": True, "stats": bank_online.get_global_stats(bank_id)})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>/status", methods=["POST"])
def api_bank_set_status(bank_id):
    """Toggle status (auteur seul, RLS). Body: {status: 'draft'|'public'|'archived'}."""
    try:
        _require_online()
        body = _json_body()
        q = bank_online.set_status(bank_id, body.get("status", ""))
        return jsonify({"ok": True, "question": q})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bank/<bank_id>/update-from-block", methods=["POST"])
def api_bank_update_from_block(bank_id):
    """Met à jour une question existante depuis un bloc du sujet courant
    (typiquement après une édition locale). Body : {bid, title?, tags?}.
    L'UI propose ce bouton sur les blocs dont data._bank_id == bank_id."""
    try:
        _require_online()
        body = _json_body()
        target_bid = (body.get("bid") or "").strip()
        if not target_bid:
            return jsonify({"error": "bid manquant"}), 400
        sub = parse_subject()
        block = next((b for b in sub["blocks"] if b.bid == target_bid), None)
        if block is None:
            return jsonify({"error": f"bloc introuvable : {target_bid}"}), 404
        # Strip _bank_id du data avant de pousser (sinon on ré-écrirait l'origine).
        clean_data = {k: v for k, v in (block.data or {}).items() if k != "_bank_id"}
        q = bank_online.update_question_content(
            bank_id, clean_data,
            title=body.get("title"),
            tags=body.get("tags"),
        )
        return jsonify({"ok": True, "bank_id": q["bank_id"], "version": q.get("version")})
    except bank_online.BankAuthError as e:
        return jsonify({"error": str(e), "auth_required": True}), 401
    except KeyError as e:
        return jsonify({"error": f"question inconnue : {bank_id}"}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------
# Édition assistée par IA (Sonnet/Opus) — un seul appel API par modif.
# Pas de Claude Code, pas d'agent multi-tours : 1 prompt → tool_use →
# JSON structuré → server applique via /api/sujet/blocks/update.
# --------------------------------------------------------------------------

_AI_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "qtype": {
            "type": "string", "enum": ["single", "mult"],
            "description": "Type du QCM."
        },
        "statement": {
            "type": "string",
            "description": "Énoncé en LaTeX (math entre $…$, commandes \\\\)."
        },
        "tag": {
            "type": "string",
            "description": "Tag identifiant la question (optionnel pour add_after)."
        },
        "answers": {
            "type": "array", "minItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "text":    {"type": "string", "description": "Texte LaTeX de la réponse."},
                    "correct": {"type": "boolean"},
                    "bareme":  {"type": "string",
                                 "description": "Points (ex. '1', '1/2', '-1/3'). Vide = défaut."}
                },
                "required": ["text", "correct"]
            }
        }
    },
    "required": ["qtype", "statement", "answers"]
}

_AI_EDIT_TOOL = {
    "name": "propose_change",
    "description": ("Propose un changement sur le sujet à partir d'une question "
                    "courante. Soit l'édition de cette question (action=\"edit\", "
                    "1 bloc), soit l'ajout d'une ou plusieurs nouvelles questions "
                    "après celle-ci (action=\"add_after\", 1 à 6 blocs)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": ["edit", "add_after"],
                "description": ("'edit' = remplacer la question courante par blocks[0]. "
                                "'add_after' = insérer 1 à 6 nouvelles questions après "
                                "la question courante.")
            },
            "blocks": {
                "type": "array", "minItems": 1, "maxItems": 6,
                "items": _AI_BLOCK_SCHEMA
            },
            "rationale": {
                "type": "string",
                "description": "Une phrase expliquant l'action et son contenu."
            }
        },
        "required": ["action", "blocks"]
    }
}

_AI_SYSTEM = (
    "Tu es un assistant pour un prof qui édite un QCM AMC en LaTeX. "
    "Tu reçois la question QCM courante et une demande utilisateur. "
    "Tu appelles UNE FOIS l'outil `propose_change` avec :\n"
    "- `action=\"edit\"` ET 1 bloc dans `blocks` SI la demande implique de "
    "MODIFIER la question courante "
    "(mots-clés : reformule, modifie, rends plus clair, traduis, corrige).\n"
    "- `action=\"add_after\"` ET 1 à 6 blocs SI la demande implique d'AJOUTER "
    "de nouvelles questions APRÈS celle-ci "
    "(mots-clés : ajoute, propose une question, génère N questions, "
    "en dessous, en plus, sur le même thème, comme celle-ci).\n"
    "Règles strictes pour chaque bloc :\n"
    "1) Préserve le LaTeX (math entre $…$, commandes \\\\) — pas de markdown.\n"
    "2) Au moins 2 réponses, au moins 1 correcte.\n"
    "3) Si qtype=single : exactement 1 correcte. Si mult : 1 ou plus.\n"
    "4) Pour edit : ne change QUE ce qui est demandé — préserve le reste.\n"
    "5) Pour add_after : invente un `tag` court et descriptif pour chaque "
    "nouveau bloc (ascii + underscores). Ne réutilise pas le tag courant.\n"
    "6) `bareme` : chaîne vide pour laisser le défaut (1 pour correctes, "
    "0 pour incorrectes).\n"
    "7) Réponse en français sauf si la question demande une autre langue."
)


# Compteur de tokens et coût cumulé sur la durée de vie du process.
# Reset au redémarrage server (volontaire : pas de persistance disque).
_AI_USAGE_TOTAL: dict = {
    "n_calls":           0,
    "input_tokens":      0,
    "cache_creation":    0,
    "cache_read":        0,
    "output_tokens":     0,
    "cost_usd":          0.0,
    "started_at":        "",   # rempli au 1er appel
    "by_backend":        {"api": 0, "claude_code": 0},
    "by_model":          {},   # {model_id: n_calls}
}


def _record_ai_usage(backend: str, model: str, usage: dict, cost_usd) -> None:
    """Incrémente `_AI_USAGE_TOTAL` après un appel IA réussi."""
    g = _AI_USAGE_TOTAL
    if not g["started_at"]:
        from datetime import datetime as _dt
        g["started_at"] = _dt.now().replace(microsecond=0).isoformat()
    g["n_calls"] += 1
    u = usage or {}
    g["input_tokens"]   += int(u.get("input_tokens") or 0)
    g["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
    g["cache_read"]     += int(u.get("cache_read_input_tokens") or 0)
    g["output_tokens"]  += int(u.get("output_tokens") or 0)
    if cost_usd is not None:
        g["cost_usd"] += float(cost_usd)
    g["by_backend"][backend] = g["by_backend"].get(backend, 0) + 1
    g["by_model"][model] = g["by_model"].get(model, 0) + 1


def _api_cost_estimate(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estime le coût USD pour la voie API (Anthropic pricing public)."""
    rates = {
        "claude-sonnet-4-6": (3.0,   15.0),   # $/M : input, output
        "claude-opus-4-7":   (15.0,  75.0),
        "claude-haiku-4-5":  (1.0,    5.0),
    }
    ri, ro = rates.get(model, (3.0, 15.0))
    return round((input_tokens * ri + output_tokens * ro) / 1e6, 6)


@app.route("/api/ai/usage")
def api_ai_usage():
    """Compteur cumulatif de tokens/coût depuis le démarrage du server.
    Reset via `POST /api/ai/usage/reset`. Non persisté."""
    return jsonify({"ok": True, **_AI_USAGE_TOTAL})


@app.route("/api/ai/usage/reset", methods=["POST"])
def api_ai_usage_reset():
    """Remet le compteur à zéro."""
    for k in ("n_calls", "input_tokens", "cache_creation", "cache_read",
              "output_tokens"):
        _AI_USAGE_TOTAL[k] = 0
    _AI_USAGE_TOTAL["cost_usd"] = 0.0
    _AI_USAGE_TOTAL["started_at"] = ""
    _AI_USAGE_TOTAL["by_backend"] = {"api": 0, "claude_code": 0}
    _AI_USAGE_TOTAL["by_model"] = {}
    return jsonify({"ok": True})


def _call_claude_code(cc_path: str, system_prompt: str, user_msg: str,
                       model: str = "", timeout_s: int = 90) -> dict:
    """Spawn `claude --print --output-format json` et retourne le résultat parsé.

    Claude Code utilise l'auth OAuth de l'utilisateur (abonnement Pro/Max),
    pas une clé API. La réponse est attendue au format ``<edit_json>{...}</edit_json>``
    (le system_prompt cadre ça). Tools désactivés pour faire du one-shot pur.
    `model` accepte 'sonnet', 'opus', 'haiku' ou le full id (claude-sonnet-4-6…).
    Retourne `{ok, parsed, raw_text, cost_usd, usage}`.
    """
    import subprocess as _sp
    # CWD = /tmp pour éviter qu'un CLAUDE.md du projet vienne polluer le prompt.
    cmd = [
        cc_path,
        "--print",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        # Tous les tools désactivés : on ne veut qu'une réponse textuelle.
        "--disallowed-tools", "Bash", "Read", "Write", "Edit", "Grep",
        "Glob", "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite",
        "Agent", "ExitPlanMode", "ScheduleWakeup",
    ]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", user_msg]
    try:
        proc = _sp.run(cmd, capture_output=True, timeout=timeout_s,
                       text=True, cwd="/tmp")
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"Claude Code n'a pas répondu en {timeout_s}s."}
    except Exception as e:
        return {"ok": False, "error": f"Spawn Claude Code échoué : {e}"}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[-500:]
        return {"ok": False, "error": f"Claude Code (rc={proc.returncode}) : {err or '?'}"}
    try:
        result = json.loads(proc.stdout)
    except Exception as e:
        return {"ok": False, "error": f"Sortie Claude Code non-JSON : {e}"}
    text = result.get("result") or ""
    # Extrait le JSON entre <edit_json>…</edit_json>.
    m = re.search(r"<edit_json>\s*(\{.*\})\s*</edit_json>", text, re.DOTALL)
    if not m:
        return {"ok": False, "error": "Pas de bloc <edit_json> dans la réponse Claude Code.",
                "raw_text": text}
    try:
        parsed = json.loads(m.group(1))
    except Exception as e:
        return {"ok": False, "error": f"JSON invalide dans <edit_json> : {e}",
                "raw_text": text}
    usage = result.get("usage") or {}
    return {
        "ok":        True,
        "parsed":    parsed,
        "raw_text":  text,
        "cost_usd":  result.get("total_cost_usd"),
        "usage":     usage,
    }


def _detect_claude_code_binary() -> str:
    """Cherche le binaire `claude` (Claude Code).

    Précédence : env `CLAUDE_CODE_EXECPATH` (utilisé par l'extension VSCode) →
    `which claude` dans le PATH → extension VSCode standard sous Linux/macOS.
    Retourne le chemin absolu si trouvé, sinon "".
    """
    env = os.environ.get("CLAUDE_CODE_EXECPATH", "").strip()
    if env and Path(env).is_file():
        return env
    import shutil as _shutil
    w = _shutil.which("claude")
    if w:
        return w
    # Extension VSCode (chemins typiques Linux)
    home = Path.home()
    for pat in (
        ".vscode/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/claude",
        ".vscode/extensions/anthropic.claude-code-*-darwin-*/resources/native-binary/claude",
    ):
        for p in home.glob(pat):
            if p.is_file():
                return str(p)
    return ""


@app.route("/api/ai/auth-status")
def api_ai_auth_status():
    """État de la connexion IA : `{has_api_key, cc_binary_path, ai_model}`.
    L'UI s'en sert pour afficher un panneau « Connecter » au lieu d'une erreur."""
    cfg_dict = load_config()
    has_key = bool((cfg_dict.get("anthropic_api_key") or "").strip()
                   or os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return jsonify({
        "ok":              True,
        "has_api_key":     has_key,
        "cc_binary_path":  _detect_claude_code_binary(),
        "ai_model":        cfg_dict.get("ai_model") or "claude-sonnet-4-6",
    })


@app.route("/api/ai/edit-block", methods=["POST"])
def api_ai_edit_block():
    """Demande à Sonnet/Opus de modifier un bloc QCM. Retourne `{current,
    proposed, new_data, rationale, usage}` — l'UI affiche un diff et applique
    via /api/sujet/blocks/update si l'utilisateur valide."""
    body = _json_body()
    bid = (body.get("bid") or "").strip()
    user_prompt = (body.get("prompt") or "").strip()
    if not bid or not user_prompt:
        return jsonify({"error": "Champs `bid` et `prompt` requis."}), 400

    cfg_dict = load_config()
    api_key = (cfg_dict.get("anthropic_api_key") or "").strip()
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    cc_path = _detect_claude_code_binary() if not api_key else ""
    if not api_key and not cc_path:
        return jsonify({"error": "Aucune clé API et pas de Claude Code détecté. Voir Connecter Claude."}), 400

    model = cfg_dict.get("ai_model") or "claude-sonnet-4-6"

    try:
        sub = parse_subject()
    except Exception as e:
        return jsonify({"error": f"Erreur de parsing du sujet : {e}"}), 500
    block = next((b for b in (sub.get("blocks") or []) if b.bid == bid), None)
    if not block:
        return jsonify({"error": f"Bloc introuvable : {bid}"}), 404
    if block.kind != "question_qcm":
        return jsonify({"error": "L'édition IA est limitée aux blocs question_qcm pour l'instant."}), 400

    current = {
        "tag":       block.data.get("tag", ""),
        "qtype":     block.data.get("qtype", "single"),
        "statement": block.data.get("statement", ""),
        "answers": [
            {"text":    a.get("text", ""),
             "correct": bool(a.get("correct")),
             "bareme":  a.get("bareme", "")}
            for a in (block.data.get("answers") or [])
        ],
    }
    user_msg = (
        "Bloc QCM courant :\n```json\n"
        + json.dumps(current, ensure_ascii=False, indent=2)
        + "\n```\n\nDemande utilisateur :\n"
        + user_prompt
    )

    proposal = None
    backend = ""
    usage: dict = {}
    cost_usd = None

    if api_key:
        # Voie 1 : API Anthropic directe (clé fournie) — tool use → JSON garanti.
        try:
            from anthropic import Anthropic
        except ImportError:
            return jsonify({"error": "Le package `anthropic` n'est pas installé."}), 500
        try:
            client = Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=model,
                max_tokens=4000,
                system=_AI_SYSTEM,
                tools=[_AI_EDIT_TOOL],
                tool_choice={"type": "tool", "name": "propose_change"},
                messages=[{"role": "user", "content": user_msg
                           + "\n\nAppelle `propose_change` avec action='edit' ou 'add_after'."}],
            )
        except Exception as e:
            return jsonify({"error": f"Erreur API Anthropic : {e}"}), 502
        for blk in (resp.content or []):
            if getattr(blk, "type", "") == "tool_use" and getattr(blk, "name", "") == "propose_change":
                proposal = blk.input or {}
                break
        if not proposal:
            return jsonify({"error": "Réponse Anthropic sans tool_use."}), 500
        backend = "api"
        usage = {
            "input_tokens":  getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
        }
        cost_usd = _api_cost_estimate(model,
                                       int(usage["input_tokens"] or 0),
                                       int(usage["output_tokens"] or 0))
    else:
        # Voie 2 : Claude Code subprocess (utilise l'abonnement OAuth de l'user).
        cc_system = (_AI_SYSTEM +
            "\n\nRÉPONSE OBLIGATOIRE : ENCADRER le JSON dans des balises "
            "<edit_json>...</edit_json> et rien d'autre. Pas de markdown autour, "
            "pas de phrase d'intro. Schéma JSON : "
            "{\"action\":\"edit\"|\"add_after\","
            "\"blocks\":[{\"qtype\":\"single\"|\"mult\",\"statement\":\"...\","
            "\"tag\":\"...\",\"answers\":[{\"text\":\"...\",\"correct\":true|false,"
            "\"bareme\":\"...\"}]}],\"rationale\":\"...\"}")
        # CC accepte les full IDs (claude-sonnet-4-6) OU les alias (sonnet/opus/haiku).
        result = _call_claude_code(cc_path, cc_system, user_msg, model=model)
        if not result.get("ok"):
            err = result.get("error", "?")
            return jsonify({"error": f"Claude Code : {err}",
                            "raw": result.get("raw_text", "")[:500]}), 502
        proposal = result["parsed"] or {}
        backend = "claude_code"
        u = result.get("usage") or {}
        usage = {
            "input_tokens":  u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"),
            "cache_read_input_tokens":     u.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
        }
        cost_usd = result.get("cost_usd")

    # --- Compat : si le modèle renvoie le schéma plat (qtype/statement/answers
    # à la racine, sans action/blocks), on le wrap en action=edit.
    action = (proposal.get("action") or "").strip()
    raw_blocks = proposal.get("blocks")
    if not raw_blocks:
        if proposal.get("statement") and proposal.get("answers"):
            raw_blocks = [{
                "qtype":     proposal.get("qtype"),
                "statement": proposal.get("statement"),
                "answers":   proposal.get("answers"),
                "tag":       proposal.get("tag"),
            }]
            action = action or "edit"
    if action not in ("edit", "add_after"):
        action = "edit"

    # Valide + normalise chaque bloc proposé.
    normalized: list[dict] = []
    old_ans = current["answers"]
    for idx, b in enumerate(raw_blocks or []):
        if not isinstance(b, dict):
            continue
        qtype_b = (b.get("qtype") or current["qtype"]).strip()
        if qtype_b not in ("single", "mult"):
            qtype_b = "single"
        ans = []
        for i, a in enumerate(b.get("answers") or []):
            bareme = (a.get("bareme") or "").strip()
            if not bareme:
                if action == "edit" and i < len(old_ans):
                    bareme = old_ans[i].get("bareme") or ("1" if a.get("correct") else "0")
                else:
                    bareme = "1" if a.get("correct") else "0"
            ans.append({
                "text":    str(a.get("text", "")),
                "correct": bool(a.get("correct")),
                "bareme":  bareme,
            })
        if len(ans) < 2:
            return jsonify({"error": f"Bloc #{idx+1} : moins de 2 réponses."}), 422
        n_corr = sum(1 for a in ans if a["correct"])
        if qtype_b == "single" and n_corr != 1:
            return jsonify({"error": f"Bloc #{idx+1} (single) : {n_corr} corrects (attendu : 1)."}), 422
        if n_corr == 0:
            return jsonify({"error": f"Bloc #{idx+1} : aucune réponse correcte."}), 422
        tag = (b.get("tag") or "").strip() or f"q_{secrets.token_hex(3)}"
        tag = re.sub(r"[^A-Za-z0-9_]+", "_", tag).strip("_") or f"q_{secrets.token_hex(3)}"
        normalized.append({
            "qtype":     qtype_b,
            "tag":       tag,
            "statement": str(b.get("statement", "")),
            "answers":   ans,
        })
    if not normalized:
        return jsonify({"error": "Aucun bloc valide dans la proposition."}), 422

    if action == "edit":
        if len(normalized) > 1:
            return jsonify({"error": "action=edit attend 1 seul bloc."}), 422
        b0 = normalized[0]
        new_data = dict(block.data)
        new_data["qtype"]     = b0["qtype"]
        new_data["statement"] = b0["statement"]
        new_data["answers"]   = b0["answers"]
        proposed_resp = {
            "qtype":     b0["qtype"],
            "statement": b0["statement"],
            "answers":   b0["answers"],
        }
    else:
        # add_after : pas de new_data — l'UI insérera chaque bloc via /api/sujet/blocks/add.
        new_data = None
        proposed_resp = {"blocks": normalized}

    _record_ai_usage(backend, model, usage, cost_usd)

    return jsonify({
        "ok":        True,
        "action":    action,
        "current":   current,
        "proposed":  proposed_resp,
        "rationale": str(proposal.get("rationale", "")),
        "new_data":  new_data,
        "after_bid": bid if action == "add_after" else None,
        "model":     model,
        "backend":   backend,
        "cost_usd":  cost_usd,
        "usage":     usage,
        "total":     dict(_AI_USAGE_TOTAL),
    })


# --------------------------------------------------------------------------
# Pipeline copies scannées : upload PDF → extract → grade → seed
# Async (background thread + polling) car cv_grade prend plusieurs minutes
# pour des dizaines de pages.
# --------------------------------------------------------------------------

import threading as _threading
import time as _time
import uuid as _uuid

# Tâches asynchrones : task_id → {status, step, progress, log[], started_at,
# error?, n_extracted?, n_graded?, n_seeded?, finished_at?}
_PIPE_TASKS: dict = {}


def _task_registries():
    """Tous les registres de tâches asynchrones : (libellé, dict)."""
    return (("traitement des scans", _PIPE_TASKS),
            ("reconnaissance des noms", _HTR_TASKS),
            ("vérification des noms", _HTR_VERIFY_TASKS))


def running_tasks() -> list[str]:
    """Libellés des tâches en cours, tous registres confondus.

    Sert à refuser (a) un 2e pipeline concurrent — deux threads qui écrivent
    les mêmes JPG puis deux `seed_raw_responses` en parallèle sur les mêmes
    JSON — et (b) un changement de projet pendant une écriture, le switch
    faisant un `os._exit(0)` brutal.
    """
    out = []
    for label, reg in _task_registries():
        for tid, t in list(reg.items()):
            if (t or {}).get("status") == "running":
                out.append(f"{label} ({tid})")
    return out


def _purge_finished_tasks(max_age_s: int = 3600) -> None:
    """Oublie les tâches terminées depuis plus d'une heure (registres en mémoire)."""
    from datetime import datetime as _dt
    now = _dt.now()
    for _label, reg in _task_registries():
        for tid, t in list(reg.items()):
            if (t or {}).get("status") == "running":
                continue
            fin = (t or {}).get("finished_at")
            if not fin:
                continue
            try:
                age = (now - _dt.fromisoformat(fin)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age > max_age_s:
                reg.pop(tid, None)


def _safe_filename(name: str) -> str:
    """Empêche les chemins ../ et caractères farfelus. Garde le nom + extension."""
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name)
    return name[:120] or "uploaded.pdf"


@app.route("/api/scan-pdf/hide", methods=["POST"])
def api_scan_pdf_hide():
    """Retire un PDF de la liste de tracking AMCx (le fichier reste sur disque).

    Body : `{name}`. Ajoute le nom à `config.scan_pdfs_excluded` (liste).
    L'auto-découverte (`_project_files_info` + `extract_pages.discover_pdfs`)
    filtre cette liste. Réversible via `POST /api/scan-pdf/unhide`.
    """
    body = _json_body()
    name = (body.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "nom invalide"}), 400
    cfg = load_config()
    excluded = list(cfg.get("scan_pdfs_excluded") or [])
    if name not in excluded:
        excluded.append(name)
    save_config({"scan_pdfs_excluded": excluded})
    return jsonify({"ok": True, "hidden": name, "excluded": excluded})


@app.route("/api/scan-pdf/unhide", methods=["POST"])
def api_scan_pdf_unhide():
    """Ré-ajoute un PDF retiré à la liste AMCx. Body : `{name}`."""
    body = _json_body()
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nom manquant"}), 400
    cfg = load_config()
    excluded = [n for n in (cfg.get("scan_pdfs_excluded") or []) if n != name]
    save_config({"scan_pdfs_excluded": excluded})
    return jsonify({"ok": True, "restored": name, "excluded": excluded})


@app.route("/api/upload-scan-pdf", methods=["POST"])
def api_upload_scan_pdf():
    """Reçoit un PDF de copies scannées et le copie dans `amc_dir/`.

    Si `amc_dir` n'existe pas, le crée. Si `amc_dir` est read-only (test
    EXAM_2026), retourne une erreur claire. Ne lance pas le pipeline ;
    il faut un POST séparé sur `/api/process-scans` pour ça.
    """
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "champ `file` manquant"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "fichier .pdf attendu"}), 400
    dst_dir = config.amc_dir()
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"impossible de créer {dst_dir} : {e}"}), 500
    safe = _safe_filename(f.filename)
    dst = dst_dir / safe
    try:
        f.save(dst)
    except PermissionError:
        return jsonify({"error": f"{dst_dir} en lecture seule"}), 403
    except Exception as e:
        return jsonify({"error": f"échec écriture : {e}"}), 500
    # Lit le nb de pages pour réponse
    try:
        doc = fitz.open(str(dst))
        n_pages = doc.page_count
        doc.close()
    except Exception:
        n_pages = 0
    return jsonify({"ok": True, "name": safe, "path": str(dst),
                    "pages": n_pages, "size_mb": round(dst.stat().st_size / 1e6, 2)})


def _pipeline_set(task_id, **kw):
    if task_id not in _PIPE_TASKS:
        return
    t = _PIPE_TASKS[task_id]
    t.update(kw)


def _pipeline_log(task_id, msg):
    if task_id not in _PIPE_TASKS:
        return
    _PIPE_TASKS[task_id]["log"].append(msg)
    # Cap à 200 lignes pour pas exploser la mémoire
    if len(_PIPE_TASKS[task_id]["log"]) > 200:
        _PIPE_TASKS[task_id]["log"] = _PIPE_TASKS[task_id]["log"][-200:]


def _run_pipeline(task_id: str):
    """Worker thread : extract → grade → seed.

    Met à jour `_PIPE_TASKS[task_id]` au fil de l'eau. Capture les exceptions
    pour ne jamais crasher silencieusement.
    """
    from datetime import datetime as _dt
    import subprocess as _sp
    proj_root = config.project_root()
    try:
        # Réserve 1 coeur pour Flask + l'OS pendant la pipeline (mono-coeur → 1).
        import os as _os
        n_workers = max(1, (_os.cpu_count() or 1) - 1)

        # 1+2. EXTRACT + GRADE (pipeline fusionné) -------------------------
        # Chaque worker rend une page PDF → pixmap, écrit son JPG (pour l'UI)
        # ET grade en mémoire sans relecture disque. Évite le décodage JPEG
        # côté CV et la perte de qualité associée.
        _pipeline_set(task_id, step="Pipeline fusionné", progress=2)
        import extract_pages
        import cv_grade
        pdfs = extract_pages.discover_pdfs()
        if not pdfs:
            raise RuntimeError("Aucun PDF dans amc_dir. Upload un PDF d'abord.")
        jpg_root = proj_root / "pages"
        cv_dir = proj_root / "raw_responses_cv"
        _pipeline_log(task_id,
                      f"{len(pdfs)} PDF — pipeline fusionné (workers={n_workers})")
        n_graded = 0
        n_failed = 0
        seen_pdfs = set()

        def _on_fused(done, total, pdf_stem, page_num, summary):
            nonlocal n_graded, n_failed
            if pdf_stem not in seen_pdfs:
                seen_pdfs.add(pdf_stem)
                _pipeline_log(task_id, f"  ▶ {pdf_stem}.pdf")
            if isinstance(summary, Exception):
                n_failed += 1
                _pipeline_log(task_id,
                              f"    ✘ {pdf_stem}/page_{page_num:03d} : {summary}")
            else:
                n_graded += 1
            if done % 5 == 0 or done == total:
                _pipeline_set(task_id,
                              step=f"Render + grade ({done}/{total})",
                              progress=2 + 83 * done / max(total, 1))

        cv_grade.grade_pdfs_fused(pdfs, jpg_root=jpg_root, json_root=cv_dir,
                                  workers=n_workers, on_progress=_on_fused)
        _pipeline_set(task_id, n_extracted=n_graded + n_failed, n_graded=n_graded)
        _pipeline_log(task_id, f"pages traitées : {n_graded} ✓  ({n_failed} échouées)")

        # 3. SEED -----------------------------------------------------------
        _pipeline_set(task_id, step="Fusion dans raw_responses/", progress=88)
        # Subprocess de seed_raw_responses.py (CLI propre, gère --preserve-manual).
        seed_script = Path(__file__).resolve().parent / "seed_raw_responses.py"
        try:
            proc = _sp.run([sys.executable, str(seed_script), "--preserve-manual"],
                           capture_output=True, timeout=180, text=True, cwd=str(proj_root))
            if proc.stdout:
                _pipeline_log(task_id, proc.stdout[-2000:])
            if proc.returncode != 0:
                raise RuntimeError(f"seed_raw_responses rc={proc.returncode} : {proc.stderr[-500:]}")
        except Exception as e:
            _pipeline_log(task_id, f"  ✘ seed : {e}")
            raise

        n_rr = sum(1 for _ in (proj_root / "raw_responses").rglob("page_*.json")) \
                if (proj_root / "raw_responses").is_dir() else 0
        _pipeline_set(task_id, n_seeded=n_rr, step="Terminé", progress=100,
                      status="done", finished_at=_dt.now().isoformat(timespec="seconds"))
        _pipeline_log(task_id, f"raw_responses/ : {n_rr} copies prêtes pour la relecture")
    except Exception as e:
        _pipeline_set(task_id, status="error", error=str(e), progress=100,
                      finished_at=_dt.now().replace(microsecond=0).isoformat())
        _pipeline_log(task_id, f"ERREUR : {e}")


@app.route("/api/process-scans", methods=["POST"])
def api_process_scans():
    """Lance le pipeline async. Retourne `{ok, task_id}`."""
    # Réentrance : deux pipelines concurrents écrivent les mêmes JPG puis
    # lancent deux `seed_raw_responses` en parallèle sur les mêmes JSON.
    _purge_finished_tasks()
    if any((t or {}).get("status") == "running" for t in _PIPE_TASKS.values()):
        return jsonify({"error": "Un traitement des scans est déjà en cours. "
                                 "Attends qu'il finisse avant d'en relancer un."}), 409
    # Vérifie qu'il y a au moins un PDF
    info = _project_files_info()
    if not info["scan_pdfs"]:
        return jsonify({"error": "Aucun PDF de copies dans amc_dir. Upload un PDF avant."}), 400
    task_id = _uuid.uuid4().hex[:8]
    from datetime import datetime as _dt
    _PIPE_TASKS[task_id] = {
        "status":      "running",
        "step":        "Initialisation",
        "progress":    0,
        "log":         [],
        "started_at":  _dt.now().replace(microsecond=0).isoformat(),
    }
    _threading.Thread(target=_run_pipeline, args=(task_id,), daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/process-scans/<task_id>")
def api_process_scans_status(task_id):
    """Polling : état d'une tâche de pipeline."""
    t = _PIPE_TASKS.get(task_id)
    if t is None:
        return jsonify({"error": "task_id inconnu (probablement terminé+expiré)"}), 404
    return jsonify({"ok": True, **t})


# --------------------------------------------------------------------------
# Onglet « Courriels » : gabarit, secret SMTP, envoi des notes
# --------------------------------------------------------------------------
_MAIL_TASKS: dict = {}


def _mail_settings(cfg: dict) -> dict:
    """Réglages d'envoi, **sans le moindre secret**.

    ⚠ `password_set` est un booléen, jamais la valeur : le mot de passe ne
    remonte à aucun client. Le champ du formulaire est en écriture seule.
    """
    import mail_results as mr
    sender = (cfg.get("mail_sender") or "").strip()
    return {
        "subject":     cfg.get("mail_subject") or "",
        "date":        cfg.get("mail_date") or "",
        "sender":      sender,
        "sender_name": cfg.get("mail_sender_name") or "",
        "smtp_host":   cfg.get("mail_smtp_host") or "smtp.gmail.com",
        "smtp_port":   int(cfg.get("mail_smtp_port") or 465),
        # ⚠ Deux valeurs, pas une : `smtp_user` est l'identifiant EFFECTIF (pour
        # se connecter), `smtp_user_raw` ce qui est réellement stocké. Le champ
        # du formulaire affiche le second — sinon l'enregistrement automatique
        # figerait « = adresse d'expédition » en valeur explicite, et changer
        # l'adresse ensuite ne l'entraînerait plus.
        "smtp_user":     (cfg.get("mail_smtp_user") or "").strip() or sender,
        "smtp_user_raw": (cfg.get("mail_smtp_user") or "").strip(),
        "score_col":   cfg.get("mail_score_col") or "note_finale",
        "max_score":   float(cfg.get("mail_max_score") or 0) or None,
        "password_set": mr.has_password(),
        "password_path": str(mr.PASSWORD_FILE),
        "env_password": bool(os.environ.get("AMCX_SMTP_PASSWORD")),
    }


def _mail_effective_max(cfg: dict, settings: dict) -> float:
    """Barème annoncé : celui réglé dans l'onglet, sinon celui du sujet.

    ⚠ Les deux colonnes vivaient sur deux échelles tant que « note finale »
    était une agrégation réglable. Depuis que la note d'un examen est son score
    brut, elles partagent le barème du sujet — et c'est lui qu'il faut
    annoncer : servir une échelle d'agrégation dirait « 3,5 / 20 » sur un QCM
    qui vaut 5.
    """
    import mail_results as mr
    if settings["max_score"]:
        return settings["max_score"]
    return subject_total_max() or mr.default_max_score(cfg)


def _mail_payload(cfg: dict) -> dict:
    """Tout ce dont l'onglet a besoin : réglages, gabarit, destinataires."""
    import mail_results as mr
    st = _mail_settings(cfg)
    try:
        data = mr.load_recipients(mr.NOTES_CSV, st["score_col"])
        recipients, skipped, error = data["recipients"], data["skipped"], ""
    except mr.MailError as e:
        recipients, skipped, error = [], [], str(e)
    done = mr.already_sent(mr.SENT_LOG)
    for r in recipients:
        r["sent"] = r["email"] in done
    return {
        "settings": st,
        "fields": list(mr.FIELDS),
        "template": mr.load_template(),
        "template_path": str(mr.PROJECT_TEMPLATE),
        "notes_csv": str(mr.NOTES_CSV),
        "log_path": str(mr.SENT_LOG),
        "max_score": _mail_effective_max(cfg, st),
        "recipients": recipients,
        "skipped": [{"who": w, "why": y} for w, y in skipped],
        "error": error,
    }


@app.route("/mail")
def mail_page():
    if not project_state.is_valid_project(config.project_root()):
        return redirect(url_for("index"))
    return render_template("mail.html", active="mail", **_mail_payload(load_config()))


@app.route("/api/mail", methods=["GET"])
def api_mail():
    return jsonify({"ok": True, **_mail_payload(load_config())})


@app.route("/api/mail/settings", methods=["POST"])
def api_mail_settings():
    """Enregistre les réglages. Le mot de passe part AILLEURS (0600, hors projet)."""
    import mail_results as mr
    body = request.get_json(force=True, silent=True) or {}
    updates = {}
    for key, cfg_key in (("subject", "mail_subject"), ("date", "mail_date"),
                         ("sender", "mail_sender"),
                         ("sender_name", "mail_sender_name"),
                         ("smtp_host", "mail_smtp_host"),
                         ("smtp_user", "mail_smtp_user"),
                         ("score_col", "mail_score_col")):
        if key in body:
            updates[cfg_key] = str(body[key] or "").strip()
    if "smtp_port" in body:
        try:
            port = int(body["smtp_port"])
        except (TypeError, ValueError):
            return jsonify({"error": "port invalide"}), 400
        if not (1 <= port <= 65535):
            return jsonify({"error": "port hors bornes"}), 400
        updates["mail_smtp_port"] = port
    if "max_score" in body:
        v = body["max_score"]
        try:
            updates["mail_max_score"] = 0 if v in (None, "") else float(v)
        except (TypeError, ValueError):
            return jsonify({"error": "barème invalide"}), 400
    if "template" in body:
        try:
            mr.save_template(str(body["template"]))
        except OSError as e:
            return jsonify({"error": f"écriture du gabarit : {e}"}), 500
    # ⚠ Écriture seule, et seulement si la clé est présente : un enregistrement
    # de réglages ne doit pas effacer le secret parce que le champ est vide.
    if "password" in body:
        try:
            mr.save_password(str(body["password"] or ""))
        except OSError as e:
            return jsonify({"error": f"écriture du secret : {e}"}), 500
    cfg = save_config(updates) if updates else load_config()
    return jsonify({"ok": True, **_mail_payload(cfg)})


@app.route("/api/mail/preview", methods=["POST"])
def api_mail_preview():
    """Rend le gabarit pour UN destinataire, sans rien enregistrer ni envoyer.

    Le gabarit vient du corps de la requête, pas du disque : l'aperçu porte sur
    ce que l'utilisateur a sous les yeux, éditions non enregistrées comprises.
    """
    import mail_results as mr
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    st = _mail_settings(cfg)
    template = body.get("template")
    if template is None:
        template = mr.load_template()
    try:
        data = mr.load_recipients(mr.NOTES_CSV, body.get("score_col")
                                  or st["score_col"])
    except mr.MailError as e:
        return jsonify({"error": str(e)}), 400
    recipients = data["recipients"]
    want = (body.get("email") or "").strip().lower()
    rec = next((r for r in recipients if r["email"].lower() == want), None)
    if rec is None:
        rec = recipients[0] if recipients else {
            "email": "etudiant@exemple.fr", "id": "0000",
            "full_name": "EXEMPLE Camille", "first_name": "Camille", "score": 0.0}
    max_score = body.get("max_score") or _mail_effective_max(cfg, st)
    date = body.get("date") or st["date"] or ""
    sender_name = body.get("sender_name") or st["sender_name"] or st["sender"]
    try:
        text = mr.render(template, rec, date=date, max_score=float(max_score),
                         sender_name=sender_name)
    except mr.MailError as e:
        return jsonify({"error": str(e)}), 400
    subject = (body.get("subject") or st["subject"]
               or mr.default_subject(date)).strip()
    try:
        # L'objet passe par le même moteur : « $name, your result » est un
        # usage légitime, et un `$champ` fautif doit se voir dans l'aperçu.
        subject = mr.render(subject, rec, date=date, max_score=float(max_score),
                            sender_name=sender_name)
    except mr.MailError as e:
        return jsonify({"error": f"objet : {e}"}), 400
    return jsonify({"ok": True, "to": rec["email"], "to_name": rec["full_name"],
                    "subject": subject, "body": text,
                    "n_recipients": len(recipients)})


def _run_mail(task_id: str, targets: list, opts: dict):
    """Worker : envoie, journalise, et ne meurt jamais en silence."""
    import mail_results as mr
    t = _MAIL_TASKS.get(task_id)
    if t is None:
        return
    try:
        sent, failures = 0, []
        ctx = __import__("ssl").create_default_context()
        import smtplib
        with smtplib.SMTP_SSL(opts["host"], opts["port"], context=ctx) as smtp:
            smtp.login(opts["user"], opts["password"])
            for i, rec in enumerate(targets, 1):
                body = mr.render(opts["template"], rec, date=opts["date"],
                                 max_score=opts["max_score"],
                                 sender_name=opts["sender_name"])
                msg = mr.build_message(
                    rec, body,
                    subject=mr.render(opts["subject"], rec, date=opts["date"],
                                      max_score=opts["max_score"],
                                      sender_name=opts["sender_name"]),
                    sender=opts["sender"], sender_name=opts["sender_name"])
                try:
                    smtp.send_message(msg)
                except Exception as e:              # noqa: BLE001
                    failures.append(rec["email"])
                    mr.log_send(mr.SENT_LOG, rec, "echec", str(e)[:200])
                    t["log"].append(f"✘ {rec['full_name']} <{rec['email']}> : {e}")
                else:
                    sent += 1
                    mr.log_send(mr.SENT_LOG, rec, "ok")
                    t["log"].append(f"✓ {rec['full_name']} <{rec['email']}>")
                t.update(progress=round(i * 100 / max(1, len(targets))),
                         step=f"{i} / {len(targets)}")
                if opts["delay"]:
                    _time.sleep(opts["delay"])
        t.update(status="done", progress=100, sent=sent, failed=len(failures),
                 step=f"{sent} envoyé(s), {len(failures)} échec(s)")
    except Exception as e:                          # noqa: BLE001
        t.update(status="error", step=str(e))
        t["log"].append(f"✘ {e}")


@app.route("/api/mail/send", methods=["POST"])
def api_mail_send():
    """Lance l'envoi. `mode` : `test` (une adresse) ou `all`.

    ⚠ L'envoi est irréversible et sort du poste : le front confirme, et ici on
    refuse tout ce qui n'est pas explicitement demandé — pas de destinataire
    par défaut, pas de renvoi aux adresses déjà servies sans `force`.
    """
    import mail_results as mr
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    st = _mail_settings(cfg)
    if any((t or {}).get("status") == "running" for t in _MAIL_TASKS.values()):
        return jsonify({"error": "Un envoi est déjà en cours."}), 409
    if not st["sender"]:
        return jsonify({"error": "Renseigne l'adresse d'expédition."}), 400
    try:
        password = mr.smtp_password(interactive=False)
    except mr.MailError as e:
        return jsonify({"error": str(e)}), 400
    try:
        data = mr.load_recipients(mr.NOTES_CSV, st["score_col"])
    except mr.MailError as e:
        return jsonify({"error": str(e)}), 400
    recipients = data["recipients"]
    mode = body.get("mode")
    if mode == "test":
        to = (body.get("to") or "").strip()
        if not to:
            return jsonify({"error": "Indique l'adresse de test."}), 400
        # Un vrai destinataire sert de modèle, mais le message part à `to` :
        # l'essai doit montrer ce que l'étudiant recevra, pas un texte inventé.
        model = recipients[0] if recipients else {
            "email": to, "id": "0000", "full_name": "EXEMPLE Camille",
            "first_name": "Camille", "score": 0.0}
        targets = [{**model, "email": to}]
    elif mode == "all":
        done = set() if body.get("force") else mr.already_sent(mr.SENT_LOG)
        targets = [r for r in recipients if r["email"] not in done]
    else:
        return jsonify({"error": "mode inconnu"}), 400
    if not targets:
        return jsonify({"error": "Aucun destinataire à servir."}), 400

    task_id = _uuid.uuid4().hex[:8]
    _MAIL_TASKS[task_id] = {"status": "running", "step": "Connexion…",
                            "progress": 0, "log": [], "sent": 0, "failed": 0,
                            "total": len(targets), "mode": mode}
    date = st["date"]
    opts = {
        "host": st["smtp_host"], "port": st["smtp_port"],
        "user": st["smtp_user"] or st["sender"], "password": password,
        "sender": st["sender"], "sender_name": st["sender_name"] or st["sender"],
        "subject": st["subject"] or mr.default_subject(date),
        "template": mr.load_template(), "date": date,
        "max_score": _mail_effective_max(cfg, st),
        "delay": 0.5,
    }
    _threading.Thread(target=_run_mail, args=(task_id, targets, opts),
                      daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "total": len(targets)})


@app.route("/api/mail/send/<task_id>")
def api_mail_send_status(task_id):
    t = _MAIL_TASKS.get(task_id)
    if t is None:
        return jsonify({"error": "task_id inconnu"}), 404
    return jsonify({"ok": True, **t})


# --------------------------------------------------------------------------
# Onglet « Questions » : ranking + stats par question (QCM only) + aperçu PDF
# --------------------------------------------------------------------------

def question_stats() -> dict:
    """Stats par question QCM (depuis raw_responses/) pour le projet actif.

    Pour chaque QCM en ordre document : `{q, tag, type, statement, max_score,
    mean_raw (points), n_eval, n_perfect, mean (normalisé ∈ [-∞,1]), scores,
    bank_id, bid, preview_q, q_in_version, version}`. Skip open/answerbox.

    ⚠ **Une seule implémentation**, servie à l'onglet Questions (via
    `/api/questions/stats`) comme à la page Évaluation, qui n'en affiche que la
    moyenne brute. Deux calculs finiraient par afficher deux moyennes.

    ⚠ **`q` n'est ni le numéro imprimé ni la clé de l'aperçu.** Trois
    numérotations coexistent et les confondre a été constaté sur un sujet à deux
    versions : l'onglet Questions demandait l'aperçu de « Q10 » et recevait le
    cadre de la question AMC 10, c'est-à-dire la **première** question de
    l'après-midi (imprimée « Question 1 »), tandis que Q6 à Q9 tombaient sur les
    colonnes du code étudiant et n'avaient aucun aperçu.

    - `q` : ordre du document — clé de `parse_tex()`, du barème, de `answers` ;
    - `preview_q` : clé de région (numéro AMC), via `block_preview_keys()` ;
    - `q_in_version` + `version` : ce qui est imprimé sur la copie.
    """
    sub = parse_subject()
    qs = parse_tex()
    if not qs:
        return {"questions": [], "total_copies": 0}

    # q_num → block AMCx (pour récupérer `_bank_id`). En canonique l'ordre
    # des QCM dans `sub["blocks"]` correspond aux clés de `parse_tex()`.
    qcm_blocks_in_order = [b for b in (sub.get("blocks") or [])
                           if b.kind == "question_qcm"]
    q_to_block = {i: b for i, b in enumerate(qcm_blocks_in_order, start=1)}
    preview_keys = block_preview_keys(sub.get("blocks") or [])
    rank_in_version = qcm_rank_in_version(sub.get("blocks") or [])
    version_names = {v.group: (v.name or v.group)
                     for v in (sub["config"].versions or [])}

    scores_per_q: dict[int, list[float]] = {q: [] for q in qs}
    n_perfect_per_q: dict[int, int] = {q: 0 for q in qs}
    max_per_q: dict[int, float] = {q: 1.0 for q in qs}

    total_copies = 0
    if RAW_DIR.exists():
        for batch_dir in sorted(RAW_DIR.iterdir()):
            if not batch_dir.is_dir():
                continue
            for jp in sorted(batch_dir.glob("page_*.json")):
                try:
                    with open(jp, encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                total_copies += 1
                copy_id = int(d.get("_copy_id", 1))
                ans = {int(k): v for k, v in (d.get("answers") or {}).items()}
                t2a = tex_to_amc(copy_id)   # cf. note du même motif dans /api/bank/sync
                for q_num in qs:
                    q_amc = t2a.get(q_num) if t2a else q_num
                    if q_amc is None:
                        continue
                    sel = ans.get(q_amc) or []
                    try:
                        sc = score_question(q_amc, sel, copy=copy_id)
                        mx = max_of(q_amc, copy_id)
                    except Exception:
                        continue
                    if mx <= 0:
                        continue
                    scores_per_q[q_num].append(sc)
                    if sc >= mx - 1e-9:
                        n_perfect_per_q[q_num] += 1
                    max_per_q[q_num] = mx

    out: list[dict] = []
    for q_num, info in sorted(qs.items()):
        block = q_to_block.get(q_num)
        statement = (info.get("statement") or "").replace("\n", " ").strip()
        statement = re.sub(r"\s+", " ", statement)
        if len(statement) > 140:
            statement = statement[:140].rstrip() + "…"
        scores = scores_per_q[q_num]
        n_eval = len(scores)
        mx = max_per_q[q_num] or 1.0
        mean = None
        if n_eval > 0 and mx > 0:
            mean = sum(s / mx for s in scores) / n_eval
        out.append({
            "q":         q_num,
            "tag":       info.get("tag", ""),
            "type":      info.get("type", "single"),
            "statement": statement,
            "max_score": round(mx, 4),
            "n_eval":    n_eval,
            "n_perfect": n_perfect_per_q[q_num],
            "mean":      round(mean, 4) if mean is not None else None,
            # Moyenne en POINTS du barème : ce que l'évaluation affiche. La
            # version normalisée sert au ranking de l'onglet Questions.
            "mean_raw":  round(sum(scores) / n_eval, 3) if n_eval else None,
            "scores":    [round(s, 4) for s in scores],
            "bank_id":   (block.data.get("_bank_id") if block else None),
            "bid":       (block.bid if block else None),
            # Clé de l'aperçu PDF — surtout pas `q` (cf. la note ci-dessus).
            "preview_q": (preview_keys.get(block.bid) if block else None),
            "q_in_version": (rank_in_version.get(block.bid) if block else None),
            "version":   (version_names.get(block.group, "") if block else ""),
        })
    return {"questions": out, "total_copies": total_copies}


@app.route("/api/questions/stats")
def api_questions_stats():
    """Stats par question, en JSON, pour l'onglet Questions."""
    try:
        out = question_stats()
    except Exception as e:                              # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, **out})


@app.route("/questions")
def questions_page():
    """Onglet Questions : ranking par taux de réussite + aperçu PDF + histo."""
    has_pdf = (SUJET_DIR / "DOC-sujet.pdf").exists()
    return render_template("questions.html", active="questions", has_pdf=has_pdf)


@app.route("/sujet/region/<int:q>.png")
def sujet_region(q):
    """Crop PNG de la région d'une question dans le PDF (aperçu au survol)."""
    r = pdf_regions().get(q)
    pdf = SUJET_DIR / "DOC-sujet.pdf"
    if not r or not pdf.exists():
        abort(404)
    try:
        doc = fitz.open(str(pdf))
        if r["page"] - 1 >= doc.page_count:
            abort(404)
        page = doc[r["page"] - 1]
        sc = 72.0 / 300.0   # coords layout 300 dpi → points PDF
        pr = page.rect
        clip = fitz.Rect(max(pr.x0, r["x0"] * sc), max(pr.y0, r["y0"] * sc),
                         min(pr.x1, r["x1"] * sc), min(pr.y1, r["y1"] * sc))
        data = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip).tobytes("png")
        doc.close()
    except Exception:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/png")


@app.route("/sujet/page/<int:n>.png")
def sujet_full_page(n):
    """Page PDF entière rendue en PNG (~150 dpi). Sert l'aperçu vertical complet
    dans le panneau droit + dans le lightbox."""
    pdf = SUJET_DIR / "DOC-sujet.pdf"
    if not pdf.exists():
        abort(404)
    try:
        doc = fitz.open(str(pdf))
        if n - 1 < 0 or n - 1 >= doc.page_count:
            doc.close()
            abort(404)
        page = doc[n - 1]
        data = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
        doc.close()
    except Exception:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/png")


@app.route("/sujet/region/<int:q>.json")
def sujet_region_info(q):
    """Méta d'une région : `{page, y_top, y_bot, page_h, total_pages}` avec
    `y_top`/`y_bot` en ratio [0..1] de la hauteur de page (300 dpi). Le front
    s'en sert pour scroller la page complète à la position de la question."""
    r = pdf_regions().get(q)
    pdf = SUJET_DIR / "DOC-sujet.pdf"
    if not r or not pdf.exists():
        abort(404)
    try:
        doc = fitz.open(str(pdf))
        total_pages = doc.page_count
        doc.close()
    except Exception:
        total_pages = 0
    # `pdf_regions()` utilise (2480, 3508) px @ 300 dpi par défaut pour la
    # hauteur ; on récupère via layout_store si possible.
    try:
        import layout_store
        lay = layout_store.get_layout()
        page_h = float(lay.pages[r["page"]].height) if r["page"] in lay.pages else 3508.0
    except Exception:
        page_h = 3508.0
    return jsonify({
        "page": r["page"],
        "y_top": float(r["y0"]) / page_h,
        "y_bot": float(r["y1"]) / page_h,
        "page_h": page_h,
        "total_pages": total_pages,
    })


def qcm_rank_in_version(blocks) -> dict:
    """`{bid: rang du QCM DANS SA VERSION}` — le numéro imprimé sur la copie.

    ⚠ Le rang global (l'ordre du document) reste la clé de `parse_tex()` et du
    barème, mais il n'est **imprimé nulle part** : avec deux versions, la 6e
    question du document est « Question 1 » sur le sujet de l'après-midi. Une
    seule implémentation, partagée par l'onglet Sujet, l'onglet Questions et la
    page Évaluation — trois numérotations divergentes, c'est trois pages qui
    nomment la même question différemment.
    """
    out, seen = {}, {}
    for b in blocks:
        if b.kind != "question_qcm":
            continue
        seen[b.group] = seen.get(b.group, 0) + 1
        out[b.bid] = seen[b.group]
    return out


def block_preview_keys(blocks):
    """`{bid: clé de région}` pour chaque bloc du sujet.

    La clé est le **numéro AMC** pour les questions (celui de `pdf_regions()`)
    et le **bid** pour les blocs texte, dont la région est indexée ainsi. Les
    blocs absents du PDF compilé n'ont pas d'entrée.

    ⚠ Une seule implémentation, partagée par la page (`data-preview-q`) et par
    `/sujet/regions.json` (champ `bid`) : si les deux divergeaient, un cadre de
    l'aperçu ne pointerait plus sur le bon bloc. Elle s'appuie sur
    `layout.question_names` plutôt que sur `amc_question_map()`, dont la
    classification range la grille de barème d'un `answerbox` (étiquetée 0,1,2…)
    parmi les colonnes du code étudiant.
    """
    # tag AMC → numéro. Un tag dupliqué (question importée dans son propre
    # projet d'origine) ne désigne personne : on le retire plutôt que de
    # garder le dernier vu.
    by_tag: dict = {}
    try:
        for q_num, tag in layout_store.get_layout().question_names.items():
            by_tag.setdefault(str(tag).strip(), []).append(q_num)
    except Exception:
        pass
    tag_to_q = {t: n[0] for t, n in by_tag.items() if len(n) == 1}
    try:
        regions = pdf_regions()
    except Exception:
        regions = {}

    out, qcm_seq = {}, 0
    for b in blocks:
        key = None
        if b.kind == "question_qcm":
            qcm_seq += 1
            # ⚠ Par le TAG, pas par la position. Les deux coïncident sur un
            # sujet simple ; avec plusieurs versions la 6e question du document
            # porte le numéro AMC 10, et l'indexer par sa position collait son
            # cadre d'aperçu sur la question de l'autre version.
            key = tag_to_q.get((b.data.get("tag") or "").strip())
            if key is None:
                key = qcm_seq
        elif b.kind in ("question_open", "question_freeform"):
            key = tag_to_q.get((b.data.get("tag") or "").strip())
        elif b.kind == "answerbox":
            key = tag_to_q.get(f"bareme-{b.bid}")
        if key is None and b.bid in regions:
            key = b.bid                      # bloc texte localisé par son contenu
        if key is not None:
            out[b.bid] = key
    return out


@app.route("/api/sujet/charmap")
def api_sujet_charmap():
    """Lettres de case par question, pour une copie donnée.

    `?copy=N` → `{copy, chars: {q: [lettres…]}, stale}`. Avec `shuffle_answers`,
    une même réponse ne porte pas la même lettre d'un exemplaire à l'autre : le
    sélecteur de copie de l'onglet Sujet rejoue l'affichage avec cette carte.
    `stale` signale que le sujet a été édité depuis la dernière compilation,
    donc que les lettres décrivent l'ancien sujet.
    """
    try:
        copy = int(request.args.get("copy") or 1)
    except (TypeError, ValueError):
        copy = 1
    return jsonify({"copy": copy,
                    "chars": {str(q): c for q, c in charmap_for_copy(copy).items()},
                    "stale": letters_stale()})


@app.route("/sujet/regions.json")
def sujet_regions_all():
    """Toutes les régions de question du PDF, en **ratios** de la page.

    Sert au lien bidirectionnel éditeur ↔ aperçu de l'onglet Sujet : le front
    empile les pages (`/sujet/page/<n>.png`) et pose un rectangle absolu par
    question, positionné en `%` — donc indépendant de la largeur de rendu du
    panneau (qui change avec la fenêtre) et du dpi choisi côté serveur.

    `{pages: [{n, w, h}], total_pages, regions: [{q, page, x0, y0, x1, y1}]}`
    avec x/y ∈ [0,1].
    """
    regions = pdf_regions()
    pdf = SUJET_DIR / "DOC-sujet.pdf"
    if not pdf.exists():
        return jsonify({"pages": [], "total_pages": 0, "regions": []})
    try:
        doc = fitz.open(str(pdf))
        total_pages = doc.page_count
        doc.close()
    except Exception:
        total_pages = 0
    # Dimensions par page (px 300 dpi) — mêmes valeurs de repli que pdf_regions().
    # Une passe par version : avec deux `\exemplaire`, le calage de la copie 1
    # ne décrit que les pages de la première moitié du sujet, et l'aperçu ne
    # montrait donc qu'un seul groupe.
    dims = {}
    for _c in sujet_region_copies():
        try:
            lay = layout_store.get_layout(copy=_c)
        except Exception:
            continue
        _pm = lay.pdf_page_map()
        dims.update({_pm.get(p, p): (float(pi.width), float(pi.height))
                     for p, pi in lay.pages.items()})
    # Chaque région reçoit le `bid` du bloc qu'elle représente : c'est la clé
    # que l'éditeur utilise pour relier un bloc à son cadre. Tous les blocs ont
    # un bid, y compris ceux sans région (pas encore compilés) — c'est ce qui
    # rend n'importe quel bloc sélectionnable.
    bid_of = {}
    try:
        for bid, key in block_preview_keys(parse_subject()["blocks"]).items():
            bid_of[key] = bid
    except Exception:
        pass

    out = []
    # Clés mixtes : int (numéro AMC) pour les questions, str (bid) pour les
    # blocs texte — on trie donc par position dans le PDF, pas par clé.
    for q, r in sorted(regions.items(), key=lambda kv: (kv[1]["page"], kv[1]["y0"])):
        w, h = dims.get(r["page"], (2480.0, 3508.0))
        if w <= 0 or h <= 0:
            continue
        out.append({
            # `q` = clé interne (numéro AMC, ou bid pour un bloc texte).
            # `bid` = le bloc de l'éditeur — c'est la clé qu'utilise le front.
            "q": q, "bid": bid_of.get(q, q if isinstance(q, str) else None),
            "page": r["page"],
            "x0": max(0.0, float(r["x0"]) / w), "y0": max(0.0, float(r["y0"]) / h),
            "x1": min(1.0, float(r["x1"]) / w), "y1": min(1.0, float(r["y1"]) / h),
        })
    # On n'expose que les pages couvertes par le calage : avec `\exemplaire{N}`
    # le PDF contient N copies à la suite, mais le calage (donc les régions)
    # ne décrit que la copie 1. Empiler les copies 2..N donnerait des pages
    # sans aucun cadre, sans que l'utilisateur comprenne pourquoi.
    last = max(dims) if dims else total_pages
    last = min(last, total_pages) if total_pages else last
    pages = [{"n": n, "w": dims.get(n, (2480.0, 3508.0))[0],
              "h": dims.get(n, (2480.0, 3508.0))[1]}
             for n in range(1, last + 1)]
    return jsonify({"pages": pages, "total_pages": total_pages,
                    "shown_pages": last, "regions": out})


# ---------------------------------------------------------------------------
# Multi-projets : gestion du projet actif + liste des récents.
# Le switch de projet redémarre le process Flask (project_state.execv) pour
# garantir l'invalidation de TOUS les caches modules. Le browser doit attendre
# que le serveur revienne (~500-1500 ms) puis recharger la page.
# ---------------------------------------------------------------------------

@app.route("/api/doctor")
def api_doctor():
    """Diagnostic d'installation en JSON (cf. auto_grading/doctor.py)."""
    import doctor as _doctor
    checks = _doctor.run_checks()
    return jsonify({
        "ok": not any(c["status"] == "fail" for c in checks),
        "checks": checks,
    })


@app.route("/diagnostic")
def diagnostic_page():
    """Page de diagnostic — à envoyer au support quand « ça ne marche pas »."""
    import doctor as _doctor
    return render_template("diagnostic.html", checks=_doctor.run_checks(),
                           active="")


@app.route("/api/projects")
def api_projects():
    """État courant : projet actif (ou None) + liste des récents."""
    p = config.project_root()
    valid = project_state.is_valid_project(p)
    return jsonify({
        "active": str(p) if valid else None,
        "active_name": project_state.display_name(p) if valid else None,
        "recent": project_state.recent_projects(),
        "default_root": str(project_state.DEFAULT_PROJECTS_ROOT),
    })


@app.route("/api/projects/browse")
def api_projects_browse():
    """Sous-dossiers d'un dossier, pour choisir où créer un projet.

    `?path=` (défaut : la racine des projets). Renvoie `{path, display,
    parent, at_root, dirs:[{name, path, is_project}]}` — `parent` vaut `null`
    quand on est à la racine autorisée, ce qui grise le bouton « remonter ».

    ⚠ Borné au dossier personnel (`project_state.browse_root`) : le serveur
    n'est pas authentifié et `--host` permet de l'exposer.
    """
    raw = request.args.get("path") or ""
    path = project_state.resolve_dir(raw)
    try:
        dirs = project_state.list_subdirs(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except NotADirectoryError:
        return jsonify({"error": f"Dossier introuvable : {path}"}), 404
    root = project_state.browse_root().resolve()
    resolved = path.resolve()
    return jsonify({
        "path": str(resolved),
        "display": project_state.display_dir(resolved),
        "parent": None if resolved == root else str(resolved.parent),
        "at_root": resolved == root,
        "dirs": dirs,
    })


@app.route("/api/projects/mkdir", methods=["POST"])
def api_projects_mkdir():
    """Crée un sous-dossier : `{parent, name}` → `{path, display}`.

    Sert à ranger les projets sans quitter la modale. Même borne que le
    sélecteur (`project_state.check_under_browse_root`) et même règle de nom
    que pour un projet — un dossier de rangement peut en devenir un.
    """
    body = _json_body()
    parent = project_state.resolve_dir(body.get("parent"))
    name = str(body.get("name") or "").strip()
    try:
        created = project_state.make_subdir(parent, name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except NotADirectoryError:
        return jsonify({"error": f"Dossier introuvable : {parent}"}), 404
    except FileExistsError:
        return jsonify({"error": f"« {name} » existe déjà."}), 409
    except OSError as e:
        return jsonify({"error": f"Création impossible : {e}"}), 400
    return jsonify({"ok": True, "path": str(created),
                    "display": project_state.display_dir(created)})


@app.route("/api/projects/open", methods=["POST"])
def api_projects_open():
    """Switche le projet actif vers `path` puis redémarre Flask.

    Body: `{"path": "/chemin/vers/projet"}`. Le `path` peut être soit le dossier
    racine d'un projet (`~/Documents/AMCx/foo`) soit son sous-dossier
    `auto_grading/` directement — on auto-corrige.
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "Chemin manquant."}), 400
    p = Path(raw).expanduser().resolve()
    # Auto-correction : `~/Documents/AMCx/foo` → `foo/auto_grading` si applicable.
    if not project_state.is_valid_project(p):
        alt = p / "auto_grading"
        if project_state.is_valid_project(alt):
            p = alt
    if not project_state.is_valid_project(p):
        return jsonify({"error": f"Dossier invalide : pas de sujet/exam.tex dans {p}."}), 400
    # Le switch de projet tue le process (`os._exit(0)`) : refuser tant qu'une
    # tâche écrit dans raw_responses/ ou pages/.
    busy = running_tasks()
    if busy:
        return jsonify({"error": "Tâche en cours : " + ", ".join(busy)
                                 + ". Attends la fin avant de changer de projet."}), 409
    # Réponse renvoyée AVANT le restart : Flask flushe la réponse, puis on exec.
    return _restart_after_response(
        jsonify({"ok": True, "active": str(p),
                 "active_name": project_state.display_name(p)}), p)


def _restart_after_response(resp, target):
    """Programme le redémarrage du serveur **après** l'envoi de la réponse.

    ⚠ L'ancienne version lançait un thread qui dormait 200 ms puis appelait
    `os._exit(0)`, sans lien avec l'état de la réponse. Sur une création de
    projet un peu longue (import d'un `.tex`, écriture disque), la connexion
    était coupée avant que le corps ne soit parti : le navigateur affichait
    « Erreur réseau » alors que le projet avait bien été créé.

    `call_on_close` se déclenche quand le corps de la réponse a été remis au
    serveur WSGI ; le court délai qui suit laisse le socket se vider.
    """
    import threading
    import time as _t

    def _go():
        _t.sleep(0.4)
        project_state.restart_server_with_project(target)

    resp.call_on_close(lambda: threading.Thread(target=_go, daemon=True).start())
    return resp


@app.route("/api/projects/discover")
def api_projects_discover():
    """Liste les projets présents dans `~/Documents/AMCx/` (modale Ouvrir).

    Inclut les dossiers incomplets (sans `sujet/exam.tex`) pour que l'UI
    puisse offrir un bouton de suppression — sinon ces coquilles bloquent
    la recréation d'un projet du même nom.
    """
    return jsonify({
        "root": str(project_state.DEFAULT_PROJECTS_ROOT),
        "projects": project_state.discover_projects(),
    })


@app.route("/api/projects/delete-folder", methods=["POST"])
def api_projects_delete_folder():
    """Supprime un dossier de projet sous `~/Documents/AMCx/`.

    Refuse de supprimer le projet **actif** (le serveur tournerait dessus,
    pages would crash). Body : `{"dir": "/chemin/du/dossier"}`.
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get("dir") or "").strip()
    if not raw:
        return jsonify({"error": "Champ 'dir' manquant."}), 400
    target = Path(raw).expanduser().resolve()
    # Refus si le projet actif est dans ce dossier.
    active = config.project_root()
    try:
        active.relative_to(target)
        return jsonify({
            "error": "C'est le projet actif. Bascule sur un autre projet avant de le supprimer."
        }), 409
    except ValueError:
        pass
    try:
        project_state.delete_folder_under_root(target)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": f"Échec suppression : {e}"}), 500
    # Retire aussi de la liste des récents au cas où.
    project_state.forget_project(target / "auto_grading")
    project_state.forget_project(target)
    return jsonify({"ok": True})


@app.route("/api/projects/forget", methods=["POST"])
def api_projects_forget():
    """Retire `path` de la liste des récents (ne supprime rien sur disque)."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "Chemin manquant."}), 400
    project_state.forget_project(Path(raw).expanduser().resolve())
    return jsonify({"ok": True, "recent": project_state.recent_projects()})


@app.route("/api/templates")
def api_templates():
    """Liste des templates fournis pour créer un projet vierge."""
    from new_project import list_templates
    return jsonify(list_templates())


# Dossier temporaire pour stocker les .tex importés avant la création.
# Dossier d'upload temporaire : `mkdtemp` (0700, nom imprévisible) plutôt qu'un
# chemin fixe et partagé de /tmp, squattable par un autre utilisateur.
_AMC_UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="amcx_uploads_"))


@app.route("/api/projects/create", methods=["POST"])
def api_projects_create():
    """Crée un projet dans `~/Documents/AMCx/<name>/` puis redémarre Flask.

    Accepte deux formats :
      - JSON : `{"name": "...", "template": "examen_minimal", "title?": "...", "author?": "..."}`
      - multipart : `name`, `template="from_amc"`, `file=<file.tex>` (upload du sujet AMC)
    """
    from new_project import create_project as np_create
    name = ""
    template = "examen_minimal"
    parent_raw = ""
    source_tex: Path | None = None

    if request.content_type and request.content_type.startswith("multipart/"):
        name = (request.form.get("name") or "").strip()
        template = (request.form.get("template") or "examen_minimal").strip()
        parent_raw = (request.form.get("parent") or "").strip()
        if template == "from_amc":
            f = request.files.get("file")
            if not f or not f.filename:
                return jsonify({"error": "Fichier .tex manquant."}), 400
            _AMC_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe = secure_filename(f.filename) or "import.tex"
            source_tex = _AMC_UPLOAD_DIR / f"{int(__import__('time').time())}_{safe}"
            f.save(source_tex)
    else:
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        template = (data.get("template") or "examen_minimal").strip()
        parent_raw = (data.get("parent") or "").strip()

    err = project_state.project_name_error(name)
    if err:
        return jsonify({"error": err}), 400

    # Comme /api/projects/open : la création se termine par un restart.
    busy = running_tasks()
    if busy:
        return jsonify({"error": "Tâche en cours : " + ", ".join(busy)
                                 + ". Attends la fin avant de créer un projet."}), 409

    # Dossier parent choisi par l'utilisateur, défaut = la racine des projets.
    parent = project_state.resolve_dir(parent_raw)
    if parent == project_state.DEFAULT_PROJECTS_ROOT:
        project_state.ensure_default_root()
    if not parent.is_dir():
        return jsonify({"error": f"Dossier introuvable : {parent}"}), 400
    if not os.access(parent, os.W_OK):
        return jsonify({"error": f"Dossier non modifiable : {parent}"}), 400
    dest = parent / name
    if dest.exists():
        return jsonify({"error": f"Existe déjà : {dest}"}), 409

    import_report: dict = {}
    try:
        ag = np_create(dest, template=template, source_tex=source_tex,
                       report=import_report)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": f"Template inconnu : {e}"}), 400
    except Exception as e:
        return jsonify({"error": f"Échec création : {e}"}), 500

    out = {"ok": True, "path": str(ag), "name": project_state.display_name(ag)}
    # Import AMC dont la structure n'a pas été comprise : le projet existe et
    # se corrige, mais le sujet n'est pas éditable. Le dire — sans ça, l'écran
    # annonce « projet créé » et l'onglet Sujet s'ouvre vide.
    if import_report and not import_report.get("migrated"):
        out["warning"] = import_report.get("log") or (
            "Le sujet importé n'a pas pu être converti : il reste en lecture "
            "seule (mode legacy).")
    return _restart_after_response(jsonify(out), ag)


# ==========================================================================
# Vue d'ensemble — plusieurs examens d'un même dossier
# ==========================================================================
# ⚠ Ces routes ne dépendent PAS du projet actif : un ensemble lit ses examens
# par sous-processus (`cohort.read_members`). Changer d'ensemble ne redémarre
# donc pas le serveur, contrairement à changer de projet.


def _cohort_dir():
    d = project_state.active_cohort()
    return d if (d and Path(d).is_dir()) else None


def _cohort_view(d: Path) -> dict:
    """Tout ce que la page affiche : rapport + géométries des graphiques."""
    rep = cohort.report(d)
    cfg, cols, rows = rep["config"], rep["columns"], rep["students"]
    g = float(cfg.get("hist_granularity", 1.0))
    thr = float(cfg.get("final_threshold", 20.0))
    pass_mark = float(cfg.get("pass_mark", 10.0))
    top = compute_multi_series_stats(cohort.calibration_series(rows, cols), g)
    finals = [r["final"] for r in rows if r.get("final") is not None]
    bottom = compute_multi_series_stats(
        [{"name": "Note finale", "color": SERIES_COLORS[0],
          "values": finals, "opacity": 1.0}], g)
    stats = dict(bottom["series"][0]["stats"])
    stats["pass_mark"] = pass_mark
    stats["n_below"] = sum(1 for v in finals if v < pass_mark)
    scale = max([c["max"] for c in cols] + [thr, 1.0])
    return {
        **rep,
        "hist_top": multi_histogram_geometry(top, mean_line=False),
        "hist_bottom": multi_histogram_geometry(bottom, mean_line=True,
                                                vline=pass_mark),
        "stats": stats,
        "formula": build_formula(cols, thr),
        "scatter": cohort.scatter_data(rows, cols),
        "ranges": slider_ranges(cfg, cols, scale),
    }


@app.route("/cohorte")
def cohorte_page():
    d = _cohort_dir()
    view = None
    error = ""
    if d:
        try:
            view = _cohort_view(d)
        except cohort.CohortError as e:
            error = str(e)
    return render_template("cohorte.html", view=view, error=error,
                           cohort_dir=str(d) if d else "",
                           default_root=str(project_state.DEFAULT_PROJECTS_ROOT),
                           active="cohorte")


@app.route("/api/cohorte/open", methods=["POST"])
def api_cohorte_open():
    """Ouvre un dossier comme ensemble. ⚠ `create` est explicite : poser un
    `cohorte.json` dans un dossier au hasard n'est pas anodin."""
    body = request.get_json(force=True)
    raw = str(body.get("path", "")).strip()
    if not raw:
        return jsonify({"error": "chemin vide"}), 400
    d = Path(raw).expanduser()
    try:
        project_state.check_under_browse_root(d)
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 403
    if not d.is_dir():
        return jsonify({"error": f"{d} n'existe pas"}), 404
    if not cohort.is_cohort(d):
        if not body.get("create"):
            return jsonify({"error": f"{d} ne contient pas de {cohort.COHORT_FILE}",
                            "can_create": True}), 404
        cohort.save(d, dict(cohort.DEFAULTS, name=d.name))
    project_state.set_active_cohort(d)
    return jsonify({"ok": True, "path": str(d)})


@app.route("/api/cohorte/config", methods=["POST"])
def api_cohorte_config():
    """Réglages de l'ensemble : plafond, seuil de réussite, granularité, et
    les trois nombres de chaque colonne (normalisation, échelle, poids)."""
    d = _cohort_dir()
    if not d:
        return jsonify({"error": "aucun ensemble actif"}), 404
    body = request.get_json(force=True)
    cfg = cohort.load(d)
    for key in ("final_threshold", "pass_mark", "hist_granularity"):
        if key in body and body[key] is not None:
            cfg[key] = _pos_float(body, key, allow_zero=(key == "pass_mark"))
    by_path = {e["path"]: e for e in cfg.get("exams", [])}
    for c in body.get("columns") or []:
        e = by_path.get(str(c.get("path", "")))
        if e is None:
            continue
        # ⚠ `null` = « auto » (le barème de l'examen), et c'est une valeur : la
        # remplacer par le nombre affiché figerait l'échelle au barème du jour.
        for k in ("seuil", "max"):
            if k in c:
                e[k] = None if c[k] in (None, "") else float(c[k])
        if c.get("agg_weight") is not None:
            e["agg_weight"] = float(c["agg_weight"])
    cohort.save(d, cfg)
    return jsonify({"ok": True})


@app.route("/api/cohorte/exams", methods=["POST"])
def api_cohorte_exams():
    """Ajoute ou retire un examen. Retirer ne touche **rien** sur le disque."""
    d = _cohort_dir()
    if not d:
        return jsonify({"error": "aucun ensemble actif"}), 404
    body = request.get_json(force=True)
    rel = str(body.get("path", "")).strip()
    if not rel or ".." in rel or Path(rel).is_absolute():
        return jsonify({"error": "chemin invalide"}), 400
    cfg = cohort.load(d)
    exams = [e for e in cfg.get("exams", []) if e["path"] != rel]
    if not body.get("remove"):
        if not (d / rel).is_dir():
            return jsonify({"error": f"{rel} introuvable"}), 404
        exams.append({"path": rel, "label": str(body.get("label") or rel),
                      "seuil": None, "max": None, "agg_weight": 1.0})
    cfg["exams"] = exams
    cohort.save(d, cfg)
    return jsonify({"ok": True, "n_exams": len(exams)})


@app.route("/api/cohorte/report", methods=["POST"])
def api_cohorte_report():
    """Écrit `<ensemble>/compte_rendu/notes.csv`, **envoyable tel quel**.

    ⚠ Ce n'est pas un second format : c'est le même fichier que
    `/cohorte/export.csv`, posé là où les courriels le cherchent. Ses colonnes
    portent les noms qu'attend `mail_results.load_recipients` (`id_canonique`,
    `nom_prenom`, `courriel`, `note_finale`).

    ⚠ L'envoi lui-même passe par la ligne de commande, avec **`--log` sur le
    journal de l'ensemble** : le journal du projet ferait sauter les étudiants
    déjà servis pour l'examen — même adresse, autre note.
    """
    d = _cohort_dir()
    if not d:
        return jsonify({"error": "aucun ensemble actif"}), 404
    rep = cohort.report(d)
    header, rows = cohort.export_rows(rep["students"], rep["columns"])
    out_dir = Path(d) / "compte_rendu"
    out_dir.mkdir(exist_ok=True)
    notes = out_dir / "notes.csv"
    with open(notes, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    scale = max([c["max"] for c in rep["columns"]]
                + [float(rep["config"].get("final_threshold", 20.0))])
    cmd = (f'python auto_grading/mail_results.py --notes "{notes}" '
           f'--log "{out_dir / "mail_log.csv"}" --out-of {scale:g}')
    return jsonify({"ok": True, "path": str(notes), "n_rows": len(rows),
                    "command": cmd})


@app.route("/cohorte/export.csv")
def cohorte_export_csv():
    d = _cohort_dir()
    if not d:
        abort(404)
    rep = cohort.report(d)
    header, rows = cohort.export_rows(rep["students"], rep["columns"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", rep["name"]) or "ensemble"
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}_notes.csv"},
    )


# ==========================================================================
# Onglet Fichiers — le dossier de travail et son arborescence
# ==========================================================================
#
# ⚠ **Ces routes écrivent sur le disque de l'utilisateur.** Tous les garde-fous
# vivent dans [workspace.py](../workspace.py) : chemins relatifs à la racine,
# vérification après résolution des liens symboliques, validation des noms,
# suppression = corbeille. Les routes ne font que traduire ses refus en codes
# HTTP — aucune ne recalcule un chemin de son côté, sinon les deux finiraient
# par diverger et c'est l'écriture qui coûterait cher.

def _ws_error(e: Exception):
    if isinstance(e, workspace.WorkspaceError):
        return jsonify({"error": str(e)}), 400
    if isinstance(e, PermissionError):
        return jsonify({"error": f"Droits insuffisants : {e}"}), 403
    if isinstance(e, OSError):
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": str(e)}), 500


def _ws_state() -> dict:
    r = workspace.root()
    return {
        "root":         str(r) if r else "",
        "display":      project_state.display_dir(r) if r else "",
        "name":         r.name if r else "",
        "default_root": str(project_state.DEFAULT_PROJECTS_ROOT),
        "home":         str(project_state.browse_root()),
        "n_trash":      len(workspace.list_trash()) if r else 0,
        "active":       str(config.project_root()),
        "cohort":       bool(r and workspace.is_cohort(r)),
        # ⚠ La racine peut être posée sur un dossier d'examen : le menu du vide
        # ne doit alors pas proposer d'en faire un projet (cf. `make_cohort`).
        "project":      bool(r and workspace.is_project(r)),
    }


@app.route("/fichiers")
def fichiers_page():
    return render_template("fichiers.html", ws=_ws_state(), active="fichiers")


@app.route("/api/workspace")
def api_workspace():
    return jsonify({"ok": True, **_ws_state()})


@app.route("/api/workspace/root", methods=["POST"])
def api_workspace_root():
    """Définit le dossier de travail. ⚠ N'écrit **rien** dedans — c'est ce qui
    permet de le poser sur un dossier existant sans le transformer en
    « ensemble » tant qu'on n'a pas composé de notes."""
    try:
        r = workspace.set_root(_json_body().get("path", ""))
        return jsonify({"ok": True, **_ws_state(), "root": str(r)})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/list")
def api_workspace_list():
    try:
        rel = request.args.get("path", "")
        hidden = request.args.get("hidden") in ("1", "true", "yes")
        return jsonify({"ok": True, "path": rel.strip("/"),
                        "entries": workspace.listdir(rel, hidden=hidden)})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/info")
def api_workspace_info():
    try:
        return jsonify({"ok": True, "entry": workspace.info(
            request.args.get("path", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/mkdir", methods=["POST"])
def api_workspace_mkdir():
    b = _json_body()
    try:
        return jsonify({"ok": True,
                        "path": workspace.mkdir(b.get("parent", ""),
                                                b.get("name", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/cohorte", methods=["POST"])
def api_workspace_new_cohorte():
    """Crée un **projet** (dossier + `cohorte.json`) : `{parent, name}`.

    ⚠ Ne bascule pas dessus — cf. `workspace.new_cohort`. L'ouvrir se fait par
    `/api/workspace/root`, qui re-enracine l'arbre.
    """
    b = _json_body()
    try:
        return jsonify({"ok": True,
                        "path": workspace.new_cohort(b.get("parent", ""),
                                                     b.get("name", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/cohorte/set", methods=["POST"])
def api_workspace_set_cohorte():
    """Change un dossier existant en **projet**, ou l'inverse.

    `{path, is_project: true|false}`. Une seule route pour les deux sens : le
    garde-fou (ce qui peut devenir un projet, ce qui peut cesser de l'être) est
    alors écrit à un seul endroit.

    ⚠ Retirer ne supprime rien : le `cohorte.json` part à la corbeille, et
    aucune évaluation n'est touchée.
    """
    b = _json_body()
    rel = b.get("path", "")
    try:
        if b.get("is_project"):
            return jsonify({"ok": True, "path": workspace.make_cohort(rel),
                            "n_trash": len(workspace.list_trash())})
        return jsonify({"ok": True, **workspace.unmake_cohort(rel),
                        "n_trash": len(workspace.list_trash())})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/rename", methods=["POST"])
def api_workspace_rename():
    b = _json_body()
    try:
        return jsonify({"ok": True,
                        "path": workspace.rename(b.get("path", ""),
                                                 b.get("name", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/move", methods=["POST"])
def api_workspace_move():
    b = _json_body()
    try:
        return jsonify({"ok": True,
                        "path": workspace.move(b.get("path", ""),
                                               b.get("dest", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/delete", methods=["POST"])
def api_workspace_delete():
    """Met à la corbeille. ⚠ **Ne supprime rien** — cf. `workspace.trash`."""
    b = _json_body()
    paths = b.get("paths") or ([b["path"]] if b.get("path") else [])
    done, failed = [], []
    for rel in paths:
        try:
            done.append(workspace.trash(rel))
        except Exception as e:
            failed.append({"path": rel, "error": str(e)})
    # ⚠ Un échec ne fait pas échouer les autres, et il est RENDU : supprimer
    # 5 dossiers dont 1 verrouillé ne doit ni s'arrêter au premier, ni laisser
    # croire que les 5 sont partis.
    return jsonify({"ok": not failed, "trashed": done, "failed": failed,
                    "n_trash": len(workspace.list_trash())})


@app.route("/api/workspace/trash")
def api_workspace_trash():
    try:
        return jsonify({"ok": True, "entries": workspace.list_trash(),
                        "size": workspace.trash_size()})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/trash/restore", methods=["POST"])
def api_workspace_trash_restore():
    try:
        return jsonify({"ok": True,
                        "path": workspace.restore(_json_body().get("slot", "")),
                        "n_trash": len(workspace.list_trash())})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/trash/empty", methods=["POST"])
def api_workspace_trash_empty():
    """⚠ La seule route de tout le projet qui détruit vraiment des fichiers."""
    try:
        return jsonify({"ok": True, "removed": workspace.empty_trash()})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/upload", methods=["POST"])
def api_workspace_upload():
    dest = (request.form.get("dest") or "").strip()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "aucun fichier"}), 400
    saved, failed = [], []
    for f in files:
        try:
            saved.append(workspace.save_upload(dest, f.filename, f.stream))
        except Exception as e:
            failed.append({"name": f.filename, "error": str(e)})
    return jsonify({"ok": not failed, "saved": saved, "failed": failed})


@app.route("/api/workspace/preview")
def api_workspace_preview():
    """Contenu d'un fichier, pour le panneau de détail (texte tronqué)."""
    try:
        return jsonify({"ok": True,
                        **workspace.preview(request.args.get("path", ""))})
    except Exception as e:
        return _ws_error(e)


@app.route("/api/workspace/view")
def api_workspace_view():
    """Sert un fichier **en ligne**, sur liste blanche stricte de types.

    ⚠ PDF, PNG et JPEG seulement (`workspace.INLINE_TYPES`), avec le type
    annoncé et `nosniff`. Servir un `.html` ou un `.svg` de l'utilisateur ici
    le placerait sur l'origine du serveur : ce serait du script exécuté avec
    les droits de l'interface. Le reste passe par `/download`, qui n'exécute
    rien.
    """
    rel = request.args.get("path", "")
    try:
        mime = workspace.inline_type(rel)
        p = workspace.resolve(rel)
    except workspace.WorkspaceError as e:
        return jsonify({"error": str(e)}), 400
    resp = send_file(p, mimetype=mime, as_attachment=False,
                     download_name=p.name)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; object-src 'self'"
    return resp


@app.route("/api/workspace/download")
def api_workspace_download():
    """Télécharge un fichier du dossier de travail.

    ⚠ **Toujours `as_attachment=True`, quel que soit le type.** Servir en ligne
    un fichier fourni par l'utilisateur le place sur l'origine du serveur : un
    `.html` déposé dans le dossier de travail deviendrait du script exécuté
    avec les droits de l'interface, et un PDF peut porter du JavaScript. Un
    téléchargement ne peut rien exécuter ici.
    """
    try:
        p = workspace.resolve(request.args.get("path", ""))
    except workspace.WorkspaceError as e:
        return jsonify({"error": str(e)}), 400
    if not p.is_file():
        return jsonify({"error": "pas un fichier"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


def _check_pdflatex():
    import shutil as _sh
    if _sh.which("pdflatex") is None:
        print("⚠ pdflatex introuvable — le bouton « Compiler » de l'onglet Sujet")
        print("  sera en erreur. Pour l'installer :")
        print("    Ubuntu/Debian : sudo apt install texlive-latex-extra texlive-lang-french")
        print("    macOS         : brew install --cask mactex-no-gui")
        print("    Windows       : https://miktex.org/download")
        print()


def main():
    ap = argparse.ArgumentParser(prog="amcx")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    _check_pdflatex()
    # Sujet ↔ calage : un décalage ici fausse silencieusement toutes les notes.
    try:
        check_layout_consistency()
    except Exception as e:  # noqa: BLE001 — jamais bloquant au démarrage
        print(f"⚠ vérification du calage impossible : {e}")
    print(f"Serving on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
