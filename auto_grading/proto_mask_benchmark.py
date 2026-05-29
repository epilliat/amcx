"""proto_mask_benchmark.py — PROTOTYPE exploratoire (ne modifie rien).

Compare la mesure de noirceur actuelle (`cv_grade.box_fill_ratio`, shrink=0.18)
à une mesure **masquée**, calée **par case** sur le cadre carré :
  - référence = rendu 300 dpi de la feuille de réponses du PDF du sujet ;
  - on détecte le CADRE de la case (minAreaRect du contour) → 4 coins → similarité
    (translation + rotation + échelle) réf→scan estimée sur ces 4 coins ;
  - on masque cadre + lettre imprimés (référence calée, dilatée) et on moyenne la
    noirceur du scan dans l'intérieur érodé de la case.

Benchmark A/B sur les cases ground-truth AMC + diagnostic des échecs de détection
du cadre (sont-ils sur des cases vides/légères, qui comptent, ou pleines ?).

    python proto_mask_benchmark.py
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import cv2
import fitz
import numpy as np

import config
import layout_store
from cv_grade import (box_fill_ratio, compute_per_question_offsets,
                      detect_mires, warp_to_canonical)

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
MARGIN = 18
_K3 = np.ones((3, 3), np.uint8)


def render_reference(lay) -> np.ndarray:
    """Rendu 300 dpi de la feuille de réponses du PDF du sujet (gris, canonique)."""
    for cand in (config.amc_dir() / "DOC-sujet.pdf", ROOT / "sujet" / "DOC-sujet.pdf"):
        if cand.exists():
            pdf = cand
            break
    else:
        raise FileNotFoundError("DOC-sujet.pdf introuvable (amc_dir ou sujet/)")
    doc = fitz.open(str(pdf))
    pix = doc[lay.answer_sheet_page - 1].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    gray = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY) if pix.n >= 3 else a[:, :, 0].copy()
    doc.close()
    return gray


def _order_corners(pts) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float32)
    c = pts.mean(axis=0)
    return pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]


def ref_corners_global(box) -> np.ndarray:
    cx, cy = (box.xmin + box.xmax) / 2.0, (box.ymin + box.ymax) / 2.0
    bw, bh = box.xmax - box.xmin, box.ymax - box.ymin
    return _order_corners(cv2.boxPoints(((cx, cy), (bw, bh), 0.0)))


def detect_frame(warped: np.ndarray, box, margin: int = MARGIN):
    """Détecte le cadre carré de la case → (4 coins globaux, raison_échec).

    raison_échec ∈ {'', 'oob', 'no_contour', 'taille', 'forme'} ; coins=None si échec."""
    bx1, by1 = int(box.xmin), int(box.ymin)
    bw, bh = int(box.xmax) - bx1, int(box.ymax) - by1
    x0, y0 = bx1 - margin, by1 - margin
    w, h = bw + 2 * margin, bh + 2 * margin
    if x0 < 0 or y0 < 0 or x0 + w > warped.shape[1] or y0 + h > warped.shape[0]:
        return None, "oob"
    crop = warped[y0:y0 + h, x0:x0 + w].astype(np.uint8)
    _, binv = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(binv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    target = (bw + bh) / 2.0
    cx0, cy0 = w / 2.0, h / 2.0
    best, best_d, seen = None, 1e18, ""
    for c in cnts:
        (cx, cy), (rw, rh), _a = cv2.minAreaRect(c)
        if rw < 5 or rh < 5:
            continue
        if not (0.80 * target <= (rw + rh) / 2.0 <= 1.20 * target):
            seen = seen or "taille"
            continue
        if abs(rw - rh) > 0.28 * target:
            seen = seen or "forme"
            continue
        d = (cx - cx0) ** 2 + (cy - cy0) ** 2
        if d < best_d:
            best_d, best = d, ((cx, cy), (rw, rh), _a)
    if best is None:
        return None, (seen or "no_contour")
    return _order_corners(cv2.boxPoints(best) + np.array([x0, y0], np.float32)), ""


def masked_ratio(warped: np.ndarray, ref: np.ndarray, box,
                 ref_corners, scan_corners, margin: int = MARGIN):
    """Noirceur du scan dans l'intérieur érodé de la case, hors masque imprimé.

    `ref_corners` et `scan_corners` viennent du MÊME détecteur (bord extérieur du
    cadre) — la similarité réf→scan est ainsi sans biais d'échelle."""
    bx1, by1 = int(box.xmin), int(box.ymin)
    bw, bh = int(box.xmax) - bx1, int(box.ymax) - by1
    x0, y0 = bx1 - margin, by1 - margin
    w, h = bw + 2 * margin, bh + 2 * margin
    if (x0 < 0 or y0 < 0 or x0 + w > min(warped.shape[1], ref.shape[1])
            or y0 + h > min(warped.shape[0], ref.shape[0])):
        return None
    M, _ = cv2.estimateAffinePartial2D(ref_corners, scan_corners)
    if M is None:
        return None
    scan = warped[y0:y0 + h, x0:x0 + w].astype(np.float32)
    Mq3 = np.vstack([M, [0, 0, 1]]).astype(np.float64)
    T = np.array([[1, 0, x0], [0, 1, y0], [0, 0, 1]], np.float64)
    M_d2s = (np.linalg.inv(Mq3) @ T)[:2].astype(np.float32)
    ref_al = cv2.warpAffine(ref, M_d2s, (w, h),
                            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=255)
    # masque large : couvre le halo flou du cadre/lettre SCANNÉS (plus épais que le
    # rendu PDF net) — seuil haut + dilatation généreuse.
    mask = cv2.dilate((ref_al < 200).astype(np.uint8), _K3, iterations=4)
    poly = (scan_corners - np.array([x0, y0], np.float32)).astype(np.int32)
    inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inside, poly, 1)
    inside = cv2.erode(inside, _K3, iterations=5)
    bg = (inside == 1) & (mask == 0)
    if int(bg.sum()) < 25:
        return None
    # FRACTION de points sombres PARMI les seuls points hors masque.
    # Aucune soustraction d'image : la référence n'a servi qu'à EXCLURE
    # (cadre + lettre). Seuil RELATIF au niveau du papier (p85 du crop) →
    # robuste à l'exposition du scan CamScanner (papier « blanc » ≠ 255).
    paper = max(float(np.percentile(scan, 85)), 1.0)
    return float((scan[bg] < 0.60 * paper).mean())


