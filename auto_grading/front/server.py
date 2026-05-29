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
import io
import json
import math
import os
import secrets
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from threading import Lock

import cv2
import fitz  # PyMuPDF — rendu des aperçus PDF
import numpy as np
from flask import Flask, abort, jsonify, render_template, request, send_file, url_for
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
from cv_grade import (detect_mires, warp_to_canonical, load_layout,
                      compute_per_question_offsets, load_name_field)
from student_list import StudentMatcher
from score import score_copy, score_question
from sujet_store import (parse_tex, save_questions, compile_pdf, SUJET_DIR,
                         effective_spec, pdf_regions,
                         max_score as sujet_max_score, total_max as sujet_total_max,
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
from config import load_config, save_config


def _bank():
    """Dispatcher banque locale ↔ Supabase selon `config.bank_mode`.

    Le module retourné expose les mêmes fonctions (load, save, delete,
    list_questions, update_project_stats, from_block, to_block). Switch à
    chaud par l'user via Réglages → Banque sans redémarrer le serveur.
    """
    return bank_online if load_config().get("bank_mode") == "online" else bank
from grade_imports import (read_table, analyze_table, match_report, set_name_override,
                           jsonable_cell, build_all_series, add_grade_file,
                           remove_grade_file, ensure_imports_dir, IMPORTS_DIR)

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
    """Variables disponibles dans tous les templates : nom du projet actif + récents.

    Utilisé par la zone projet de la topbar (`base.html`). Le nom affiché est le
    parent du dossier `auto_grading/` (cf. `project_state.display_name()`)."""
    p = config.project_root()
    valid = project_state.is_valid_project(p)
    return {
        "project_name": project_state.display_name(p) if valid else "Aucun projet",
        "project_path": str(p) if valid else "",
        "project_recent": project_state.recent_projects(),
        "app_name": project_state.APP_NAME,
    }


# Caches
_layout_cache = None
_layout_by_qchar = None
_warp_cache = {}   # (batch, page) -> warped gray
_offsets_cache = {}  # (batch, page) -> {q: (dx, dy)}
_warp_lock = Lock()
_matcher_cache = None
_series_cache = None   # séries de notes importées, jointes aux étudiants


def get_layout():
    global _layout_cache, _layout_by_qchar
    if _layout_cache is None:
        _layout_cache = load_layout()
        _layout_by_qchar = {}
        for b in _layout_cache:
            _layout_by_qchar[(b.question, b.char)] = b
    return _layout_cache, _layout_by_qchar


def question_numbers() -> tuple[list, list]:
    """(questions QCM, colonnes du code étudiant) dérivées du calage.

    Une « question » est une colonne de code si ses cases portent des chiffres."""
    layout, _ = get_layout()
    by_q: dict[int, list] = {}
    for b in layout:
        by_q.setdefault(b.question, []).append(b)
    qcm, ids = [], []
    for q, bs in sorted(by_q.items()):
        chars = [b.char for b in bs if b.char]
        (ids if (chars and all(c.isdigit() for c in chars)) else qcm).append(q)
    return qcm, ids


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


def get_warped(batch: str, page: int) -> tuple[np.ndarray, dict]:
    """Retourne (warped_image, offsets_par_question)."""
    key = (batch, page)
    if key in _warp_cache:
        return _warp_cache[key], _offsets_cache[key]
    with _warp_lock:
        if key in _warp_cache:
            return _warp_cache[key], _offsets_cache[key]
        img_path = PAGES_DIR / batch / f"page_{page:03d}.jpg"
        img = cv2.imread(str(img_path))
        if img is None:
            raise FileNotFoundError(img_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lay = layout_store.get_layout()
        canon_mires = np.asarray(lay.mires, dtype=np.float32)
        canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
        mires = detect_mires(gray)
        if mires is None or len(canon_mires) != 4:
            warped = cv2.resize(gray, (canon_w, canon_h))
        else:
            warped = warp_to_canonical(gray, mires, canon_mires, canon_w, canon_h)
        layout, _ = get_layout()
        offsets = compute_per_question_offsets(warped, layout)
        if len(_warp_cache) > 20:
            old = next(iter(_warp_cache))
            _warp_cache.pop(old)
            _offsets_cache.pop(old, None)
        _warp_cache[key] = warped
        _offsets_cache[key] = offsets
        return warped, offsets


def load_copy_json(batch: str, page: int) -> dict | None:
    p = RAW_DIR / batch / f"page_{page:03d}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_copy_json(batch: str, page: int, data: dict):
    p = RAW_DIR / batch / f"page_{page:03d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
    """Comme matcher.resolve() mais honore d['_student_override'] (id canonique posé manuellement)."""
    override = d.get("_student_override")
    if override:
        s = matcher.by_full_id(str(override))
        if s is not None:
            return {"matched": s, "method": "override", "score": 1.0, "flag": ""}
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
            out.append({
                "batch": batch,
                "page": page,
                "student_id": sid,
                "student_name": name,
                "canonical_name": f"{canon.nom} {canon.prenom}" if canon else "?",
                "canonical_id": canon.id if canon else "",
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


# Palette des séries (QCM = index 0, puis colonnes importées)
SERIES_COLORS = ["#0a6ed1", "#e8820c", "#1f9d57", "#9b59b6",
                 "#d6485a", "#0a9bb5", "#c0392b", "#7f8c8d"]


def _is_to_review(c: dict) -> bool:
    """Copie nécessitant une vérification (diff CV/AMC, ambiguë, ou problème de lecture)."""
    if c.get("cv_amc_diff_count"):
        return True
    fl = c.get("flags", [])
    return any(f in fl or f.startswith("cv_differs_amc")
               for f in ("ambiguous", "no_mires", "id_incomplet"))


def series_stats(values: list) -> dict:
    """Statistiques d'une série de valeurs : n, moyenne, variance, σ, médiane, min, max."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": 0.0, "variance": 0.0, "std": 0.0,
                "median": 0.0, "min": 0.0, "max": 0.0}
    mean = sum(vals) / n
    variance = sum((v - mean) ** 2 for v in vals) / n   # variance population (÷n)
    std = variance ** 0.5
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"n": n, "mean": mean, "variance": variance, "std": std,
            "median": median, "min": vals[0], "max": vals[-1]}


def rescale(raw: float, seuil: float, max_: float) -> float:
    """Note rescalée : raw × max / seuil (linéaire, sans plancher ni plafond)."""
    return raw * max_ / seuil if seuil else 0.0


def grade_columns(cfg: dict, imported: list) -> list:
    """Descripteurs unifiés des colonnes de note : QCM puis colonnes importées.

    Chacun : {key, label, color, seuil, max, agg_weight, path?, idx?}.
    QCM lit qcm_* ; les colonnes importées lisent grade_files[*].grade_cols[*].
    """
    cols = [{
        "key": "qcm", "label": "QCM", "color": SERIES_COLORS[0],
        "seuil": float(cfg.get("qcm_seuil", 32.0)),
        "max": float(cfg.get("qcm_max", 20.0)),
        "agg_weight": float(cfg.get("qcm_agg_weight", 1.0)),
    }]
    params = {}
    for fc in cfg.get("grade_files", []):
        for gc in fc.get("grade_cols", []):
            params[(fc["path"], gc.get("idx"))] = gc
    for i, s in enumerate(imported):
        gc = params.get((s["file"], s["idx"]), {})
        cols.append({
            "key": f'{s["file"]}::{s["idx"]}',
            "path": s["file"], "idx": s["idx"], "label": s["name"],
            "color": SERIES_COLORS[(i + 1) % len(SERIES_COLORS)],
            "seuil": float(gc.get("seuil", 20.0)),
            "max": float(gc.get("max", 20.0)),
            "agg_weight": float(gc.get("agg_weight", 1.0)),
        })
    return cols


def build_calibration_series(copies: list, columns: list, imported: list) -> list:
    """Séries RESCALÉES pour le graphe de calibration : une par colonne de note."""
    by_key = {f'{s["file"]}::{s["idx"]}': s for s in imported}
    out = []
    for col in columns:
        if col["key"] == "qcm":
            vals = [rescale(c["score"], col["seuil"], col["max"]) for c in copies]
        else:
            vmap = (by_key.get(col["key"]) or {}).get("values", {})
            vals = [rescale(vmap[c["canonical_id"]], col["seuil"], col["max"])
                    for c in copies
                    if c["canonical_id"] and c["canonical_id"] in vmap]
        out.append({"name": col["label"], "color": col["color"], "values": vals,
                    "opacity": 0.5 if col["key"] == "qcm" else 0.45})
    return out


def copy_rescaled(copy: dict, columns: list, imported: list) -> list:
    """Notes rescalées d'une copie, par colonne présente : [(col, N*), …]."""
    by_key = {f'{s["file"]}::{s["idx"]}': s for s in imported}
    cid = copy.get("canonical_id")
    out = []
    for col in columns:
        if col["key"] == "qcm":
            out.append((col, rescale(copy["score"], col["seuil"], col["max"])))
        else:
            vmap = (by_key.get(col["key"]) or {}).get("values", {})
            if cid and cid in vmap:
                out.append((col, rescale(vmap[cid], col["seuil"], col["max"])))
    return out


def compute_aggregate(copy: dict, columns: list, imported: list,
                      final_threshold: float) -> float:
    """Note finale : moyenne pondérée des notes rescalées, plafonnée au seuil final.

    Les notes rescalées entrent NON plafonnées dans la moyenne ; seul le résultat
    final reçoit le plafond dur `final_threshold`."""
    num = den = 0.0
    for col, val in copy_rescaled(copy, columns, imported):
        num += col["agg_weight"] * val
        den += col["agg_weight"]
    agg = num / den if den else 0.0
    return min(agg, final_threshold)


def build_formula(columns: list, final_threshold: float) -> dict:
    """Structure de la formule de la note finale (affichée pour les étudiants)."""
    terms = [{"label": c["label"], "weight": c["agg_weight"],
              "seuil": c["seuil"], "max": c["max"]} for c in columns]
    return {"terms": terms, "denom": sum(c["agg_weight"] for c in columns),
            "threshold": final_threshold}


def build_scatter(copies: list, columns: list, imported: list,
                  finals: list) -> dict:
    """Données du nuage de points : variables sélectionnables + 1 point par copie.

    Valeurs = notes rescalées (note*) par colonne + note finale. `None` si la
    copie n'a pas cette note."""
    variables = [{"id": c["key"], "label": c["label"]} for c in columns]
    variables.append({"id": "__final__", "label": "Note finale"})
    points = []
    for c, fin in zip(copies, finals):
        resc = {col["key"]: val for col, val in copy_rescaled(c, columns, imported)}
        v = {col["key"]: (round(resc[col["key"]], 3) if col["key"] in resc else None)
             for col in columns}
        v["__final__"] = round(fin, 3)
        points.append({"name": c["canonical_name"], "v": v})
    return {"variables": variables, "points": points}


def compute_multi_series_stats(series: list, granularity: float) -> dict:
    """Bins de largeur fixe `granularity` (alignés sur des multiples), partagés."""
    g = granularity if (granularity and granularity > 0) else 1.0
    all_vals = [v for s in series for v in s["values"]]
    if all_vals:
        lo = math.floor(min(0.0, min(all_vals)) / g) * g
        hi = math.ceil(max(all_vals) / g) * g
    else:
        lo, hi = 0.0, g
    if hi <= lo:
        hi = lo + g
    nbins = max(1, min(400, round((hi - lo) / g)))
    hi = lo + nbins * g
    w = (hi - lo) / nbins
    out_series, max_count = [], 0
    for s in series:
        counts = [0] * nbins
        for v in s["values"]:
            idx = min(nbins - 1, max(0, int((v - lo) / w)))
            counts[idx] += 1
        max_count = max(max_count, max(counts, default=0))
        out_series.append({"name": s["name"], "color": s["color"],
                           "opacity": s.get("opacity", 0.45), "counts": counts,
                           "stats": series_stats(s["values"])})
    return {"lo": lo, "hi": hi, "nbins": nbins, "series": out_series,
            "max_count": max_count}


def multi_histogram_geometry(mstats: dict, width: int = 560, height: int = 210,
                             mean_line: bool = False, vline: float | None = None) -> dict:
    """Géométrie SVG : barres superposées (une couleur/alpha par série) + ticks.

    `vline` : si fourni, ajoute une ligne verticale (x clampé au cadre)."""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 10, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_t + plot_h
    nbins = mstats["nbins"]
    lo, hi = mstats["lo"], mstats["hi"]
    span = (hi - lo) or 1.0
    max_c = mstats["max_count"] or 1
    slot = plot_w / nbins

    def sx(value):
        return pad_l + (value - lo) / span * plot_w

    series_geo = []
    for s in mstats["series"]:
        bars = []
        for i, c in enumerate(s["counts"]):
            bh = (c / max_c) * plot_h
            bars.append({
                "x": pad_l + i * slot + slot * 0.06, "w": slot * 0.88,
                "y": base_y - bh, "h": bh, "count": c,
                "cx": pad_l + i * slot + slot / 2,
                "lo": round(lo + i * span / nbins, 2),
                "hi": round(lo + (i + 1) * span / nbins, 2),
            })
        series_geo.append({"name": s["name"], "color": s["color"],
                           "opacity": s["opacity"], "bars": bars, "stats": s["stats"]})
    tick_vals = [round(lo + (hi - lo) * f, 1) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    xticks = [{"x": sx(v), "label": f"{v:g}"} for v in tick_vals]
    geo = {"width": width, "height": height, "base_y": base_y, "pad_t": pad_t,
           "series": series_geo, "xticks": xticks,
           "legend": [{"name": s["name"], "color": s["color"]} for s in series_geo]}
    if mean_line and series_geo:
        st = series_geo[0]["stats"]
        band_lo = max(lo, st["mean"] - st["std"])
        band_hi = min(hi, st["mean"] + st["std"])
        geo["mean_x"] = sx(st["mean"])
        geo["band_x"] = sx(band_lo)
        geo["band_w"] = sx(band_hi) - sx(band_lo)
        geo["mean"] = st["mean"]
    if vline is not None:
        geo["vline_x"] = min(max(sx(vline), pad_l), pad_l + plot_w)
        geo["vline_val"] = vline
    return geo


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
    for q in question_numbers()[0]:
        spec = effective_spec(q, copy=copy)
        sel = sorted(answers_int.get(q, []))
        per_question.append({
            "q": q, "tag": spec["tag"], "type": spec["type"],
            "correct": "".join(sorted(spec["correct"])),
            "selected": "".join(sel) or "—",
            "score": scores["per_question"][q],
            "max": sujet_max_score(q, copy=copy),
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
        "n_students":   0,
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
        if xpath.exists():
            try:
                from student_list import StudentMatcher as _SM
                m = _SM(); info["n_students"] = len(getattr(m, "students", []) or [])
            except Exception:
                info["n_students"] = 0
    return info


@app.route("/")
def index():
    # Pas de projet actif valide → page d'accueil (onboarding).
    p = config.project_root()
    if not project_state.is_valid_project(p):
        return render_template("onboarding.html",
                               recent=project_state.recent_projects(),
                               default_root=str(project_state.DEFAULT_PROJECTS_ROOT),
                               active="onboarding")

    copies = list_all_copies()
    cfg = load_config()
    g = float(cfg.get("hist_granularity", 1.0))
    imported = get_series()
    columns = grade_columns(cfg, imported)

    # haut : calibration — séries rescalées superposées
    top_series = build_calibration_series(copies, columns, imported)
    hist_top = multi_histogram_geometry(
        compute_multi_series_stats(top_series, g), mean_line=False)

    # bas : note finale agrégée (moyenne pondérée des notes rescalées, plafonnée)
    threshold = float(cfg.get("final_threshold", 20.0))
    pass_mark = float(cfg.get("pass_mark", 10.0))
    finals = [compute_aggregate(c, columns, imported, threshold) for c in copies]
    bottom = compute_multi_series_stats(
        [{"name": "Note finale", "color": SERIES_COLORS[0],
          "values": finals, "opacity": 1.0}], g)
    hist_bottom = multi_histogram_geometry(bottom, mean_line=True, vline=pass_mark)

    stats = dict(bottom["series"][0]["stats"])
    stats["n_validated"] = sum(1 for c in copies if c["validated"])
    stats["n_to_review"] = sum(1 for c in copies if _is_to_review(c))
    stats["pass_mark"] = pass_mark
    stats["n_below"] = sum(1 for v in finals if v < pass_mark)

    return render_template("dashboard.html", copies=copies, total=len(copies),
                           stats=stats, cfg=cfg, columns=columns,
                           formula=build_formula(columns, threshold),
                           hist_top=hist_top, hist_bottom=hist_bottom,
                           scatter=build_scatter(copies, columns, imported, finals),
                           grade_files_info=build_grade_files_info(cfg, imported),
                           project_files=_project_files_info(),
                           active="dashboard")


@app.route("/api/student-card/<batch>/<int:page>")
def api_student_card(batch, page):
    card = build_student_card(batch, page)
    if card is None:
        abort(404)
    return render_template("_student_card.html", c=card)


@app.route("/export.csv")
def export_csv():
    """CSV récap des notes (1 ligne / étudiant), régénéré à la volée."""
    copies = list_all_copies()
    cfg = load_config()
    imported = get_series()
    columns = grade_columns(cfg, imported)
    threshold = float(cfg.get("final_threshold", 20.0))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["batch", "page", "id_canonique", "nom_prenom", "id_lu",
                "note_sur_32", "note_finale", "validee", "flags"])
    for c in sorted(copies, key=lambda x: x["canonical_name"]):
        w.writerow([
            c["batch"], c["page"], c["canonical_id"], c["canonical_name"],
            c["student_id"], round(c["score"], 2),
            round(compute_aggregate(c, columns, imported, threshold), 2),
            "oui" if c["validated"] else "non",
            ";".join(c["flags"]),
        ])
    out = buf.getvalue()
    return app.response_class(
        out, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qcm_notes.csv"},
    )


def _pos_float(body, key, allow_zero=False):
    """Float strictement positif (ou ≥0 si allow_zero). Lève ValueError sinon."""
    v = float(body[key])
    if v < 0 or (v == 0 and not allow_zero):
        raise ValueError(key)
    return v


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """GET → config courante ; POST → granularité + paramètres seuil/max/poids."""
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
                updates["qcm_seuil"] = _pos_float(body, "qcm_seuil")
            if "qcm_max" in body:
                updates["qcm_max"] = _pos_float(body, "qcm_max")
            if "qcm_agg_weight" in body:
                updates["qcm_agg_weight"] = _pos_float(body, "qcm_agg_weight",
                                                       allow_zero=True)
            if "final_threshold" in body:
                updates["final_threshold"] = _pos_float(body, "final_threshold")
            if "pass_mark" in body:
                updates["pass_mark"] = _pos_float(body, "pass_mark", allow_zero=True)
            if "anthropic_api_key" in body:
                updates["anthropic_api_key"] = str(body["anthropic_api_key"] or "").strip()
            if "ai_model" in body:
                m = str(body["ai_model"] or "").strip()
                allowed = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
                if m and m not in allowed:
                    return jsonify({"error": f"Modèle inconnu : {m}"}), 400
                updates["ai_model"] = m or "claude-sonnet-4-6"
            # Banque (mode + credentials Supabase). Les tokens user (bank_user_token,
            # bank_refresh_token, etc.) sont posés par bank_auth, pas via cette route.
            if "bank_mode" in body:
                m = str(body["bank_mode"] or "local").strip()
                if m not in ("local", "online"):
                    return jsonify({"error": f"bank_mode invalide : {m}"}), 400
                updates["bank_mode"] = m
            if "bank_supabase_url" in body:
                updates["bank_supabase_url"] = str(body["bank_supabase_url"] or "").strip()
            if "bank_supabase_anon_key" in body:
                updates["bank_supabase_anon_key"] = str(body["bank_supabase_anon_key"] or "").strip()
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
        return jsonify({"ok": True, "config": cfg})
    return jsonify(load_config())


@app.route("/api/save-report", methods=["POST"])
def api_save_report():
    """Crée/met à jour le dossier `compte_rendu/` : notes.csv + graphiques SVG.

    N'écrit JAMAIS dans raw_responses/ — uniquement compte_rendu/."""
    body = request.get_json(force=True)
    report_dir = ROOT / "compte_rendu"
    report_dir.mkdir(exist_ok=True)
    copies = list_all_copies()
    cfg = load_config()
    imported = get_series()
    columns = grade_columns(cfg, imported)
    threshold = float(cfg.get("final_threshold", 20.0))
    by_key = {f'{s["file"]}::{s["idx"]}': s for s in imported}
    imp_cols = [c for c in columns if c["key"] != "qcm"]

    # notes.csv : notes brutes (QCM + colonnes importées) + note finale agrégée
    header = (["batch", "page", "id_canonique", "nom_prenom", "QCM_brut_sur_32"]
              + [f'{c["label"]}_brut' for c in imp_cols]
              + ["note_finale", "validee"])
    with open(report_dir / "notes.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        n_rows = 0
        for c in sorted(copies, key=lambda x: x["canonical_name"]):
            row = [c["batch"], c["page"], c["canonical_id"], c["canonical_name"],
                   round(c["score"], 2)]
            for col in imp_cols:
                s = by_key.get(col["key"])
                val = s["values"].get(c["canonical_id"]) if s else None
                row.append("" if val is None else round(val, 2))
            row.append(round(compute_aggregate(c, columns, imported, threshold), 2))
            row.append("oui" if c["validated"] else "non")
            w.writerow(row)
            n_rows += 1

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


@app.route("/api/upload-xlsx", methods=["POST"])
def api_upload_xlsx():
    """Reçoit un .xlsx, le stocke en pending, renvoie ses colonnes d'en-tête."""
    f = request.files.get("file")
    if f is None or not f.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "fichier .xlsx attendu"}), 400
    pending = ROOT / "student_list_pending.xlsx"
    f.save(pending)
    try:
        from student_list import xlsx_header
        cols = xlsx_header(pending)
    except Exception as e:
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    return jsonify({"ok": True, "columns": cols})


@app.route("/api/student-list", methods=["POST"])
def api_student_list():
    """Valide le fichier pending + le mapping de colonnes → config + rebuild matcher."""
    global _matcher_cache, _series_cache
    body = request.get_json(force=True)
    pending = ROOT / "student_list_pending.xlsx"
    if not pending.exists():
        return jsonify({"error": "aucun fichier uploadé"}), 400
    final = ROOT / "student_list.xlsx"
    pending.replace(final)
    save_config({
        "student_xlsx": "student_list.xlsx",
        "xlsx_id_col": body.get("id_col", ""),
        "xlsx_nom_col": body.get("nom_col", ""),
        "xlsx_prenom_col": body.get("prenom_col", ""),
    })
    _matcher_cache = None  # forcer le rechargement
    _series_cache = None   # la jointure des notes importées dépend du matcher
    try:
        n = len(get_matcher().students)
    except Exception as e:
        return jsonify({"error": f"liste invalide : {e}"}), 400
    return jsonify({"ok": True, "n_students": n})


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
    try:
        rows = read_table(dest)
        analysis = analyze_table(rows, get_matcher())
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"lecture impossible : {e}"}), 400
    if not analysis["ncol"]:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "fichier vide"}), 400
    analysis["rows_preview"] = [[jsonable_cell(c) for c in r] for r in rows[:14]]
    return jsonify({"ok": True, "path": f"imports/{dest.name}", "analysis": analysis})


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
    try:
        rows = read_table(full)
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
        # 3 groupes UI : unresolved (orange) / manual (bleu, posé à la main) /
        # auto (vert, détecté par la grille ID ou un fuzzy HTR).
        if not is_assigned:
            group = "unresolved"
        elif method == "override":
            group = "manual"
        else:                              # "id" ou "name_fuzzy"
            group = "auto"
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
    group_order = {"unresolved": 0, "manual": 1, "auto": 2}
    copies.sort(key=lambda c: (group_order[c["group"]], c["batch"], c["page"]))
    counts = {
        "unresolved": sum(1 for c in copies if c["group"] == "unresolved"),
        "manual":     sum(1 for c in copies if c["group"] == "manual"),
        "auto":       sum(1 for c in copies if c["group"] == "auto"),
    }
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
    """Fixe un chiffre du numéro étudiant (Q32-35) — single-select par colonne, persisté."""
    body = request.get_json(force=True)
    batch, page = body["batch"], int(body["page"])
    q = int(body["q"])
    char = str(body["char"])
    # "?" = effacer le chiffre (colonne marquée comme non-lue)
    if not (32 <= q <= 35) or (char not in "0123456789" and char != "?"):
        return jsonify({"error": "paramètres invalides"}), 400
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "copie introuvable"}), 404
    cur = d.get("student_id", "????") or "????"
    if "_cv_student_id" not in d:        # garder la lecture CV originale (immuable)
        d["_cv_student_id"] = cur
    digits = list((cur + "????")[:4])
    digits[q - 32] = char
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
    body = request.get_json(force=True)
    batch, page = body["batch"], int(body["page"])
    sid = str(body.get("student_id", "")).strip()
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
    for q in question_numbers()[0]:
        spec = effective_spec(q, copy=copy)
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
    qcm_qs, id_qs = question_numbers()
    qcm_set, id_set = set(qcm_qs), set(id_qs)
    try:
        _warped, offsets = get_warped(batch, page)
    except Exception:
        offsets = {}
    sid = (d.get("student_id") or "????")
    cells = []
    for b in lay.sheet_boxes():
        dx, dy = offsets.get(b.question, (0, 0))
        common = {
            "q": b.question, "char": b.char,
            "cx": (b.xmin + b.xmax) / 2.0 + dx,
            "cy": (b.ymin + b.ymax) / 2.0 + dy,
            "r": 0.45 * min(b.xmax - b.xmin, b.ymax - b.ymin),
        }
        if b.question in qcm_set:
            cells.append({**common, "kind": "qcm",
                          "selected": b.char in answers_int.get(b.question, [])})
        elif b.question in id_set:
            col_idx = b.question - 32      # 0..3 (set-id-digit attend q ∈ [32,35])
            cur = sid[col_idx] if 0 <= col_idx < len(sid) else "?"
            cells.append({**common, "kind": "id",
                          "selected": b.char == cur})

    # Grille zoom (cases recadrées) embarquée dans le panel droit
    zoom_questions = build_zoom_questions(d)

    # Liste de TOUTES les copies pour la sidebar (mêmes données que le dashboard)
    sidebar_copies = list_all_copies()

    return render_template("student.html",
                           batch=batch, page=page, data=d,
                           match=match, scores=scores, questions=questions, nav=nav,
                           cells=cells, canon_w=canon_w, canon_h=canon_h,
                           zoom_questions=zoom_questions,
                           sidebar_copies=sidebar_copies,
                           active="")


