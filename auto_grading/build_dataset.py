"""Construit le dataset labellisé (features + label) pour le classifieur de cases.

Pour chaque copie utilisable (= avec au moins une source de label), on :
  1. Lit l'image, détecte les mires, warp canonique
  2. Calcule les offsets par question
  3. Pour chaque case Q1-Q31, extrait 17 features
  4. Détermine le label (filled=1 / empty=0) depuis :
       - relecture UI : copie `validated`/`manually_edited` → label = (char ∈ answers[q])
       - sinon AMC `manual ∈ {0,1}` (repli — signal plus ancien, parfois erroné)
       - sinon : cellule ignorée

Sortie : auto_grading/results/labeled_cells.parquet (ou .csv si pyarrow KO)

Usage:
  .venv/bin/python auto_grading/build_dataset.py
  .venv/bin/python auto_grading/build_dataset.py --limit 10   # debug rapide
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from cv_grade import (
    BoxLayout,
    FEATURE_COLS,
    adaptive_threshold,
    compute_copy_baseline,
    compute_per_question_offsets,
    detect_mires,
    extract_features,
    fill_ratio_shrink,
    load_layout,
    warp_to_canonical,
)
import config
import layout_store
import masked_detect

ROOT = config.project_root()  # projet actif : pour les données
PAGES_DIR = ROOT / "pages"
RAW_DIR = ROOT / "raw_responses"
RESULTS_DIR = ROOT / "results"


# ---------------------------------------------------------------- labels


def src_to_batch_page(src: str) -> tuple[str, int] | None:
    m = re.search(r"(\d+)_.*pdf-page-(\d+)-", src)
    if not m:
        return None
    return f"batch{m.group(1)}", int(m.group(2))


def load_amc_labels() -> dict:
    """(batch, page) → {(q, char): bool_ticked} pour les cells AMC manual ∈ {0,1}.

    Vide si l'examen n'a pas été analysé par AMC (pas de capture.sqlite)."""
    cap = config.amc_data_dir() / "capture.sqlite"
    if not cap.exists():
        return {}
    lay = layout_store.get_layout()
    idx_to_char = {(b.question, b.answer): b.char for b in lay.sheet_boxes()}

    c_cap = sqlite3.connect("file:%s?mode=ro" % cap, uri=True)
    copy_to_bp = {}
    for copy, src in c_cap.execute(
            "SELECT copy, src FROM capture_page WHERE page=?",
            (lay.answer_sheet_page,)):
        bp = src_to_batch_page(src)
        if bp:
            copy_to_bp[copy] = bp

    out: dict[tuple[str, int], dict[tuple[int, str], bool]] = defaultdict(dict)
    for copy, q, a, manual in c_cap.execute(
        "SELECT copy, id_a, id_b, manual FROM capture_zone "
        "WHERE student=1 AND type=4 AND manual IN (0, 1)"
    ):
        bp = copy_to_bp.get(copy)
        if not bp or (q, a) not in idx_to_char:
            continue
        out[bp][(q, idx_to_char[(q, a)])] = manual == 1
    c_cap.close()
    return dict(out)


def load_ui_labels() -> dict:
    """(batch, page) → {(q, char): bool}  pour les copies UI-validated.

    Pour une copie `validated` : toutes les Q×options ont un label défini par answers.
    Pour `manually_edited` sans `validated` : on prend uniquement les cells dont la
    décision est plus sûre que CV — soit cellules différentes de _cv_answers (édits
    explicites). On les ajoute (label = answers, source = ui_edit).
    """
    from sujet_store import effective_spec, parse_tex
    qopts = {q: effective_spec(q)["options"] for q in parse_tex()}

    out: dict[tuple[str, int], dict[tuple[int, str], tuple[bool, str]]] = defaultdict(dict)
    for batch_dir in sorted(RAW_DIR.iterdir()):
        if not batch_dir.is_dir():
            continue
        for jp in sorted(batch_dir.glob("page_*.json")):
            with open(jp, encoding="utf-8") as f:
                d = json.load(f)
            flags = set(d.get("_flags", []))
            answers = {int(k): set(v) for k, v in d.get("answers", {}).items()}
            cv_answers = {int(k): set(v) for k, v in d.get("_cv_answers", {}).items()}
            bp = (batch_dir.name, int(jp.stem.split("_")[1]))

            if "validated" in flags:
                for q, opts in qopts.items():
                    sel = answers.get(q, set())
                    for ch in opts:
                        out[bp][(q, ch)] = (ch in sel, "ui_validated")
            elif "manually_edited" in flags:
                for q, opts in qopts.items():
                    sel = answers.get(q, set())
                    cv_sel = cv_answers.get(q, set())
                    for ch in opts:
                        # uniquement les cells qui diffèrent du CV (édits explicites)
                        if (ch in sel) != (ch in cv_sel):
                            out[bp][(q, ch)] = (ch in sel, "ui_edit")
    return dict(out)


# ---------------------------------------------------------------- pipeline


def process_copy(batch: str, page: int, layout: list[BoxLayout],
                 canon_mires, canon_w: int, canon_h: int, qcm_set: set,
                 amc_labels: dict, ui_labels: dict,
                 ref, ref_frames: dict) -> list[dict]:
    """Pour une copie, sort N rows (1 par case labellisée). Retourne [] si pas d'image / mires."""
    img_path = PAGES_DIR / batch / f"page_{page:03d}.jpg"
    if not img_path.exists():
        return []
    img = cv2.imread(str(img_path))
    if img is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mires = detect_mires(gray)
    if mires is None or len(canon_mires) != 4:
        return []  # NO_MIRES — pas de label utilisable de toute façon
    warped = warp_to_canonical(gray, mires, canon_mires, canon_w, canon_h)
    offsets = compute_per_question_offsets(warped, layout)

    # Pré-calcul des ratios par case (shrink 0.18) — sert au copy baseline + context
    all_ratios = []
    ratios_by_q: dict[int, list[tuple[BoxLayout, float]]] = defaultdict(list)
    for b in layout:
        if b.question not in qcm_set:
            continue
        r = fill_ratio_shrink(warped, b, 0.18, offsets.get(b.question, (0, 0)))
        ratios_by_q[b.question].append((b, r))
        all_ratios.append(r)
    copy_baseline = compute_copy_baseline(all_ratios)

    bp = (batch, page)
    amc_lbl = amc_labels.get(bp, {})
    ui_lbl = ui_labels.get(bp, {})

    rows = []
    for q, boxes_r in ratios_by_q.items():
        q_ratios = [r for _, r in boxes_r]
        for b, _r in boxes_r:
            key = (q, b.char)
            label = None
            source = None
            # Priorité 1 : relecture UI (`answers`) — décision FINALE de l'utilisateur.
            # Priorité 2 : AMC `manual` — repli seulement. AMC est un signal plus
            #   ancien et parfois erroné (sur EXAM_2026, 27 cellules AMC≠UI, dont 26
            #   « AMC=vide / UI=cochée » sur des marques pâles).
            if key in ui_lbl:
                v, src = ui_lbl[key]
                label = int(v)
                source = src
            elif key in amc_lbl:
                label = int(amc_lbl[key])
                source = "amc_manual"
            if label is None:
                continue
            feats = extract_features(warped, b, q_ratios, copy_baseline,
                                     offsets.get(q, (0, 0)), ref=ref,
                                     ref_corners=ref_frames.get((q, b.answer)))
            row = {
                "copy_key": f"{batch}/page_{page:03d}",
                "batch": batch,
                "page": page,
                "q": q,
                "char": b.char,
                "label": label,
                "source": source,
                **feats,
            }
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="ne traite que les N premières copies (debug)")
    ap.add_argument("--out", type=str, default=None,
                    help="chemin de sortie (défaut: results/labeled_cells.parquet)")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(exist_ok=True)
    lay = layout_store.get_layout()
    layout = lay.sheet_boxes()
    canon_mires = np.asarray(lay.mires, dtype=np.float32)
    canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
    _by_q: dict[int, list] = {}
    for b in layout:
        if b.char:
            _by_q.setdefault(b.question, []).append(b.char)
    qcm_set = {q for q, chs in _by_q.items() if not all(c.isdigit() for c in chs)}
    print(f"Layout: {len(layout)} boxes, {len(qcm_set)} questions QCM")

    ref, ref_frames = masked_detect.get_reference(lay)
    n_ref = sum(v is not None for v in ref_frames.values())
    print(f"Référence masquée : {ref.shape}, cadres {n_ref}/{len(ref_frames)}")

    print("→ Chargement labels AMC (manual ∈ {0,1})…")
    amc_labels = load_amc_labels()
    print(f"   {len(amc_labels)} copies, {sum(len(v) for v in amc_labels.values())} cells")

    print("→ Chargement labels UI (validated + manually_edited)…")
    ui_labels = load_ui_labels()
    print(f"   {len(ui_labels)} copies, {sum(len(v) for v in ui_labels.values())} cells")

    # Toutes les copies à scanner = union des deux sources
    all_bp = set(amc_labels.keys()) | set(ui_labels.keys())
    targets = sorted(all_bp)
    if args.limit:
        targets = targets[: args.limit]
    print(f"→ Copies à traiter: {len(targets)}")

    rows = []
    for i, (batch, page) in enumerate(targets, 1):
        n_before = len(rows)
        rows.extend(process_copy(batch, page, layout, canon_mires, canon_w,
                                 canon_h, qcm_set, amc_labels, ui_labels,
                                 ref, ref_frames))
        n_added = len(rows) - n_before
        if i % 10 == 0 or i == len(targets):
            print(f"   [{i:3d}/{len(targets)}] {batch}/page_{page:03d}: +{n_added} (total {len(rows)})")

    if not rows:
        print("⚠️  Aucun row produit", file=sys.stderr)
        return 1

    df = pd.DataFrame(rows)
    out = Path(args.out) if args.out else (RESULTS_DIR / "labeled_cells.parquet")
    try:
        df.to_parquet(out, index=False)
    except Exception as e:
        print(f"⚠️  parquet KO ({e}), fallback CSV")
        out = out.with_suffix(".csv")
        df.to_csv(out, index=False)
    print(f"\n✓ Écrit: {out}  ({len(df)} rows, {df['label'].sum()} positifs = {100*df['label'].mean():.1f}%)")
    print(f"  Sources : {df['source'].value_counts().to_dict()}")
    print(f"  Copies  : {df['copy_key'].nunique()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