def oracle_acc(vals: list, truths: list) -> tuple[int, int]:
    """Meilleure accuracy atteignable par un seuil unique (pred = val > thr)."""
    n = len(vals)
    if n == 0:
        return 0, 0
    order = np.argsort(vals, kind="mergesort")
    t = np.asarray(truths, dtype=np.int64)[order]
    ntrue = int(t.sum())
    pref = np.concatenate([[0], np.cumsum(t)])
    best = max((k - pref[k]) + (ntrue - pref[k]) for k in range(n + 1))
    return best, n


def oracle_threshold(vals: list, truths: list) -> float:
    """Seuil unique qui maximise l'accuracy sur (vals, truths)."""
    n = len(vals)
    if n == 0:
        return 0.5
    order = np.argsort(vals, kind="mergesort")
    sv = np.asarray(vals, dtype=np.float64)[order]
    t = np.asarray(truths, dtype=np.int64)[order]
    ntrue = int(t.sum())
    pref = np.concatenate([[0], np.cumsum(t)])
    best, best_k = -1, 0
    for k in range(n + 1):
        correct = (k - pref[k]) + (ntrue - pref[k])
        if correct > best:
            best, best_k = correct, k
    lo = sv[best_k - 1] if best_k > 0 else sv[0] - 1e-6
    hi = sv[best_k] if best_k < n else sv[-1] + 1e-6
    return float((lo + hi) / 2.0)