@app.route("/api/order")
def api_order():
    return jsonify([{"batch": b, "page": p} for (b, p) in list_ordered_keys()])


@app.route("/flagged")
def flagged():
    """Vue agrégée : toutes les cases flagguées (CV≠AMC ou ambiguës), groupées par étudiant.

    1 ligne par étudiant avec ses cases inline.
    """
    matcher = get_matcher()
    students = []
    if not RAW_DIR.exists():
        return render_template("flagged.html", students=students, total_cells=0,
                               filter_mode="all",
                               counts={"all": 0, "not_validated": 0, "validated": 0},
                               active="flagged")
    for batch_dir in sorted(RAW_DIR.iterdir()):
        if not batch_dir.is_dir():
            continue
        for jp in sorted(batch_dir.glob("page_*.json")):
            with open(jp, encoding="utf-8") as f:
                d = json.load(f)
            batch = batch_dir.name
            page = int(jp.stem.split("_")[1])
            cells = []
            seen = set()
            # diff CV/AMC
            for item in d.get("_cv_amc_diff", []):
                q, ch = item["q"], item["char"]
                if (q, ch) in seen:
                    continue
                seen.add((q, ch))
                cells.append({
                    "q": q, "char": ch,
                    "selected": ch in d.get("answers", {}).get(str(q), []),
                    "reason": "diff",
                    "cv": item["cv"], "amc": item["amc"],
                })
            # cases « douteuses » du levier 2 (multi-estimateurs : masqué / seuil / GBM)
            amb_new = d.get("_ambiguous_cells")
            if amb_new:
                for a in amb_new:
                    q, ch = a["q"], a["char"]
                    if (q, ch) in seen:
                        continue
                    seen.add((q, ch))
                    cells.append({
                        "q": q, "char": ch,
                        "selected": ch in d.get("answers", {}).get(str(q), []),
                        "reason": "doubtful",
                        "reasons": a.get("reasons", []),
                        "ratio": a.get("ratio"),
                        "masked": a.get("masked"),
                        "proba": a.get("proba"),
                    })
            else:
                # legacy : ambiguïté CV (ancien format `Qx_Y(0.42→ml:.../thr:...)` dans notes)
                notes = d.get("notes", "")
                for m in re.finditer(r"Q(\d+)_([A-Z])\(([0-9.]+)→([^)]+)\)", notes):
                    q = int(m.group(1)); ch = m.group(2); ratio = float(m.group(3))
                    if (q, ch) in seen:
                        continue
                    seen.add((q, ch))
                    cells.append({
                        "q": q, "char": ch,
                        "selected": ch in d.get("answers", {}).get(str(q), []),
                        "reason": "ambiguous", "ratio": ratio,
                    })
            if not cells:
                continue
            # ordonner par q puis char
            cells.sort(key=lambda c: (c["q"], c["char"]))
            sid = d.get("student_id", "????")
            name = d.get("student_name", "")
            match = resolve_student(d, matcher)
            canon = match["matched"]
            students.append({
                "batch": batch, "page": page,
                "student_id": sid,
                "canonical_name": f"{canon.nom} {canon.prenom}" if canon else "?",
                "canonical_id": canon.id if canon else "",
                "n_cells": len(cells),
                "cells": cells,
                "validated": "validated" in d.get("_flags", []),
                "flags": d.get("_flags", []),
                "id_questions": build_id_questions(d),
            })
    # Filtre par statut de validation (avant calcul des compteurs filtrés)
    filter_mode = request.args.get("status", "all")
    counts = {
        "all": len(students),
        "validated": sum(1 for s in students if s["validated"]),
        "not_validated": sum(1 for s in students if not s["validated"]),
    }
    if filter_mode == "validated":
        students = [s for s in students if s["validated"]]
    elif filter_mode == "not_validated":
        students = [s for s in students if not s["validated"]]
    total_cells = sum(s["n_cells"] for s in students)
    return render_template("flagged.html", students=students, total_cells=total_cells,
                           filter_mode=filter_mode, counts=counts, active="flagged")


