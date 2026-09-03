"""Benchmark cv_grade.py contre la ground truth utilisateur AMC.

Pour les 47 copies que l'utilisateur a corrigées manuellement dans AMC,
9071 cases ont `manual ∈ {0, 1}` = décision explicite utilisateur.
On compare la décision CV à cette vérité.

Usage:
  python cv_benchmark.py                 # benchmark complet
  python cv_benchmark.py --threshold 0.5 # tester un seuil spécifique
  python cv_benchmark.py --by-copy       # détail par copie
  python cv_benchmark.py --details       # 1 ligne par cellule mismatch
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from cv_grade import (
    load_layout, detect_mires, warp_to_canonical, box_fill_ratio,
    adaptive_threshold, compute_copy_baseline, compute_per_question_offsets,
    extract_features, load_cell_classifier,
)
import config
import layout_store
import masked_detect

ROOT = config.project_root()  # projet actif : pour les données
PAGES_DIR = ROOT / "pages"
RESULTS_DIR = ROOT / "results"


def _capture_db() -> Path:
    return config.amc_data_dir() / "capture.sqlite"


def src_to_batch_page(src: str) -> tuple[str, int] | None:
    m = re.search(r"(\d+)_.*pdf-page-(\d+)-", src)
    if not m:
        return None
    return f"batch{m.group(1)}", int(m.group(2))


def load_ground_truth() -> dict:
    """Pour chaque copy, retourne {(question, answer_idx): bool_ticked} (manual=0/1 uniquement)."""
    c = sqlite3.connect("file:%s?mode=ro" % _capture_db(), uri=True)
    truth: dict[int, dict[tuple[int, int], bool]] = defaultdict(dict)
    for copy, q, a, manual in c.execute("""
        SELECT copy, id_a, id_b, manual
        FROM capture_zone
        WHERE type=4 AND manual IN (0, 1)
    """):
        truth[copy][(q, a)] = (manual == 1)
    c.close()
    return dict(truth)


def load_copy_to_file(sheet_page: int) -> dict:
    """copy AMC -> (batch, page)."""
    c = sqlite3.connect("file:%s?mode=ro" % _capture_db(), uri=True)
    out = {}
    for copy, src in c.execute(
            "SELECT copy, src FROM capture_page WHERE page=?", (sheet_page,)):
        bp = src_to_batch_page(src)
        if bp:
            out[copy] = bp
    c.close()
    return out


def grade_image_raw(image_path: Path, layout, canon_mires, canon_w, canon_h,
                    shrink: float = 0.18, refine: bool = True, lay=None):
    """Comme cv_grade.grade_image mais retourne aussi warped+offsets pour features ML.

    Retourne (ratios_by_qa, warped, offsets, layout_by_qa) ou None.
    """
    img = cv2.imread(str(image_path))
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mires = detect_mires(gray, layout=lay)
    if mires is None or len(canon_mires) != 4:
        return None
    warped = warp_to_canonical(gray, mires, canon_mires, canon_w, canon_h)
    offsets = compute_per_question_offsets(warped, layout) if refine else {}
    ratios = {}
    layout_by_qa = {}
    for b in layout:
        off = offsets.get(b.question, (0, 0))
        ratios[(b.question, b.answer)] = (b.char, box_fill_ratio(warped, b, shrink=shrink, offset=off))
        layout_by_qa[(b.question, b.answer)] = b
    return ratios, warped, offsets, layout_by_qa


def run_benchmark(shrink: float,
                  by_copy: bool = False, details: bool = False, use_ml: bool = True):
    if not _capture_db().exists():
        print(f"capture.sqlite introuvable ({_capture_db()}).")
        print("Le benchmark compare le CV à la ground-truth AMC : il requiert un "
              "examen analysé par AMC.")
        return None
    lay = layout_store.get_layout()
    canon_mires = np.asarray(lay.mires, dtype=np.float32)
    canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
    layout = lay.sheet_boxes()
    # questions QCM (cases à lettres) vs colonnes du code étudiant (chiffres)
    _by_q: dict[int, list] = {}
    for b in layout:
        if b.char:
            _by_q.setdefault(b.question, []).append(b.char)
    qcm_set = {q for q, chs in _by_q.items() if not all(c.isdigit() for c in chs)}

    truth = load_ground_truth()
    copy_to_file = load_copy_to_file(lay.answer_sheet_page)
    clf_bundle = load_cell_classifier() if use_ml else None
    print(f"ML classifier: {'ON (toutes les cases)' if clf_bundle is not None else 'OFF'}")
    ref, ref_frames = (masked_detect.get_reference(lay) if use_ml else (None, {}))

    n_total = 0
    n_match = 0
    n_fp = 0  # CV dit filled, truth dit empty
    n_fn = 0  # CV dit empty, truth dit filled
    no_mires_copies = []
    mismatches = []  # liste de (copy, q, a, char, ratio, truth_bool, cv_bool, threshold_used)
    per_copy_stats = {}

    print(f"Seuil adaptatif par question, shrink={shrink}")
    print(f"Copies à évaluer: {len(truth)} (avec corrections manuelles)")

    for copy_id, gt_cells in sorted(truth.items()):
        if copy_id not in copy_to_file:
            continue
        batch, page = copy_to_file[copy_id]
        img_path = PAGES_DIR / batch / f"page_{page:03d}.jpg"
        if not img_path.exists():
            continue

        result = grade_image_raw(img_path, layout, canon_mires, canon_w, canon_h, lay=lay,
                                 shrink=shrink)
        if result is None:
            no_mires_copies.append((copy_id, batch, page))
            continue
        ratios, warped, offsets, layout_by_qa = result

        # ratios groupés par question + seuil adaptatif par question
        ratios_by_q = defaultdict(list)
        for (q, a), (ch, r) in ratios.items():
            ratios_by_q[q].append(r)
        threshold_per_q = {q: adaptive_threshold(rs) for q, rs in ratios_by_q.items()}

        # Copy baseline pour les features ML
        copy_baseline = compute_copy_baseline(
            [r for (q, _a), (_ch, r) in ratios.items() if q in qcm_set]
        )

        n_local_total = 0
        n_local_match = 0
        n_local_fp = 0
        n_local_fn = 0
        for (q, a), truth_ticked in gt_cells.items():
            if (q, a) not in ratios:
                continue
            char, ratio = ratios[(q, a)]
            t_q = threshold_per_q.get(q, 0.5)  # seuil adaptatif par question
            if clf_bundle is not None:
                # ML sur TOUTES les cases (plus de bande)
                b = layout_by_qa[(q, a)]
                feats = extract_features(
                    warped, b, ratios_by_q[q], copy_baseline,
                    offset=offsets.get(q, (0, 0)),
                    ref=ref, ref_corners=ref_frames.get((q, a)),
                )
                x = np.array([[feats[k] for k in clf_bundle["feature_cols"]]],
                             dtype=np.float64)
                cv_ticked = bool(clf_bundle["clf"].predict(x)[0] == 1)
            else:
                cv_ticked = ratio > t_q  # repli seuil adaptatif
            n_total += 1
            n_local_total += 1
            if cv_ticked == truth_ticked:
                n_match += 1
                n_local_match += 1
            else:
                if cv_ticked and not truth_ticked:
                    n_fp += 1
                    n_local_fp += 1
                else:
                    n_fn += 1
                    n_local_fn += 1
                mismatches.append((copy_id, batch, page, q, a, char, ratio, truth_ticked, cv_ticked, t_q))
        per_copy_stats[copy_id] = (n_local_total, n_local_match, n_local_fp, n_local_fn)

    print()
    print(f"  Cells évaluées:   {n_total}")
    print(f"  Match:            {n_match} ({100*n_match/max(1,n_total):.2f}%)")
    print(f"  False Positives:  {n_fp}  (CV=filled, user=empty)")
    print(f"  False Negatives:  {n_fn}  (CV=empty, user=filled)")
    print(f"  NO_MIRES copies:  {len(no_mires_copies)}")
    if no_mires_copies:
        for c, b, p in no_mires_copies[:5]:
            print(f"    - copy {c}: {b}/page_{p:03d}")

    if by_copy:
        print()
        print("Par copie (worst first):")
        sorted_copies = sorted(per_copy_stats.items(), key=lambda kv: kv[1][1] / max(1, kv[1][0]))
        for cid, (tot, m, fp, fn) in sorted_copies[:15]:
            acc = 100 * m / max(1, tot)
            print(f"  copy {cid}: {m}/{tot} ({acc:.1f}%)  FP={fp} FN={fn}")

    if details:
        # Distribution des ratios CV sur les mismatches
        print()
        print("Mismatches par tranche de ratio CV:")
        bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.0)]
        for lo, hi in bins:
            in_bin = [m for m in mismatches if lo <= m[6] < hi]
            n_fp_b = sum(1 for m in in_bin if m[8])
            n_fn_b = sum(1 for m in in_bin if not m[8])
            print(f"  ratio ∈ [{lo:.2f}, {hi:.2f}):  {len(in_bin):4d}   FP={n_fp_b}  FN={n_fn_b}")

        # CSV détaillé
        RESULTS_DIR.mkdir(exist_ok=True)
        out_csv = RESULTS_DIR / f"cv_benchmark_s{shrink:.2f}.csv"
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["copy", "batch", "page", "question", "answer", "char", "cv_ratio", "truth", "cv", "threshold_q"])
            for c, b, p, q, a, ch, r, t, cv, t_q in mismatches:
                w.writerow([c, b, p, q, a, ch, f"{r:.3f}", int(t), int(cv), f"{t_q:.3f}"])
        print(f"\n→ Détail mismatches: {out_csv} ({len(mismatches)} lignes)")

    return n_match / max(1, n_total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shrink", type=float, default=0.18)
    ap.add_argument("--by-copy", action="store_true")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--no-ml", action="store_true", help="désactiver le classifieur GBM")
    args = ap.parse_args()

    run_benchmark(args.shrink,
                  by_copy=args.by_copy,
                  details=args.details,
                  use_ml=not args.no_ml)


if __name__ == "__main__":
    main()