def main():
    lay = layout_store.get_layout()
    ref = render_reference(lay)
    layout = lay.sheet_boxes()
    canon_mires = np.asarray(lay.mires, np.float32)
    cw, ch = int(round(lay.page_w)), int(round(lay.page_h))
    # cadre de chaque case détecté UNE fois dans la référence (même détecteur)
    ref_frames = {}
    for b in layout:
        rc, _ = detect_frame(ref, b)
        ref_frames[(b.question, b.answer)] = rc
    n_ref_ok = sum(v is not None for v in ref_frames.values())
    print(f"Référence : {ref.shape} | feuille page {lay.answer_sheet_page} "
          f"| {len(layout)} cases | cadres réf détectés : {n_ref_ok}/{len(layout)}")

    cap = config.amc_data_dir() / "capture.sqlite"
    con = sqlite3.connect("file:%s?mode=ro" % cap, uri=True)
    truth = defaultdict(dict)
    for copy, q, a, manual in con.execute(
            "SELECT copy,id_a,id_b,manual FROM capture_zone "
            "WHERE type=4 AND manual IN (0,1)"):
        truth[copy][(q, a)] = (manual == 1)
    c2f = {}
    for copy, src in con.execute(
            "SELECT copy,src FROM capture_page WHERE page=?", (lay.answer_sheet_page,)):
        m = re.search(r"(\d+)_.*pdf-page-(\d+)-", src)
        if m:
            c2f[copy] = (f"batch{m.group(1)}", int(m.group(2)))
    con.close()

    rows = []                 # (q, char, truth, old, new, detected)
    fail_reason = defaultdict(int)
    copies = sorted(c for c in truth if c in c2f)
    for i, copy in enumerate(copies, 1):
        batch, page = c2f[copy]
        ip = PAGES / batch / f"page_{page:03d}.jpg"
        g = cv2.imread(str(ip), cv2.IMREAD_GRAYSCALE) if ip.exists() else None
        if g is None:
            continue
        mires = detect_mires(g)
        if mires is None:
            continue
        warped = warp_to_canonical(g, mires, canon_mires, cw, ch)
        offs = compute_per_question_offsets(warped, layout)
        for b in layout:
            t = truth[copy].get((b.question, b.answer))
            if t is None:
                continue
            old = box_fill_ratio(warped, b, shrink=0.18, offset=offs.get(b.question, (0, 0)))
            scan_c, why = detect_frame(warped, b)
            ref_c = ref_frames.get((b.question, b.answer))
            if scan_c is None or ref_c is None:
                fail_reason[why or "ref"] += 1
                rows.append((b.question, b.char, t, old, None, False, copy))
                continue
            new = masked_ratio(warped, ref, b, ref_c, scan_c)
            rows.append((b.question, b.char, t, old, new, new is not None, copy))
        if i % 12 == 0 or i == len(copies):
            print(f"  [{i}/{len(copies)}] {len(rows)} cases", flush=True)

    n = len(rows)
    det = [r for r in rows if r[5]]
    nodet = [r for r in rows if not r[5]]
    print(f"\n=== {n} cases | cadre détecté : {len(det)} "
          f"({100*len(det)//n} %) | échecs : {len(nodet)} ===")
    print(f"  raisons d'échec : {dict(fail_reason)}")

    # diagnostic : les échecs sont-ils des cases pleines (faciles) ou vides/légères ?
    f_full = [r for r in nodet if r[2]]
    f_empty = [r for r in nodet if not r[2]]
    print(f"  échecs sur cases PLEINES : {len(f_full)}  "
          f"(old ratio moyen {np.mean([r[3] for r in f_full]) if f_full else 0:.2f})")
    print(f"  échecs sur cases VIDES   : {len(f_empty)}  "
          f"(old ratio moyen {np.mean([r[3] for r in f_empty]) if f_empty else 0:.2f})")

    # A/B sur le sous-ensemble où le cadre est détecté (comparaison juste)
    print(f"\n  -- seuil oracle / question, sur les {len(det)} cases cadre-détecté --")
    byq_old = defaultdict(list)
    byq_new = defaultdict(list)
    for q, ch, t, old, new, _d, _c in det:
        byq_old[q].append((old, t))
        byq_new[q].append((new, t))
    for name, byq in (("ANCIENNE (shrink)", byq_old), ("MASQUÉE cadre", byq_new)):
        ok = tot = 0
        for grp in byq.values():
            a, tt = oracle_acc([v for v, _ in grp], [t for _, t in grp])
            ok += a
            tot += tt
        print(f"    {name:22s} : {ok}/{tot} = {100*ok/tot:.2f}%  ({tot-ok} err)")

    print("  -- cases VIDES cadre-détecté : biais de fond --")
    for name, idx in (("ANCIENNE (shrink)", 3), ("MASQUÉE cadre", 4)):
        bych = defaultdict(list)
        for r in det:
            if not r[2]:
                bych[r[1]].append(r[idx])
        means = [float(np.mean(v)) for v in bych.values() if v]
        allv = [x for v in bych.values() for x in v]
        print(f"    {name:22s} : moy={np.mean(allv):.3f}  σ_lettres={np.std(means):.3f}")

    # -- FLAGGING : les erreurs sont-elles près du seuil (→ signalables) ? --
    print("\n  -- FLAGGING : erreurs vs cases signalées (bande autour du seuil) --")
    f_full = [r for r in nodet if r[2]]
    f_empty = [r for r in nodet if not r[2]]
    print(f"    {len(nodet)} cases SANS cadre → auto-signalées : "
          f"{len(f_empty)} vides (à revoir) + {len(f_full)} pleines (trivialement cochées)")
    for name, idx in (("ANCIENNE (shrink)", 3), ("MASQUÉE cadre", 4)):
        err_d, ok_d = [], []
        for q in byq_old:
            grp = [(r[idx], r[2]) for r in det if r[0] == q]
            thr = oracle_threshold([v for v, _ in grp], [t for _, t in grp])
            for v, t in grp:
                (err_d if (v > thr) != t else ok_d).append(abs(v - thr))
        nerr = len(err_d)
        print(f"    {name} — {nerr} erreurs sur {len(det)} cases :")
        for delta in (0.04, 0.06, 0.08, 0.10):
            caught = sum(d < delta for d in err_d)
            flagged = caught + sum(d < delta for d in ok_d)
            print(f"       bande ±{delta:.2f} : {caught}/{nerr} erreurs signalées "
                  f"({100*caught//max(1,nerr)} %)  |  {flagged} cases signalées "
                  f"({100*flagged/len(det):.1f} %)")

    # -- nature des erreurs : faux positifs vs faux négatifs --
    print("\n  -- NATURE des erreurs (FP = lue cochée mais vide ; "
          "FN = lue vide mais cochée) --")
    for name, idx in (("ANCIENNE (shrink)", 3), ("MASQUÉE cadre", 4)):
        fp, fn = [], []
        for q in byq_old:
            grp = [(r[idx], r[2], r[1]) for r in det if r[0] == q]
            thr = oracle_threshold([v for v, _, _ in grp], [t for _, t, _ in grp])
            for v, t, ch in grp:
                pred = v > thr
                if pred and not t:
                    fp.append((q, ch, v))
                elif not pred and t:
                    fn.append((q, ch, v))
        print(f"    {name:22s} : {len(fp)} faux positifs | {len(fn)} faux négatifs")
        for tag, lst in (("FP", fp), ("FN", fn)):
            if lst:
                ex = ", ".join(f"Q{q}{ch}" for q, ch, _ in lst[:12])
                print(f"        {tag} : {ex}{' …' if len(lst) > 12 else ''}")
    print(f"    (cases sans cadre : {len(f_empty)} vides, {len(f_full)} pleines "
          f"— auto-signalées, non comptées ci-dessus)")

    # -- STRATÉGIE DE FLAGGING : contrôles indépendants, on flague l'union --
    print("\n  -- FLAGGING par contrôles convergents (méthode masquée) --")
    from sujet_store import effective_spec
    thr_new = {q: oracle_threshold([v for v, _ in g], [t for _, t in g])
               for q, g in byq_new.items()}
    thr_old = {q: oracle_threshold([v for v, _ in g], [t for _, t in g])
               for q, g in byq_old.items()}
    qtype = {}
    for q in byq_new:
        try:
            qtype[q] = effective_spec(q)["type"] if q <= 31 else "single"
        except Exception:
            qtype[q] = "single"   # colonnes du code : exactement 1 attendu

    cells = defaultdict(list)     # (copy,q) -> [(char,truth,old,new)]
    for q, ch, t, old, new, d, copy in rows:
        if d:
            cells[(copy, q)].append((ch, t, old, new))

    errors, f_struct, f_disagree, f_band, f_amc = set(), set(), set(), set(), set()
    for (copy, q), cs in cells.items():
        n_sel = sum(1 for ch, t, old, new in cs if new > thr_new[q])
        bad_struct = qtype.get(q, "mult") == "single" and n_sel != 1
        for ch, t, old, new in cs:
            key = (copy, q, ch)
            dec_new = new > thr_new[q]
            dec_old = old > thr_old[q]
            if dec_new != t:
                errors.add(key)
            if bad_struct:
                f_struct.add(key)              # cohérence QCM (single ⇒ 1 coche)
            if dec_old != dec_new:
                f_disagree.add(key)            # désaccord ancienne vs masquée
            if abs(new - thr_new[q]) < 0.08:
                f_band.add(key)                # proximité du seuil

    ne = len(errors)
    union = f_struct | f_disagree | f_band
    tot = len(cells) and sum(len(v) for v in cells.values())
    for name, fset in [("cohérence QCM (single≠1)", f_struct),
                       ("désaccord ancienne/masquée", f_disagree),
                       ("proximité seuil ±0.08", f_band),
                       ("UNION des trois", union)]:
        print(f"    {name:28s} : {len(errors & fset):2d}/{ne} erreurs "
              f"| {len(fset):4d} cases signalées ({100*len(fset)/tot:.1f} %)")


if __name__ == "__main__":
    main()