import re  # noqa: E402  -- pour le parsing des notes ambigus


def build_zoom_questions(d: dict) -> list:
    """Construit la liste `questions` (cases en 2 zones) pour la grille de zoom."""
    answers_int = {int(k): set(v) for k, v in d.get("answers", {}).items()}
    diff_pairs = diff_set(d)
    doubt_pairs = doubt_set(d)
    copy = copy_id_of(d)
    questions = []
    for q in question_numbers()[0]:
        spec = effective_spec(q, copy=copy)
        sel = answers_int.get(q, set())
        cases = []
        for ch in spec["options"]:
            cases.append({
                "char": ch,
                "selected": ch in sel,
                "correct": ch in spec["correct"],
                "diff": (q, ch) in diff_pairs,
                "doubtful": (q, ch) in doubt_pairs,
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
    """Colonnes du numéro étudiant (Q32-35) : 10 cases-chiffres, le chiffre lu surligné."""
    sid = d.get("student_id", "") or ""
    cols = []
    for i, q in enumerate(question_numbers()[1]):
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
    body = request.get_json()
    batch = body["batch"]
    page = int(body["page"])
    q = str(body["q"])
    char = body["char"]
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    ans = d.get("answers", {}).get(q, [])
    if char in ans:
        ans = [c for c in ans if c != char]
    else:
        # respecter l'ordre des options de cette copie
        opts = effective_spec(int(q), copy=copy_id_of(d))["options"]
        ans = [c for c in opts if c in (set(ans) | {char})]
    d.setdefault("answers", {})[q] = ans
    # marquer comme modifié manuellement
    if "manually_edited" not in d.get("_flags", []):
        d.setdefault("_flags", []).append("manually_edited")
    save_copy_json(batch, page, d)
    return jsonify({"ok": True, "answers": d["answers"]})


@app.route("/api/save", methods=["POST"])
def api_save():
    body = request.get_json()
    batch = body["batch"]
    page = int(body["page"])
    payload = body["payload"]
    save_copy_json(batch, page, payload)
    return jsonify({"ok": True})


@app.route("/api/mark_validated", methods=["POST"])
def api_mark_validated():
    body = request.get_json()
    batch = body["batch"]
    page = int(body["page"])
    d = load_copy_json(batch, page)
    if d is None:
        return jsonify({"error": "not found"}), 404
    flags = d.get("_flags", [])
    if "validated" not in flags:
        flags.append("validated")
    d["_flags"] = flags
    save_copy_json(batch, page, d)
    return jsonify({"ok": True})


@app.route("/img/<batch>/<int:page>.jpg")
def img(batch, page):
    p = PAGES_DIR / batch / f"page_{page:03d}.jpg"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/jpeg")


@app.route("/img_canon/<batch>/<int:page>.jpg")
def img_canon(batch, page):
    """Image warpée dans l'espace canonique (mires alignées, mêmes coords que `layout_store.Box`).

    Sert pour l'overlay SVG de la vue copie (ronds magenta sur les cases cochées).
    Cache disque, invalidé au mtime du JPEG source."""
    src = PAGES_DIR / batch / f"page_{page:03d}.jpg"
    if not src.exists():
        abort(404)
    cache_dir = ZOOM_CACHE / f"{batch}_page_{page:03d}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "canon.jpg"
    if cache_path.exists() and cache_path.stat().st_mtime >= src.stat().st_mtime:
        return send_file(cache_path, mimetype="image/jpeg")
    warped, _ = get_warped(batch, page)
    cv2.imwrite(str(cache_path), warped, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return send_file(cache_path, mimetype="image/jpeg")


@app.route("/zoom_img/<batch>/<int:page>/<q>_<char>.jpg")
def zoom_img(batch, page, q, char):
    cache_dir = ZOOM_CACHE / f"{batch}_page_{page:03d}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"Q{q}_{char}.jpg"
    if cache_path.exists():
        return send_file(cache_path, mimetype="image/jpeg")

    layout, by_qchar = get_layout()
    key = (int(q), char)
    if key not in by_qchar:
        abort(404)
    b = by_qchar[key]
    warped, offsets = get_warped(batch, page)
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
    # Mapping `tag AMC → numéro de question dans le .xy`. Construit depuis
    # `layout_store.question_names` (extrait du fichier exam.xy à la compilation).
    # Sert à retrouver le numéro AMC d'une question_open / d'une barème answerbox,
    # vu que ces deux types passent par `\element{group}{…}\insertgroup{group}`
    # → leur position dans le .xy n'est PAS dans l'ordre du document.
    tag_to_q: dict[str, int] = {}
    try:
        _lay = layout_store.get_layout()
        for q_num, tag in _lay.question_names.items():
            tag_to_q[tag.strip()] = q_num
    except Exception:
        pass

    # Pour chaque bloc question_qcm, enrichir avec le mapping lettre AMC + max_score.
    # Indexation par ordre des blocs `question_qcm` (q=1,2,…) — cohérent avec parse_tex().
    # `preview_q` (numéro AMC global) sert à pointer vers `/sujet/region/<q>.png` pour
    # l'aperçu PDF.
    qs_by_order = parse_tex()
    q_seq = 0          # numéro de QCM (1,2,…) pour le mapping lettre/barème
    qcm_seq = 0        # compteur AMC pour les QCM (= q_seq ici, mais pour clarté)
    enriched = []
    for b in sub["blocks"]:
        item = {"bid": b.bid, "kind": b.kind, "data": b.data}
        if b.kind == "question_qcm":
            q_seq += 1
            qcm_seq += 1
            info = qs_by_order.get(q_seq, {})
            item["q"] = q_seq
            item["preview_q"] = qcm_seq
            item["answers_with_char"] = info.get("answers", [])
            item["max"] = sujet_max_score(q_seq)
        elif b.kind == "question_open":
            tag = (b.data.get("tag") or "").strip()
            q_num = tag_to_q.get(tag)
            if q_num:
                item["preview_q"] = q_num
            item["max"] = float(b.data.get("points") or 0.0)
        elif b.kind == "question_freeform":
            tag = (b.data.get("tag") or "").strip()
            q_num = tag_to_q.get(tag)
            if q_num:
                item["preview_q"] = q_num
            item["max"] = float(b.data.get("points") or 0.0)
        elif b.kind == "answerbox":
            try:
                bareme_max = int(b.data.get("bareme_max") or 0)
            except (TypeError, ValueError):
                bareme_max = 0
            if bareme_max > 0:
                q_num = tag_to_q.get(f"bareme-{b.bid}")
                if q_num:
                    item["preview_q"] = q_num
        enriched.append(item)

    pdf = SUJET_DIR / "DOC-sujet.pdf"
    try:
        available_copies = list(layout_store.get_available_copies())
    except Exception:
        available_copies = []
    return render_template(
        "sujet.html",
        blocks=enriched,
        config=cfg,                                  # SubjectConfig (num_copies, seed, shuffle_*)
        header=cfg.header,                           # HeaderBlock (title, author, …)
        answer_sheet=cfg.answer_sheet,               # AnswerSheetConfig
        answer_sheet_tex=cfg.answer_sheet_tex,       # LaTeX brut (prime sur les champs si non vide)
        mode=sub["mode"],                            # 'canonical' | 'legacy' | 'empty'
        available_copies=available_copies,
        total_max=sujet_total_max(),
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


@app.route("/api/sujet/compile", methods=["POST"])
def api_sujet_compile():
    """Recompile exam.tex → sujet/DOC-sujet.pdf. Renvoie {ok, log, pdf_mtime}."""
    result = compile_pdf()
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
    """Ajoute un bloc. Body: {kind, after_bid?, data?} → {bid}."""
    body = _json_body()
    kind = body.get("kind", "")
    after_bid = body.get("after_bid")
    data = body.get("data") or None
    try:
        bid = sujet_add_block(kind, after_bid=after_bid, data=data)
        return jsonify({"ok": True, "bid": bid})
    except Exception as e:
        return _crud_error(e)


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
    """Auteur par défaut : email connecté en mode online, sinon
    header.author du sujet courant, sinon vide."""
    cfg = load_config()
    if cfg.get("bank_mode") == "online" and cfg.get("bank_user_email"):
        return cfg["bank_user_email"]
    try:
        sub = parse_subject()
        return (sub["config"].header.author or "").strip()
    except Exception:
        return ""


@app.route("/api/bank")
def api_bank_list():
    """Liste les questions de la banque. Query: ?kind=&q=&tags=t1,t2&author=…"""
    args = request.args
    tags = [t.strip() for t in (args.get("tags") or "").split(",") if t.strip()]
    filters = {
        "kind":   args.get("kind", ""),
        "q":      args.get("q", ""),
        "tags":   tags,
        "author": args.get("author", ""),
        # Phase B (online only — ignored by bank.py local)
        "mes_favoris": args.get("mes_favoris") in ("1", "true", "yes"),
        "mon_tag":     args.get("mon_tag", ""),
        "status":      args.get("status", ""),
    }
    try:
        b = _bank()
        items = b.list_questions(filters)
        all_tags: set[str] = set()
        for q in b.list_questions(None):
            for t in q.get("tags") or []:
                all_tags.add(t)
        return jsonify({"ok": True, "items": items,
                        "all_tags": sorted(all_tags)})
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


@app.route("/api/bank", methods=["POST"])
def api_bank_save():
    """Sauve un bloc du sujet courant dans la banque.

    Body: {bid, title, tags:[str], author?}. Le bloc est lu dans le sujet
    actif, ses champs internes sont strippés, un bank_id frais est généré.
    """
    body = _json_body()
    bid = (body.get("bid") or "").strip()
    title = (body.get("title") or "").strip()
    tags = body.get("tags") or []
    author = (body.get("author") or "").strip() or _bank_author_default()
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
                         tags=tags, author=author)
        saved = b.save(q)
        # save() local retourne un Path, online retourne la question. On
        # uniformise : prend l'id depuis ce qui est disponible.
        bid = (saved or {}).get("bank_id") if isinstance(saved, dict) else q.get("bank_id")
        return jsonify({"ok": True, "bank_id": bid, "title": q["title"]})
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
                for bid_bank, q_num in instances:
                    sel = ans.get(q_num) or []
                    try:
                        sc = score_question(q_num, sel, copy=copy_id)
                        mx = sujet_max_score(q_num, copy=copy_id)
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
    """Retourne {mode, configured, logged_in, user_id, email, ...}."""
    cfg = load_config()
    st = bank_auth.auth_status()
    st["mode"] = cfg.get("bank_mode", "local")
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
    if load_config().get("bank_mode") != "online":
        raise RuntimeError("Cette fonctionnalité nécessite le mode 'En ligne' "
                           "(Réglages → Banque).")


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
import uuid as _uuid

# Tâches asynchrones : task_id → {status, step, progress, log[], started_at,
# error?, n_extracted?, n_graded?, n_seeded?, finished_at?}
_PIPE_TASKS: dict = {}


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
    # Garbage collect : si terminée depuis > 1h, on peut purger
    return jsonify({"ok": True, **t})


# --------------------------------------------------------------------------
# Onglet « Questions » : ranking + stats par question (QCM only) + aperçu PDF
# --------------------------------------------------------------------------

@app.route("/api/questions/stats")
def api_questions_stats():
    """Stats par question QCM (depuis raw_responses/) pour le projet actif.

    Pour chaque QCM en ordre document, retourne `{q, tag, type, statement,
    max_score, n_eval, n_perfect, mean (normalisé ∈ [-∞,1]), scores: [score
    brut par copie], bank_id}`. Skip open/answerbox (pas de note auto).
    Le front s'en sert pour ranker la liste et tracer l'histogramme.
    """
    try:
        sub = parse_subject()
        qs = parse_tex()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not qs:
        return jsonify({"ok": True, "questions": [], "total_copies": 0})

    # q_num → block AMCx (pour récupérer `_bank_id`). En canonique l'ordre
    # des QCM dans `sub["blocks"]` correspond aux clés de `parse_tex()`.
    qcm_blocks_in_order = [b for b in (sub.get("blocks") or [])
                           if b.kind == "question_qcm"]
    q_to_block = {i: b for i, b in enumerate(qcm_blocks_in_order, start=1)}

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
                for q_num in qs:
                    sel = ans.get(q_num) or []
                    try:
                        sc = score_question(q_num, sel, copy=copy_id)
                        mx = sujet_max_score(q_num, copy=copy_id)
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
            "scores":    [round(s, 4) for s in scores],
            "bank_id":   (block.data.get("_bank_id") if block else None),
        })
    return jsonify({"ok": True, "questions": out, "total_copies": total_copies})


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


# ---------------------------------------------------------------------------
# Multi-projets : gestion du projet actif + liste des récents.
# Le switch de projet redémarre le process Flask (project_state.execv) pour
# garantir l'invalidation de TOUS les caches modules. Le browser doit attendre
# que le serveur revienne (~500-1500 ms) puis recharger la page.
# ---------------------------------------------------------------------------

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
    # Réponse renvoyée AVANT le restart : Flask flushe la réponse, puis on exec.
    # On utilise un after_request pour différer le restart d'un petit délai
    # via un thread daemon (le request handler doit retourner pour que la
    # réponse parte au client).
    import threading
    def _do_restart():
        import time
        time.sleep(0.2)
        project_state.restart_server_with_project(p)
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "active": str(p), "active_name": project_state.display_name(p)})


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
_AMC_UPLOAD_DIR = Path("/tmp/amcx_uploads")


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
    source_tex: Path | None = None

    if request.content_type and request.content_type.startswith("multipart/"):
        name = (request.form.get("name") or "").strip()
        template = (request.form.get("template") or "examen_minimal").strip()
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

    # Validation nom (slug-safe, sans `/`)
    import re as _re
    if not name or not _re.match(r"^[A-Za-z0-9_\-. ]+$", name) or name in {".", ".."}:
        return jsonify({"error": "Nom invalide (lettres, chiffres, espace, _ - .)."}), 400

    project_state.ensure_default_root()
    dest = project_state.DEFAULT_PROJECTS_ROOT / name
    if dest.exists():
        return jsonify({"error": f"Existe déjà : {dest}"}), 409

    try:
        ag = np_create(dest, template=template, source_tex=source_tex)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": f"Template inconnu : {e}"}), 400
    except Exception as e:
        return jsonify({"error": f"Échec création : {e}"}), 500

    # Restart dans un thread daemon pour que la réponse parte d'abord.
    import threading, time as _t
    def _do_restart():
        _t.sleep(0.2)
        project_state.restart_server_with_project(ag)
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({
        "ok": True,
        "path": str(ag),
        "name": project_state.display_name(ag),
    })


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
    print(f"Serving on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
