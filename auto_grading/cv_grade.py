"""Pipeline OpenCV pour lire une feuille de réponses AMC :
1. Détecte les 4 mires (cercles noirs en coins)
2. Calcule l'homographie vers le layout canonique (depuis layout.sqlite)
3. Cropple chaque case d'après les positions canoniques
4. Calcule le fill_ratio par case → filled / empty
5. Émet un JSON conforme à raw_responses/

Usage:
  python cv_grade.py <image.jpg> [--debug]
  python cv_grade.py --all              # traite toutes les pages dans pages/
  python cv_grade.py --failed-only      # traite seulement les 38 failed + 4 vérifs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

import config
import layout_store
import masked_detect
from layout_store import Box as BoxLayout
from masked_detect import MASKED_FEATURE_COLS


# Un worker coûte ≈ 0,9 s à démarrer (imports sklearn/pandas/pyarrow via le
# pickle du modèle) pour ≈ 0,16 s de grade par page : en dessous de quelques
# pages par worker, ajouter des workers ralentit. Mesuré : sur 16 pages, 4, 8
# et 22 workers font le même temps ; sur 50 pages, 8 = 22.
PAGES_PER_WORKER = 6


def _resolve_workers(workers: int | None, n_jobs: int) -> int:
    """`workers=None` ou 0 → auto : cpu_count, mais jamais plus d'un worker
    pour `PAGES_PER_WORKER` pages. Toujours ≥ 1 et ≤ n_jobs. Un `workers`
    explicite est respecté (cap à n_jobs). Un poste mono-cœur tombe sur la
    boucle série (pas d'overhead de Pool)."""
    if not workers:
        wanted = -(-max(1, n_jobs) // PAGES_PER_WORKER)      # ceil
        workers = min(os.cpu_count() or 1, wanted)
    return max(1, min(workers, max(1, n_jobs)))


_THREAD_ENV_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


class _single_thread_env:
    """Pose les variables de threads BLAS/OpenMP à 1 dans le PARENT le temps
    de créer le pool, puis les restaure.

    En mode `spawn`, l'enfant importe le module de l'initializer (donc numpy
    et ses deux OpenBLAS) AVANT d'exécuter l'initializer : les poser là est
    trop tard, le pool de threads existe déjà. Mesuré : 44 threads par worker
    avec l'ancien `_worker_init`, 2 avec les variables posées ici. La
    restauration compte : le serveur Flask ne doit pas garder un BLAS
    mono-thread pour le reste de sa vie.
    """

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _THREAD_ENV_VARS}
        for k in _THREAD_ENV_VARS:
            os.environ[k] = "1"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _worker_init():
    """Init worker process : 1 thread BLAS/OpenMP/OpenCV par worker.

    Ceinture et bretelles avec `_single_thread_env` : `threadpoolctl` agit sur
    les bibliothèques DÉJÀ chargées (c'est une dépendance de sklearn, donc
    toujours là), et les variables d'environnement couvrent celles qui seront
    chargées ensuite (libgomp de sklearn, au `joblib.load`).
    """
    for k in _THREAD_ENV_VARS:
        os.environ.setdefault(k, "1")
    try:
        from threadpoolctl import threadpool_limits
        threadpool_limits(1)
    except Exception:
        pass
    try:
        import cv2 as _cv2
        _cv2.setNumThreads(1)
    except Exception:
        pass

# Données projet (calculées à l'import — restart sur switch).
from config import project_root as _project_root  # noqa: E402

_INSTALL_DIR = Path(__file__).resolve().parent  # installation : pour les modèles ML
ROOT = _project_root()                          # projet actif : pour les données
PAGES_DIR = ROOT / "pages"
RAW_DIR = ROOT / "raw_responses"
MODELS_DIR = _INSTALL_DIR / "models"             # modèles partagés entre projets

# Classifieur GBM optionnel — entraîné par train_classifier.py.
# Chargé une seule fois au premier appel à load_cell_classifier().
# Si absent ou KO, on retombe sur le seuil de noirceur absolu (config.cell_threshold).
_CLF_CACHE: dict | None = None
_CLF_TRIED = False


def load_cell_classifier() -> dict | None:
    """Charge models/cell_clf_full.pkl si dispo. Retourne {'clf', 'feature_cols'} ou None."""
    global _CLF_CACHE, _CLF_TRIED
    if _CLF_TRIED:
        return _CLF_CACHE
    _CLF_TRIED = True
    try:
        import joblib
        path = MODELS_DIR / "cell_clf_full.pkl"
        if not path.exists():
            path = MODELS_DIR / "cell_clf.pkl"
        if path.exists():
            _CLF_CACHE = joblib.load(path)
    except Exception:
        _CLF_CACHE = None
    return _CLF_CACHE

# `BoxLayout` est désormais `layout_store.Box` (alias importé en tête de fichier) ;
# la page canonique, les mires et les dimensions viennent de `layout_store.get_layout()`.


def load_layout() -> list:
    """Cases de la feuille de réponses (role=1), depuis layout_store."""
    return layout_store.get_layout().sheet_boxes()


def _is_id_column(boxes) -> bool:
    """Vrai si ce groupe de cases est une colonne du code étudiant (chiffres 0-9)
    plutôt qu'une question QCM (lettres de réponse)."""
    chars = [b.char for b in boxes if b.char]
    return bool(chars) and all(c.isdigit() for c in chars)


# Repli si la zone __n est absente du calage (coords canoniques observées EXAM_2026).
NAME_FIELD_FALLBACK = (1131.4, 1878.3, 1109.2, 1395.6)  # xmin, xmax, ymin, ymax


def load_name_field() -> tuple[float, float, float, float]:
    """Rectangle canonique du champ nom manuscrit (zone `__n` de la feuille)."""
    try:
        zone = layout_store.get_layout().name_zone
        if zone:
            return tuple(float(v) for v in zone)
    except Exception:
        pass
    return NAME_FIELD_FALLBACK


# --- mires ------------------------------------------------------------------
# Une mire est un DISQUE. Le filtre historique (aire 600–3000, circularité
# 4πA/P² ≥ 0.65) laissait passer tout carré noir : une case cochée (aire ≈ 2000,
# circularité 0.79), un bit noir du code imprimé (1430, 0.79), deux bits
# contigus (2920, 0.69). Mesuré sur EXAM_2026 : 133 à 201 candidats par page, et
# seule la règle « le plus proche du coin » choisissait — le premier bit du code
# est à 736 px du coin haut-gauche, sous l'ancien seuil de 744. Une mire absente
# donnait alors une homographie fausse avec un `method=cv_full` rassurant.
#
# D'où : tests de forme disque/carré, recherche dans des FENÊTRES autour des
# positions attendues quand le calage est connu, et validation du quadrilatère
# avant de rendre quoi que ce soit. Un échec est un `None` honnête (→
# `cv_no_mires`), pas un recalage de travers.
#
# Seuils MESURÉS sur les 692 vraies mires des 173 scans d'EXAM_2026 (scans
# « pas oufs » : disques déformés, JPEG) contre 599 autres candidats tombant
# dans les fenêtres (cases cochées, bits du code, taches) :
#   rondeur = aire / cercle englobant : vraies 0.73–0.96, autres p99 0.68 ;
#   remplissage du rectangle minimal  : vraies 0.76–0.82, autres méd. 0.88
#                                       (un carré → 1.0) ;
#   circularité 4πA/P²                : vraies 0.80–0.91, autres p99 0.79.
# Chaque seuil garde 100 % des vraies mires avec une marge ; combinés, ils
# laissent passer 1 à 2 autres candidats sur 599, que la distance à la
# position attendue et la validation du quadrilatère écartent.
MIRE_MIN_ROUNDNESS = 0.70     # aire / aire du cercle englobant
MIRE_MAX_RECT_FILL = 0.86     # aire / aire du rectangle minimal (carré ≈ 1)
MIRE_MIN_CIRCULARITY = 0.75   # 4πA/P²
MIRE_AREA_RANGE = (0.5, 1.6)  # × l'aire attendue (calage), sinon [600, 3000] px²
MIRE_SEARCH_FRAC = 0.12       # demi-côté de la fenêtre, × min(w, h) (≈ 300 px)
# Géométrie du quadrilatère, mesurée sur les mêmes 173 pages : côtés opposés
# et diagonales à moins de 4.6 % l'un de l'autre ; rapport largeur/hauteur
# jusqu'à 8.5 % du canonique (chargeur qui étire la page). Une mire prise sur
# un bit du code (≥ 300 px de décalage) dévie de plus de 15 % — rejetée.
MIRE_QUAD_TOL = 0.08          # côtés opposés, diagonales
MIRE_ASPECT_TOL = 0.15        # rapport L/H vs canonique


def _mire_candidates(bw: np.ndarray, roi, area_range, edge_margin, shape) -> list:
    """Disques candidats dans une ROI de l'image binaire → [(cx, cy)] en
    coordonnées globales. `roi` = (x1, y1, x2, y2)."""
    h, w = shape
    x1, y1, x2, y2 = roi
    sub = bw[y1:y2, x1:x2]
    if sub.size == 0:
        return []
    contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
                                   offset=(x1, y1))
    lo, hi = area_range
    out = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (lo <= area <= hi):
            continue
        (_, _), r = cv2.minEnclosingCircle(cnt)
        if r <= 0 or area / (np.pi * r * r) < MIRE_MIN_ROUNDNESS:
            continue
        (_, _), (rw, rh), _ = cv2.minAreaRect(cnt)
        if rw * rh <= 0 or area / (rw * rh) > MIRE_MAX_RECT_FILL:
            continue
        perim = cv2.arcLength(cnt, True)
        if perim <= 0 or 4 * np.pi * area / (perim * perim) < MIRE_MIN_CIRCULARITY:
            continue
        M = cv2.moments(cnt)
        if M["m00"] == 0:
            continue
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        if cx < edge_margin or cx > w - edge_margin or cy < edge_margin or cy > h - edge_margin:
            continue
        out.append((cx, cy))
    return out


def _quad_ok(pts: np.ndarray, canon: np.ndarray | None, tol: float = MIRE_QUAD_TOL) -> bool:
    """Les 4 points [TL, TR, BR, BL] forment-ils le quadrilatère attendu ?

    Sans calage : convexe, orienté comme la page, côtés opposés égaux,
    diagonales égales (à `tol` près). Avec calage : en plus, le rapport
    largeur/hauteur est celui des mires canoniques — indépendant de l'échelle
    du scan."""
    p = np.asarray(pts, dtype=np.float64)
    tl, tr, br, bl = p
    top, bottom = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    left, right = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    d1, d2 = np.linalg.norm(br - tl), np.linalg.norm(bl - tr)
    if min(top, bottom, left, right) <= 0:
        return False
    # orientation : TR à droite de TL, BL sous TL, et convexité (produits
    # vectoriels de même signe en tournant TL → TR → BR → BL)
    if not (tr[0] > tl[0] and br[0] > bl[0] and bl[1] > tl[1] and br[1] > tr[1]):
        return False
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
    signs = [cross(tl, tr, br), cross(tr, br, bl), cross(br, bl, tl), cross(bl, tl, tr)]
    if not (all(s > 0 for s in signs) or all(s < 0 for s in signs)):
        return False
    if abs(top / bottom - 1) > tol or abs(left / right - 1) > tol or abs(d1 / d2 - 1) > tol:
        return False
    if canon is not None:
        c = np.asarray(canon, dtype=np.float64)
        c_top = np.linalg.norm(c[1] - c[0])
        c_left = np.linalg.norm(c[3] - c[0])
        if c_top > 0 and c_left > 0:
            if abs((top / left) / (c_top / c_left) - 1) > MIRE_ASPECT_TOL:
                return False
    return True


def _pick_nearest(cands: list, targets: np.ndarray, max_dist: float) -> np.ndarray | None:
    """Pour chaque cible, le candidat le plus proche (à moins de `max_dist`),
    chaque candidat servant au plus une fois."""
    if len(cands) < 4:
        return None
    pts = np.array(cands, dtype=np.float64)
    used = set()
    sel = []
    for t in targets:
        d = np.linalg.norm(pts - t, axis=1)
        for idx in np.argsort(d):
            idx = int(idx)
            if idx in used:
                continue
            if d[idx] > max_dist:
                return None
            used.add(idx)
            sel.append(pts[idx])
            break
        else:
            return None
    return np.array(sel, dtype=np.float32)


def detect_mires(img_gray: np.ndarray, edge_margin: int = 50,
                 layout: "layout_store.Layout | None" = None) -> np.ndarray | None:
    """Détecte les 4 mires (disques noirs ≈ 42 px de diamètre) aux 4 coins.

    `layout` (optionnel) : le calage donne la position canonique des mires et
    leur diamètre → la recherche se limite à une fenêtre autour de chaque
    position attendue (rapide, et rien du code imprimé n'y tombe), l'aire
    attendue borne les candidats, et le quadrilatère trouvé est comparé au
    canonique. Sans calage, ou si la recherche par fenêtres échoue, on cherche
    sur toute la page — mais toujours avec le test de rondeur et la validation
    géométrique.

    `edge_margin` : distance minimale d'un bord pour qu'un candidat compte
    (artefacts de marge). Retourne un array (4, 2) [TL, TR, BR, BL] ou None.
    """
    h, w = img_gray.shape
    # Un seul seuil d'Otsu pour toute la page : la binarisation est identique
    # à celle d'avant, fenêtre ou non — les centroïdes trouvés le sont aussi.
    _, bw = cv2.threshold(img_gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    canon = None
    area_range = (600.0, 3000.0)
    if layout is not None:
        try:
            canon = np.asarray(layout.mires, dtype=np.float64)
            if canon.shape != (4, 2):
                canon = None
            md = float(layout.mark_diameter or 0.0)
            if md > 0:
                expected = np.pi * (md / 2.0) ** 2
                area_range = (MIRE_AREA_RANGE[0] * expected, MIRE_AREA_RANGE[1] * expected)
        except Exception:
            canon = None

    if canon is not None and layout.page_w and layout.page_h:
        # 1. Fenêtres autour des positions attendues (mises à l'échelle du scan).
        sx, sy = w / float(layout.page_w), h / float(layout.page_h)
        targets = canon * np.array([sx, sy])
        half = int(MIRE_SEARCH_FRAC * min(w, h))
        cands = []
        for tx, ty in targets:
            roi = (max(0, int(tx) - half), max(0, int(ty) - half),
                   min(w, int(tx) + half), min(h, int(ty) + half))
            cands.extend(_mire_candidates(bw, roi, area_range, edge_margin, (h, w)))
        sel = _pick_nearest(cands, targets, max_dist=half * 1.5)
        if sel is not None and _quad_ok(sel, canon):
            return sel

    # 2. Page entière (pas de calage, scan à une autre échelle ou mal cadré).
    cands = _mire_candidates(bw, (0, 0, w, h), area_range, edge_margin, (h, w))
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    sel = _pick_nearest(cands, corners, max_dist=0.30 * min(w, h))
    if sel is None or not _quad_ok(sel, canon):
        return None
    return sel


def refine_box_offset(warped: np.ndarray, box: "BoxLayout",
                      search: int = 28) -> tuple[int, int]:
    """Cherche le rectangle imprimé près de la position canonique de la case.

    Retourne (dx, dy) à appliquer aux coordonnées canoniques pour aligner sur
    le rectangle détecté. Si rien trouvé, retourne (0, 0).

    Méthode : on cherche les CONTOURS de rectangles de la bonne taille dans
    une fenêtre élargie. On prend celui dont le centre est le plus proche du
    centre canonique. Robuste aux cases vides ET aux cases noircies (on cherche
    le contour englobant).
    """
    x1 = max(0, int(box.xmin) - search)
    y1 = max(0, int(box.ymin) - search)
    x2 = min(warped.shape[1], int(box.xmax) + search)
    y2 = min(warped.shape[0], int(box.ymax) + search)
    win = warped[y1:y2, x1:x2]
    if win.size == 0:
        return 0, 0
    _, bw = cv2.threshold(win, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    target_w = box.xmax - box.xmin
    target_h = box.ymax - box.ymin
    target_cx = (box.xmin + box.xmax) / 2 - x1
    target_cy = (box.ymin + box.ymax) / 2 - y1

    best_d = float("inf")
    best_offset = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # tolérance sur la taille (±30 %)
        if not (0.7 * target_w <= w <= 1.4 * target_w):
            continue
        if not (0.7 * target_h <= h <= 1.4 * target_h):
            continue
        cx_c = x + w / 2
        cy_c = y + h / 2
        d = ((cx_c - target_cx) ** 2 + (cy_c - target_cy) ** 2) ** 0.5
        if d > search * 1.2:  # trop loin pour être notre case
            continue
        if d < best_d:
            best_d = d
            best_offset = (cx_c - target_cx, cy_c - target_cy)

    if best_offset is None:
        return 0, 0
    return int(round(best_offset[0])), int(round(best_offset[1]))


def warp_to_canonical(img: np.ndarray, mires: np.ndarray, canon_mires: np.ndarray,
                      canon_w: int, canon_h: int) -> np.ndarray:
    """Warp l'image pour que les mires correspondent aux positions canoniques."""
    H, _ = cv2.findHomography(mires, canon_mires, method=0)
    return cv2.warpPerspective(img, H, (canon_w, canon_h))


# --------------------------------------------------------------------------
# Recalage sans mires — sur la grille des cadres imprimés
#
# Quand les 4 mires manquent (coin plié, marge rognée au scan, photo cadrée
# trop serré), le repli historique était un `cv2.resize` brutal vers la taille
# canonique. Mesuré sur 12 vraies copies dont on masquait les mires : **33 %
# seulement des questions étaient lues juste** — autrement dit une page sans
# mires versait du bruit dans `raw_responses/` sans que rien ne le signale.
#
# Or la feuille porte bien mieux que 4 repères : les ~193 CADRES imprimés des
# cases, dont le calage donne la position exacte. On s'en sert comme d'un
# nuage d'appuis : initialisation grossière par corrélation avec le rendu du
# PDF du sujet, puis quelques homographies robustes successives sur les cadres
# retrouvés. Mesuré sur les mêmes 12 copies : **100 % des questions justes
# (434/434)**, avec un résidu de 0,8 à 1,6 px — meilleur que le recalage par
# les mires lui-même (3 à 8 px).
#
# Le critère d'acceptation est la contrepartie indispensable : une page qui
# n'est pas une feuille de réponses (la pub d'un scanner, un verso vierge) ne
# retrouve presque aucun cadre — 14 sur 193, corrélation 0,04 sur le cas réel
# rencontré. Elle est alors refusée et le pipeline reste sur l'ancien repli,
# donc sur un `method=cv_no_mires` visible, plutôt que de rendre une lecture
# plausible et fausse.
# --------------------------------------------------------------------------

FRAME_ALIGN_ITERS = 3          # homographies successives
FRAME_ALIGN_MIN_FRAC = 0.50    # fraction des cases dont le cadre doit être retrouvé
FRAME_ALIGN_MAX_RESID = 3.0    # px, résidu médian toléré à la fin
FRAME_ALIGN_MIN_PTS = 30       # appuis minimaux pour estimer une homographie
FRAME_ALIGN_SCALES = np.arange(0.92, 1.09, 0.02)   # échelles essayées à l'amorce
FRAME_ALIGN_CORR_SCALE = 0.25  # facteur de réduction pour la corrélation


def _frame_centres(warped: np.ndarray, boxes: list, canon: np.ndarray):
    """Centre du cadre détecté pour chaque case → (indices, centres détectés)."""
    idx, det = [], []
    for i, b in enumerate(boxes):
        c, _ = masked_detect.detect_frame(warped, b)
        if c is not None:
            idx.append(i)
            det.append(c.mean(axis=0))
    if not idx:
        return np.empty(0, int), np.empty((0, 2), np.float32)
    return np.array(idx), np.array(det, np.float32)


def _coarse_align_to_reference(gray: np.ndarray, ref: np.ndarray,
                               canon_w: int, canon_h: int) -> tuple:
    """Amorce : translation + échelle par corrélation avec le rendu du sujet.

    Insuffisant seul (4 à 11 px de résidu), mais assez pour que la détection
    des cadres prenne le relais — c'est elle qui fait la précision.
    """
    s0 = FRAME_ALIGN_CORR_SCALE
    small = cv2.resize(gray, None, fx=s0, fy=s0, interpolation=cv2.INTER_AREA)
    best = None
    for s in FRAME_ALIGN_SCALES:
        r = cv2.resize(ref, None, fx=s0 * s, fy=s0 * s, interpolation=cv2.INTER_AREA)
        if r.shape[0] > small.shape[0] or r.shape[1] > small.shape[1]:
            continue
        m = cv2.matchTemplate(small.astype(np.float32), r.astype(np.float32),
                              cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(m)
        if best is None or mx > best[0]:
            best = (float(mx), float(s), loc)
    if best is None:
        return cv2.resize(gray, (canon_w, canon_h)), 0.0
    score, s, loc = best
    M = np.array([[1 / s, 0, -loc[0] / s0 / s], [0, 1 / s, -loc[1] / s0 / s]], np.float32)
    return cv2.warpAffine(gray, M, (canon_w, canon_h), borderValue=255), score


def align_by_frames(gray: np.ndarray, lay: "layout_store.Layout",
                    ref: np.ndarray | None = None,
                    iters: int = FRAME_ALIGN_ITERS) -> tuple:
    """Recale une page SANS ses mires, sur la grille des cadres imprimés.

    Retourne `(warped, info)` où `info` porte `ok` (le recalage est-il digne de
    confiance), `n_frames`, `n_boxes`, `resid` (px, médian) et `corr`. Quand
    `ok` est faux, `warped` ne vaut pas mieux que le repli par redimensionnement
    et l'appelant doit le traiter comme tel.
    """
    boxes = lay.sheet_boxes()
    canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))
    info = {"ok": False, "n_frames": 0, "n_boxes": len(boxes),
            "resid": float("nan"), "corr": 0.0}
    if not boxes:
        return cv2.resize(gray, (canon_w, canon_h)), info
    canon = np.array([[(b.xmin + b.xmax) / 2, (b.ymin + b.ymax) / 2] for b in boxes],
                     np.float32)
    if ref is not None:
        warped, info["corr"] = _coarse_align_to_reference(gray, ref, canon_w, canon_h)
    else:
        warped = cv2.resize(gray, (canon_w, canon_h))
    for _ in range(max(1, iters)):
        idx, det = _frame_centres(warped, boxes, canon)
        if len(idx) < FRAME_ALIGN_MIN_PTS:
            info["n_frames"] = len(idx)
            return warped, info
        H, _ = cv2.findHomography(det, canon[idx], cv2.RANSAC, 3.0)
        if H is None:
            info["n_frames"] = len(idx)
            return warped, info
        warped = cv2.warpPerspective(warped, H, (canon_w, canon_h))
    idx, det = _frame_centres(warped, boxes, canon)
    info["n_frames"] = len(idx)
    if len(idx):
        info["resid"] = float(np.median(np.linalg.norm(det - canon[idx], axis=1)))
    info["ok"] = (len(idx) >= FRAME_ALIGN_MIN_FRAC * len(boxes)
                  and info["resid"] == info["resid"]            # non NaN
                  and info["resid"] <= FRAME_ALIGN_MAX_RESID)
    return warped, info


def box_fill_ratio(img_warped_gray: np.ndarray, box: BoxLayout, shrink: float = 0.18,
                   offset: tuple[int, int] = (0, 0)) -> float:
    """% de pixels noirs (post-binarisation) à l'intérieur du carré, ignorant le bord.

    shrink: fraction du côté à ignorer de chaque bord (évite le contour imprimé).
    offset: (dx, dy) à appliquer à la position canonique (utile après refinement).
    """
    dx_off, dy_off = offset
    x1 = int(box.xmin) + dx_off
    x2 = int(box.xmax) + dx_off
    y1 = int(box.ymin) + dy_off
    y2 = int(box.ymax) + dy_off
    dx = (x2 - x1) * shrink
    dy = (y2 - y1) * shrink
    xi1, xi2 = int(x1 + dx), int(x2 - dx)
    yi1, yi2 = int(y1 + dy), int(y2 - dy)
    crop = img_warped_gray[yi1:yi2, xi1:xi2]
    if crop.size == 0:
        return 0.0
    _, bw = cv2.threshold(crop, 128, 255, cv2.THRESH_BINARY_INV)
    return float(np.mean(bw > 0))


# Le seuil dur à 128 de `box_fill_ratio` rend « blanc » tout scan pâle dont
# l'encre plafonne au-dessus (constaté sur le code imprimé : triplet (0,0,0)).
# Pour tout ce qui n'est PAS une feature du GBM — code étudiant, grille copie,
# estimateur E2 du flagging — on mesure donc relativement au papier de la page,
# comme le fait déjà `masked_detect` : un point est sombre s'il est sous
# PAPER_DARK_FRAC × le niveau du papier. Mesuré sur 164 identités vérifiées :
# 163 codes étudiants exacts contre 161 avec le seuil dur.
# ⚠ Les features `fill_ratio_*` gardent `box_fill_ratio` : les changer
# impose de ré-entraîner le modèle.
PAPER_DARK_FRAC = masked_detect._DARK_FRAC


def paper_level(warped: np.ndarray) -> float:
    """Niveau de gris du papier (p85 de la page sous-échantillonnée), ≥ 1."""
    return max(float(np.percentile(warped[::8, ::8], 85)), 1.0)


def box_dark_ratio(img_warped_gray: np.ndarray, box: BoxLayout, paper: float,
                   shrink: float = 0.18, offset: tuple[int, int] = (0, 0)) -> float:
    """Comme `box_fill_ratio`, mais seuillé relativement au papier de la page."""
    dx_off, dy_off = offset
    x1 = int(box.xmin) + dx_off
    x2 = int(box.xmax) + dx_off
    y1 = int(box.ymin) + dy_off
    y2 = int(box.ymax) + dy_off
    dx = (x2 - x1) * shrink
    dy = (y2 - y1) * shrink
    crop = img_warped_gray[int(y1 + dy):int(y2 - dy), int(x1 + dx):int(x2 - dx)]
    if crop.size == 0:
        return 0.0
    return float(np.mean(crop < PAPER_DARK_FRAC * paper))


def detect_copy_id(warped: np.ndarray, layout: "layout_store.Layout",
                   offsets_by_q: dict | None = None,
                   shrink: float = 0.18, paper: float | None = None) -> int | None:
    """Lit la grille `\\AMCcode{copie}{N}` imprimée sur la feuille → entier copie.

    Convention AMC : `\\AMCcode{copie}{N}` crée N colonnes nommées `copie[1]`,
    `copie[2]`, …, `copie[N]` dans le calage, chacune avec 10 cases-chiffres
    `0`–`9`. **`copie[1]` = chiffre des unités** (la colonne la plus à droite
    visuellement), `copie[2]` = dizaines, etc. — on lit donc des plus hauts
    rangs vers les plus bas pour reconstruire le nombre.

    Retourne `None` si :
    - aucune colonne `copie[N]` dans le calage (sujet `\\exemplaire{1}`) ;
    - une colonne n'a aucune case cochée distinctement (max < 0.15 ou gap < 0.05)
      — dans ce cas le copy_id n'est pas fiable et l'appelant retombe sur 1.
    """
    copy_cols = layout.copy_id_columns()
    if not copy_cols:
        return None
    by_col: dict[int, list] = {}
    for b in layout.sheet_boxes():
        if b.question in set(copy_cols):
            by_col.setdefault(b.question, []).append(b)
    rank: dict[int, int] = {}
    for q in copy_cols:
        nm = layout.question_names.get(q, "")
        m = re.search(r"copie\[(\d+)\]", nm)
        rank[q] = int(m.group(1)) if m else q
    # Du chiffre de plus haut rang (N) au plus bas (1) pour concaténer.
    sorted_cols = sorted(copy_cols, key=lambda q: -rank[q])
    offsets_by_q = offsets_by_q or {}
    digits: list[str] = []
    for q in sorted_cols:
        cells = sorted(by_col.get(q, []), key=lambda b: b.char)
        if not cells:
            return None
        off = offsets_by_q.get(q, (0, 0))
        if paper is not None:
            ratios = [box_dark_ratio(warped, b, paper, shrink=shrink, offset=off) for b in cells]
        else:
            ratios = [box_fill_ratio(warped, b, shrink=shrink, offset=off) for b in cells]
        idx = int(np.argmax(ratios))
        sorted_r = sorted(ratios, reverse=True)
        gap = sorted_r[0] - (sorted_r[1] if len(sorted_r) > 1 else 0.0)
        if sorted_r[0] < 0.15 or gap < 0.05:
            return None
        digits.append(cells[idx].char)
    try:
        return int("".join(digits))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Code d'identification imprimé en haut de page (copie / page / checksum)
#
# AMC imprime ce code en cases noircies (`\AMC@binaryCode`, cf.
# tex/automultiplechoice.sty) et le calage en donne les positions exactes
# (`chiffre:<kind>,<rang>`). Il n'y a donc RIEN à faire remplir à l'étudiant :
# la grille manuelle `\AMCcode{copie}` n'est qu'un repli historique.
#
# Trois différences avec les cases cochées, qui dictent la méthode :
#  - le contenu est IMPRIMÉ : les ratios sont bimodaux (~0 / ~1), pas de marque
#    pâle, pas de gomme, donc pas besoin du GBM ;
#  - les cases se touchent (1,7 px d'écart pour 37,8 px de côté), donc
#    `refine_box_offset` est inutilisable : deux bits noirs voisins fusionnent
#    en un seul contour, rejeté par son filtre de taille ;
#  - le code est REDONDANT (copie + page + checksum) et le calage connaît la
#    liste des triplets valides — ce qui fournit un critère de vérité que les
#    cases réponses n'ont pas.
#
# D'où la stratégie : décoder à la position canonique ; si le triplet obtenu
# n'est pas dans la liste du calage, balayer un petit voisinage de décalages et
# n'accepter que si UN SEUL triplet valide en ressort. Un scan mal cadré est
# ainsi rattrapé, et une lecture douteuse est rejetée au lieu d'attribuer
# silencieusement la mauvaise copie.
# --------------------------------------------------------------------------

# Rattrapage de décalage, en deux temps : balayage GROSSIER large puis
# affinage local. Mesuré sur 173 scans réels d'EXAM_2026 : après recalage sur
# les mires, il subsiste jusqu'à ~24 px de dérive VERTICALE (1,7 mm à 300 dpi)
# sur les copies mal engagées dans le chargeur. Une fenêtre de ±16 px en
# ratait 19 ; ±32 les récupère, et le balayage en deux temps coûte le même
# nombre d'évaluations qu'un pas de 2 sur ±16.
CODE_SEARCH_PX = 20          # amplitude horizontale, et vers le HAUT
# La dérive constatée est nettement asymétrique : sur EXAM_2026, les copies mal
# engagées dans le chargeur glissent vers le BAS de 70 à 85 px après recalage
# sur les mires (2 à 3 mm à 300 dpi), jamais vers le haut. On balaye donc plus
# loin dans ce sens plutôt que d'élargir symétriquement, ce qui quadruplerait
# le coût pour rien.
CODE_DRIFT_DOWN = 120
CODE_COARSE_STEP = 4
CODE_FINE_PX = 3


def _code_cell_darkness(warped: np.ndarray, box, offset: tuple[int, int],
                        shrink: float, integral: "_CodeIntegral | None" = None) -> float:
    """Noirceur moyenne dans une case du code, sur [0, 1].

    ⚠ On n'utilise PAS `box_fill_ratio` ici : il binarise à 128 en dur, et un
    scan pâle dont l'encre plafonne à 130 rend alors tout le code « blanc »
    (constaté en test : le triplet devenait (0,0,0) et la page était refusée).
    Le contenu étant imprimé et bimodal, une mesure d'intensité BRUTE, seuillée
    ensuite relativement aux 24 bits de la page, se cale toute seule sur
    l'exposition du scan.

    `integral` : image intégrale de la bande du code, qui donne la même moyenne
    en 4 lectures au lieu d'un crop + `np.mean`. Le balayage teste des centaines
    de décalages : sans elle il coûte 104 ms, avec elle 2 ms (mesuré).
    """
    dx_off, dy_off = offset
    x1, x2 = int(box.xmin) + dx_off, int(box.xmax) + dx_off
    y1, y2 = int(box.ymin) + dy_off, int(box.ymax) + dy_off
    mx, my = (x2 - x1) * shrink, (y2 - y1) * shrink
    xa, xb = int(x1 + mx), int(x2 - mx)
    ya, yb = int(y1 + my), int(y2 - my)
    if integral is not None:
        m = integral.mean(xa, ya, xb, yb)
        return 0.0 if m is None else 1.0 - m / 255.0
    crop = warped[ya:yb, xa:xb]
    if crop.size == 0:
        return 0.0
    return 1.0 - float(np.mean(crop)) / 255.0


class _CodeIntegral:
    """Image intégrale de la bande horizontale portant le code imprimé.

    `cv2.integral` d'une image uint8 donne des sommes ENTIÈRES exactes : la
    moyenne d'un rectangle est donc identique au bit près à `np.mean` du crop
    correspondant — le balayage accéléré lit exactement les mêmes bits.
    """

    def __init__(self, warped: np.ndarray, y0: int, y1: int):
        h = warped.shape[0]
        self.y0 = max(0, min(y0, h))
        self.y1 = max(self.y0, min(y1, h))
        self.w = warped.shape[1]
        band = warped[self.y0:self.y1]
        self.ii = cv2.integral(band) if band.size else None

    def mean(self, xa: int, ya: int, xb: int, yb: int) -> float | None:
        """Moyenne sur [ya, yb) × [xa, xb), ou None si hors de la bande."""
        if self.ii is None or xb <= xa or yb <= ya:
            return None
        ya -= self.y0
        yb -= self.y0
        if xa < 0 or ya < 0 or xb > self.w or yb > self.ii.shape[0] - 1:
            return None
        s = (float(self.ii[yb, xb]) - float(self.ii[ya, xb])
             - float(self.ii[yb, xa]) + float(self.ii[ya, xa]))
        return s / ((yb - ya) * (xb - xa))


def _code_bits_value(bits: list, threshold: float) -> int | None:
    """Bits d'un même code → entier. **Rang 1 = poids fort** (vérifié sur un
    sujet compilé : copie 1 → `000000000001`, checksum 60 → `111100`)."""
    if not bits:
        return None
    return int("".join("1" if r > threshold else "0" for _, r in sorted(bits)), 2)


def _code_threshold(ratios: list[float]) -> float:
    """Seuil séparant bit noirci / bit vide, par le plus grand écart.

    Le contenu étant imprimé, les deux populations sont franches. On mutualise
    les 24 bits des trois codes : il est très improbable qu'ils soient tous
    identiques, donc l'écart existe presque toujours. Repli absolu sinon.

    Le seuil est RELATIF à la page : c'est ce qui rend la lecture insensible à
    l'exposition du scan (papier gris, encre pâle).
    """
    if not ratios:
        return 0.5
    srt = sorted(ratios, reverse=True)
    best_gap, boundary = 0.0, None
    for i in range(len(srt) - 1):
        gap = srt[i] - srt[i + 1]
        if gap > best_gap:
            best_gap, boundary = gap, (srt[i] + srt[i + 1]) / 2
    # Écart minimal exigé : en dessous, les deux populations ne se distinguent
    # pas et un seuil arbitraire inventerait des bits. On rend alors un seuil
    # médian, dont le triplet sera de toute façon rejeté par la validation.
    if best_gap >= 0.15 and boundary is not None:
        return boundary
    return 0.5


def _decode_code_at(warped: np.ndarray, code_boxes: list,
                    offset: tuple[int, int], shrink: float,
                    integral: "_CodeIntegral | None" = None) -> tuple | None:
    """Décode (copie, page, checksum) à un décalage donné, sans validation."""
    ratios = [(c, _code_cell_darkness(warped, c, offset, shrink, integral))
              for c in code_boxes]
    thr = _code_threshold([r for _, r in ratios])
    out = {}
    for kind in (1, 2, 3):
        bits = [(c.rank, r) for c, r in ratios if c.kind == kind]
        out[kind] = _code_bits_value(bits, thr)
    if any(out[k] is None for k in (1, 2, 3)):
        return None
    return (out[1], out[2], out[3])


def _sweep_decode(warped: np.ndarray, code_boxes: list, offsets: list,
                  shrink: float, integral: "_CodeIntegral") -> list:
    """Décode le code à TOUS les décalages d'un coup → `[(triplet | None), …]`.

    Même calcul que `_decode_code_at` appliqué à chaque décalage, mais en une
    passe numpy. Le balayage teste ≈ 400 décalages × 24 cases : en boucle
    Python, c'est 9 600 appels de fonction pour des mesures de 4 lectures
    chacune — le surcoût d'appel domine (mesuré : 104 ms avant l'image
    intégrale, 49 ms avec, 2 ms ici).

    Les rectangles se translatent EXACTEMENT avec le décalage (la marge de
    shrink ne dépend que de la taille de la case, entière), ce qui autorise à
    précalculer les bornes une fois et à leur ajouter (dx, dy).
    """
    ii = integral.ii
    if ii is None or not offsets:
        return [None] * len(offsets)
    order = sorted(range(len(code_boxes)), key=lambda i: (code_boxes[i].kind,
                                                          code_boxes[i].rank))
    xa0, xb0, ya0, yb0 = [], [], [], []
    for i in order:
        c = code_boxes[i]
        x1, x2 = int(c.xmin), int(c.xmax)
        y1, y2 = int(c.ymin), int(c.ymax)
        mx, my = (x2 - x1) * shrink, (y2 - y1) * shrink
        xa0.append(int(x1 + mx)); xb0.append(int(x2 - mx))
        ya0.append(int(y1 + my) - integral.y0); yb0.append(int(y2 - my) - integral.y0)
    xa0 = np.array(xa0); xb0 = np.array(xb0)
    ya0 = np.array(ya0); yb0 = np.array(yb0)
    off = np.asarray(offsets, dtype=np.int64)              # (N, 2)
    XA = xa0[None, :] + off[:, 0:1]
    XB = xb0[None, :] + off[:, 0:1]
    YA = ya0[None, :] + off[:, 1:2]
    YB = yb0[None, :] + off[:, 1:2]
    hh, ww = ii.shape[0] - 1, ii.shape[1] - 1
    ok = ((XA >= 0) & (YA >= 0) & (XB <= ww) & (YB <= hh)
          & (XB > XA) & (YB > YA)).all(axis=1)
    if not ok.any():
        return [None] * len(offsets)
    XA, XB, YA, YB = XA[ok], XB[ok], YA[ok], YB[ok]
    s = (ii[YB, XB] - ii[YA, XB] - ii[YB, XA] + ii[YA, XA]).astype(np.float64)
    dark = 1.0 - s / ((YB - YA) * (XB - XA)) / 255.0        # (M, 24)

    # Seuil par décalage : le plus grand écart entre deux mesures triées,
    # exigé ≥ 0.15 (cf. `_code_threshold`), repli 0.5 sinon.
    srt = -np.sort(-dark, axis=1)
    gaps = srt[:, :-1] - srt[:, 1:]
    j = gaps.argmax(axis=1)
    rows = np.arange(srt.shape[0])
    best = gaps[rows, j]
    boundary = (srt[rows, j] + srt[rows, j + 1]) / 2.0
    thr = np.where(best >= 0.15, boundary, 0.5)
    bits = dark > thr[:, None]

    # Bits → entier, rang 1 = poids fort, par kind.
    vals = {}
    pos = 0
    for kind in (1, 2, 3):
        n = sum(1 for i in order if code_boxes[i].kind == kind)
        if n == 0:
            return [None] * len(offsets)
        w = (1 << np.arange(n - 1, -1, -1)).astype(np.int64)
        vals[kind] = bits[:, pos:pos + n] @ w
        pos += n
    triples = list(zip(vals[1].tolist(), vals[2].tolist(), vals[3].tolist()))
    out: list = [None] * len(offsets)
    for k, i in enumerate(np.flatnonzero(ok).tolist()):
        out[i] = triples[k]
    return out


def _code_box_dy(warped: np.ndarray, box, span: int = 18,
                 base: int = 0) -> int | None:
    """Décalage vertical propre à UNE case du code, ou None si rien de net.

    On glisse une fenêtre de la hauteur de la case le long d'une bande verticale
    et on retient la position qui capte le plus d'encre. Marche pour les deux
    états : une case noircie est pleine, une case vide a ses deux bordures — dans
    les deux cas l'encre est maximale quand la fenêtre coïncide avec la case.

    `span` reste inférieur à l'écart entre les deux rangées du code (50 px),
    sinon une case vide pourrait s'accrocher à la rangée voisine.
    """
    x1, x2 = int(box.xmin), int(box.xmax)
    h = max(1, int(box.ymax - box.ymin))
    y1 = max(0, int(box.ymin) + base - span)
    y2 = min(warped.shape[0], int(box.ymax) + base + span)
    win = warped[y1:y2, x1:x2]
    if win.size == 0 or win.shape[0] <= h:
        return None
    ink = 1.0 - win.mean(axis=1) / 255.0
    if float(ink.max()) < 0.05:
        return None                      # bande vide : rien à aligner
    conv = np.convolve(ink, np.ones(h), mode="valid")
    return int(y1 + int(np.argmax(conv)) - int(box.ymin))


def _code_row_fit(warped: np.ndarray, code_boxes: list, base: int = 0) -> dict:
    """Décalage vertical par case, lissé par une droite ajustée sur chaque rangée.

    Sur les scans engagés de travers, la rangée n'est pas seulement décalée :
    elle est **inclinée et légèrement bombée** (constaté sur EXAM_2026). Aucun
    décalage global ne peut donc aligner les 24 cases à la fois. On ajuste une
    droite `dy = a·x + b` par rangée — l'inclinaison est la part dominante de la
    déformation — plutôt que d'utiliser les mesures brutes, trop bruitées case
    par case.
    """
    rows: dict = {}
    for c in code_boxes:
        rows.setdefault(round(c.ymin), []).append(c)
    out: dict = {}
    for _y, boxes in rows.items():
        pts = [(float(b.xmin), _code_box_dy(warped, b, base=base)) for b in boxes]
        pts = [(x, d) for x, d in pts if d is not None]
        if len(pts) < 4:
            continue
        xs = np.array([x for x, _ in pts], dtype=np.float64)
        ds = np.array([d for _, d in pts], dtype=np.float64)
        # Rejet des mesures aberrantes avant l'ajustement (une case peut
        # s'accrocher à une poussière) : on garde l'écart absolu médian.
        med = float(np.median(ds))
        keep = np.abs(ds - med) <= max(6.0, 3.0 * float(np.median(np.abs(ds - med))))
        if keep.sum() >= 3:
            xs, ds = xs[keep], ds[keep]
        a, b = np.polyfit(xs, ds, 1) if len(xs) >= 3 else (0.0, med)
        for box in boxes:
            out[(box.kind, box.rank)] = (0, int(round(a * float(box.xmin) + b)))
    return out


def _decode_code_warped(warped: np.ndarray, code_boxes: list,
                        offsets: dict, shrink: float) -> tuple | None:
    """Comme `_decode_code_at`, mais avec un décalage propre à chaque case."""
    ratios = [(c, _code_cell_darkness(warped, c,
                                      offsets.get((c.kind, c.rank), (0, 0)), shrink))
              for c in code_boxes]
    thr = _code_threshold([r for _, r in ratios])
    out = {}
    for kind in (1, 2, 3):
        bits = [(c.rank, r) for c, r in ratios if c.kind == kind]
        out[kind] = _code_bits_value(bits, thr)
    if any(out[k] is None for k in (1, 2, 3)):
        return None
    return (out[1], out[2], out[3])


def decode_page_code(warped: np.ndarray, layout: "layout_store.Layout",
                     shrink: float = 0.18,
                     search: int = CODE_SEARCH_PX,
                     step: int = CODE_COARSE_STEP) -> tuple | None:
    """Lit le code imprimé en haut de page → `(copie, page, checksum)`.

    Retourne `None` si le calage ne décrit pas ce code (sujet compilé par une
    version du style qui ne le trace pas, ou `layout.sqlite` d'AMC) ou si
    aucune lecture ne donne un triplet connu du calage.

    L'en-tête étant identique sur toutes les pages, on se sert des positions
    d'une page quelconque comme gabarit : c'est le code lui-même qui dit de
    quelle page il s'agit.
    """
    if not getattr(layout, "code_boxes", None) or not layout.page_ids:
        return None
    ref_page = min(c.page for c in layout.code_boxes)
    code_boxes = layout.code_boxes_on_page(ref_page)
    if not code_boxes:
        return None
    known = set(layout.page_ids)

    # Cas normal : scan bien cadré, lecture directe.
    got = _decode_code_at(warped, code_boxes, (0, 0), shrink)
    if got in known:
        return got

    # Rattrapage : on balaye un voisinage. La validation par triplet connu sert
    # aussi de critère d'alignement — inutile de deviner le recalage
    # géométriquement. L'image intégrale de la bande rend chaque essai
    # quasi gratuit (4 lectures par case au lieu d'un crop + moyenne).
    y0 = int(min(c.ymin for c in code_boxes)) - search - 4
    y1 = int(max(c.ymax for c in code_boxes)) + CODE_DRIFT_DOWN + 4
    integral = _CodeIntegral(warped, y0, y1)
    found = {}
    coarse_offsets = [(dx, dy)
                      for dy in range(-search, CODE_DRIFT_DOWN + 1, step)
                      for dx in range(-search, search + 1, step)
                      if not (dx == 0 and dy == 0)]
    coarse = []
    for o, got in zip(coarse_offsets, _sweep_decode(warped, code_boxes,
                                                    coarse_offsets, shrink, integral)):
        if got in known:
            found.setdefault(got, o)
            coarse.append(o)
    # Affinage autour des positions grossières retenues : un pas de 4 px peut
    # tomber juste à côté du bon alignement, et une case voisine à demi
    # recouverte suffit à inverser un bit.
    fine = [(dx, dy)
            for cx, cy in coarse[:4]
            for dy in range(cy - CODE_FINE_PX, cy + CODE_FINE_PX + 1)
            for dx in range(cx - CODE_FINE_PX, cx + CODE_FINE_PX + 1)]
    for o, got in zip(fine, _sweep_decode(warped, code_boxes, fine, shrink, integral)):
        if got in known:
            found.setdefault(got, o)
    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        # Plusieurs triplets DIFFÉRENTS : ambigu, on rend la main.
        return None

    # Toujours rien : la rangée est en plus inclinée ou bombée, aucun décalage
    # rigide ne l'aligne. On suit alors sa déformation case par case, en partant
    # du décalage vertical le plus probable.
    for base in (0, CODE_DRIFT_DOWN // 2):
        got = _decode_code_warped(
            warped, code_boxes, _code_row_fit(warped, code_boxes, base=base), shrink)
        if got in known:
            return got
    return None


def compute_per_question_offsets(warped: np.ndarray, layout: list) -> dict:
    """Pour chaque question, calcule le décalage médian de ses cases (dx, dy).

    Robuste : prend la médiane sur toutes les cases d'une question, ce qui
    annule les contours bruyants (cases noircies fusionnées).
    """
    offsets_by_q = {}
    for b in layout:
        dx, dy = refine_box_offset(warped, b)
        offsets_by_q.setdefault(b.question, []).append((dx, dy))
    out = {}
    for q, lst in offsets_by_q.items():
        dxs = sorted(d[0] for d in lst)
        dys = sorted(d[1] for d in lst)
        n = len(lst)
        med_dx = dxs[n // 2] if n else 0
        med_dy = dys[n // 2] if n else 0
        out[q] = (med_dx, med_dy)
    return out


def fill_ratio_shrink(warped: np.ndarray, box: "BoxLayout", shrink: float,
                      offset: tuple[int, int] = (0, 0)) -> float:
    """Variante de box_fill_ratio() avec shrink configurable (utilisée par extract_features)."""
    return box_fill_ratio(warped, box, shrink=shrink, offset=offset)


# Features dans l'ordre attendu par le classifieur : 18 « historiques » (mesure
# d'image brute) + les features « masquées » (cf. masked_detect.MASKED_FEATURE_COLS,
# noirceur hors de l'encre imprimée). Toute modif ⇒ ré-entraîner le GBM.
_BASE_FEATURE_COLS = [
    "fill_ratio_s05", "fill_ratio_s18", "fill_ratio_s30",
    "centroid_dx", "centroid_dy", "dark_std_x", "dark_std_y",
    "n_components", "largest_cc_frac",
    "mean_intensity", "std_intensity", "edge_density", "light_gray_ratio",
    "ratio_minus_q_median", "ratio_z_in_q", "ratio_minus_copy_baseline",
    "question_threshold", "ratio_above_threshold",
]
FEATURE_COLS = _BASE_FEATURE_COLS + MASKED_FEATURE_COLS

# Flagging — seuil ABSOLU de présence d'encre sur l'échelle masquée : une case
# vide vaut ~0 (σ≈0.03), une marque même pâle ≳0.2. Sert d'estimateur E1,
# indépendant de la calibration du GBM (cf. grade_image).
MASKED_INK_ABS = 0.12

# Une page est une « feuille de réponses » tant que MOINS de cette fraction de
# cases QCM voit son cadre imprimé absent du scan. Sur une vraie feuille ≈0.02–0.10 ;
# sur une page sans grille (recto sujet d'une copie multi-pages) ≈1.0. Seuil large
# (0.7) → ne jette jamais une vraie copie, même mal scannée. Cf. grade_image.
ANSWER_SHEET_FRAME_FAIL_FRAC = 0.7


def question_stats(q_ratios_s18) -> tuple:
    """(médiane, moyenne, écart-type, seuil adaptatif) des ratios d'une question.

    Ces quatre valeurs sont identiques pour toutes les cases d'une question :
    les recalculer case par case dans `extract_features` coûtait 17,6 ms par
    page — le premier poste de la fonction. `grade_image` les calcule une fois
    par question et les passe.
    """
    if len(q_ratios_s18) > 0:
        return (float(np.median(q_ratios_s18)), float(np.mean(q_ratios_s18)),
                float(np.std(q_ratios_s18)), adaptive_threshold(list(q_ratios_s18)))
    return (0.0, 0.0, 0.0, 0.50)


def extract_features(warped: np.ndarray, box: "BoxLayout",
                     q_ratios_s18: list[float], copy_baseline: float,
                     offset: tuple[int, int] = (0, 0),
                     ref: np.ndarray | None = None, ref_corners=None,
                     masked_feats: dict | None = None,
                     q_stats: tuple | None = None,
                     r18: float | None = None) -> dict:
    """Features d'une case (cf. FEATURE_COLS). q_ratios_s18 = ratios shrink=0.18 de la Q.

    Features masquées : `masked_feats` si déjà calculé (évite un recalcul), sinon
    dérivé de `ref`/`ref_corners` (rendu du PDF sujet — cf. masked_detect). Si la
    référence est absente → NaN (le GBM HistGradientBoosting gère les NaN).

    `q_stats` et `r18` : valeurs que l'appelant a déjà (cf. `question_stats`,
    et le ratio shrink 0.18 que `grade_image` mesure pour toutes les cases
    avant d'entrer ici). Sans elles, elles sont recalculées à l'identique."""
    dx, dy = offset
    x1 = int(box.xmin) + dx
    x2 = int(box.xmax) + dx
    y1 = int(box.ymin) + dy
    y2 = int(box.ymax) + dy

    # 1-3. Multi-shrink fill ratios (catch tick-in-corner vs full)
    r05 = fill_ratio_shrink(warped, box, 0.05, offset)
    if r18 is None:
        r18 = fill_ratio_shrink(warped, box, 0.18, offset)
    r30 = fill_ratio_shrink(warped, box, 0.30, offset)

    # Crop intérieur (shrink=0.18) pour features de forme/texture
    sx = (x2 - x1) * 0.18
    sy = (y2 - y1) * 0.18
    ix1, iy1, ix2, iy2 = int(x1 + sx), int(y1 + sy), int(x2 - sx), int(y2 - sy)
    crop = warped[iy1:iy2, ix1:ix2]
    if crop.size == 0:
        crop = np.zeros((1, 1), dtype=np.uint8)

    h, w = crop.shape
    _, bw = cv2.threshold(crop, 128, 255, cv2.THRESH_BINARY_INV)
    bw_mask = bw > 0
    n_dark = int(bw_mask.sum())

    # 4-7. Centroïde + dispersion des pixels noirs
    if n_dark > 5:
        ys, xs = np.where(bw_mask)
        cx_off = float(xs.mean() / max(1, w) - 0.5)
        cy_off = float(ys.mean() / max(1, h) - 0.5)
        std_x = float(xs.std() / max(1, w))
        std_y = float(ys.std() / max(1, h))
    else:
        cx_off = cy_off = 0.0
        std_x = std_y = 0.0

    # 8-9. Composantes connexes (tick vs fill vs croix)
    n_cc, labels = cv2.connectedComponents(bw, connectivity=8)
    n_components = max(0, n_cc - 1)
    if n_components > 0 and n_dark > 0:
        sizes = [int((labels == i).sum()) for i in range(1, n_cc)]
        largest_frac = max(sizes) / max(1, n_dark)
    else:
        largest_frac = 0.0

    # 10-11. Texture / intensité (crayon léger, hachures)
    mean_int = float(crop.mean())
    std_int = float(crop.std())

    # 12. Edge density
    if crop.size > 4:
        edges = cv2.Canny(crop, 50, 150)
        edge_density = float((edges > 0).mean())
    else:
        edge_density = 0.0

    # 13. Light-gray (Tipp-Ex residue)
    light_gray = float(((crop >= 120) & (crop <= 200)).mean())

    # 14-18. Context features (relatives à la même Q / copie)
    med_q, mean_q, std_q, t_q = (q_stats if q_stats is not None
                                 else question_stats(q_ratios_s18))
    z_q = (r18 - mean_q) / max(std_q, 0.01)

    feats = {
        "fill_ratio_s05": r05,
        "fill_ratio_s18": r18,
        "fill_ratio_s30": r30,
        "centroid_dx": cx_off,
        "centroid_dy": cy_off,
        "dark_std_x": std_x,
        "dark_std_y": std_y,
        "n_components": n_components,
        "largest_cc_frac": largest_frac,
        "mean_intensity": mean_int,
        "std_intensity": std_int,
        "edge_density": edge_density,
        "light_gray_ratio": light_gray,
        "ratio_minus_q_median": r18 - med_q,
        "ratio_z_in_q": z_q,
        "ratio_minus_copy_baseline": r18 - copy_baseline,
        "question_threshold": t_q,
        "ratio_above_threshold": r18 - t_q,
    }
    if masked_feats is None:
        if ref is not None:
            masked_feats = masked_detect.masked_features(warped, ref, box,
                                                         ref_corners, offset)
        else:
            masked_feats = {k: float("nan") for k in MASKED_FEATURE_COLS}
    feats.update(masked_feats)
    return feats


def adaptive_threshold(ratios: list[float], min_gap: float = 0.10,
                       abs_default: float = 0.50, abs_floor: float = 0.18,
                       copy_baseline: float | None = None) -> float:
    """Seuil par question basé sur la détection du gap le plus large.

    Idée : trier les ratios par ordre décroissant, trouver le gap le plus grand.
    Si gap ≥ min_gap, c'est la frontière naturelle entre cases cochées (au-dessus)
    et non cochées (en-dessous). Sinon, fallback sur seuil absolu (ajusté à la copie).

    copy_baseline: si fourni (médiane des ratios sur toute la copie), utilisé
    pour ajuster abs_default — pour les copies "light" (baseline ~0.15) on baisse
    le seuil absolu à baseline + 0.10.
    """
    if not ratios:
        return abs_default
    s = sorted(ratios, reverse=True)
    best_gap = 0.0
    boundary = None
    for i in range(len(s) - 1):
        gap = s[i] - s[i + 1]
        if gap > best_gap:
            best_gap = gap
            boundary = (s[i] + s[i + 1]) / 2
    if best_gap >= min_gap and boundary >= abs_floor:
        return boundary
    # fallback
    if copy_baseline is not None:
        # Pour une copie light, on autorise un seuil plus bas
        return max(abs_floor, copy_baseline + 0.10)
    return abs_default


def compute_copy_baseline(ratios_all: list[float]) -> float:
    """Médiane des ratios sur la copie entière — indicateur de 'darkness' globale.
    Une copie solide a baseline ~0.18-0.25 (lettres imprimées + quelques cases cochées).
    Une copie light a baseline ~0.15-0.20. Une copie vide ~0.15.
    """
    if not ratios_all:
        return 0.20
    s = sorted(ratios_all)
    return s[len(s) // 2]


def grade_image(image_path: Path | None = None, debug: bool = False,
                shrink: float = 0.18, adaptive: bool = True, refine: bool = True,
                *, gray: "np.ndarray | None" = None,
                page_index: int | None = None) -> dict:
    """Pipeline complet sur une image.

    Accepte soit un `image_path` (lecture cv2 + conversion gris), soit
    directement un array `gray` déjà décodé (pipeline fusionné PDF → pixmap
    → grade, évite l'I/O JPG quand on vient juste de rendre la page).

    refine : si True, ajuste la position de chaque case par détection de contour
    (médiane par question). Compense les déformations non-planaires de la photo.
    """
    if gray is None:
        if image_path is None:
            raise ValueError("grade_image: image_path ou gray requis")
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(image_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Page index pour le HTR freeform (Feature B) : déduit du nom de fichier
    # `page_NNN.jpg` si non passé explicitement. None → HTR sauté (sécurité).
    if page_index is None and image_path is not None:
        try:
            stem = Path(image_path).stem
            if stem.startswith("page_"):
                page_index = int(stem.split("_")[1])
        except (ValueError, IndexError):
            page_index = None

    lay = layout_store.get_layout()
    canon_mires = np.asarray(lay.mires, dtype=np.float32)
    canon_w, canon_h = int(round(lay.page_w)), int(round(lay.page_h))

    # Référence masquée (rendu du PDF sujet) — features masquées du GBM, et
    # amorce du recalage sans mires. Chargée avant le warp pour cette raison.
    try:
        ref_img, ref_frames = masked_detect.get_reference(lay)
    except Exception as e:
        ref_img, ref_frames = None, {}
        if debug:
            print(f"  ⚠️  référence masquée indisponible: {e}")

    mires = detect_mires(gray, layout=lay)
    method = "cv_full"
    align_info = None
    if mires is None or len(canon_mires) != 4:
        # Pas de mires : plutôt qu'un redimensionnement brutal (33 % de
        # questions justes, mesuré), on recale sur la grille des cadres
        # imprimés. Si ce recalage n'inspire pas confiance, on retombe sur
        # l'ancien repli et `method` le dit.
        warped, align_info = align_by_frames(gray, lay, ref_img)
        if align_info["ok"]:
            method = "cv_frames"
            if debug:
                print(f"  mires absentes → recalage sur {align_info['n_frames']} cadres "
                      f"imprimés (résidu {align_info['resid']:.2f} px)")
        else:
            method = "cv_no_mires"
            warped = cv2.resize(gray, (canon_w, canon_h))
            if debug:
                print(f"  ⚠️  ni mires ni cadres exploitables "
                      f"({align_info['n_frames']}/{align_info['n_boxes']} cadres) "
                      f"— traitement direct (probable échec)")
    else:
        warped = warp_to_canonical(gray, mires, canon_mires, canon_w, canon_h)

    layout = lay.sheet_boxes()
    offsets_by_q = compute_per_question_offsets(warped, layout) if refine else {}
    paper = paper_level(warped)   # niveau du papier, pour les mesures relatives

    # --- identification de la copie (sujets randomisés multi-copies) ---
    # Deux sources, dans cet ordre :
    #  1. le CODE IMPRIMÉ en haut de page — l'étudiant n'a rien à remplir, et le
    #     triplet (copie, page, checksum) est validé contre le calage, donc une
    #     lecture douteuse est refusée plutôt qu'attribuée au hasard ;
    #  2. à défaut, la grille manuelle `\AMCcode{copie}` (sujets compilés avant
    #     l'ajout de cette lecture, ou calage sans les cases `chiffre:*`).
    # Les positions des cases sont identiques entre copies : on lit avec le
    # layout courant (copie #1 par défaut) puis on recharge celui de la copie
    # réellement scannée pour avoir le bon mapping case ↔ lettre.
    page_code = decode_page_code(warped, lay)
    copy_src = "none"
    if page_code is not None:
        copy_id, page_no, _cs = page_code
        copy_src = "printed"
    else:
        copy_id = detect_copy_id(warped, lay, offsets_by_q, paper=paper) or 1
        page_no = None
        copy_src = "grid" if copy_id != 1 else "default"
    if copy_id != 1:
        available = layout_store.get_available_copies()
        if copy_id in available:
            lay = layout_store.get_layout(copy=copy_id)
            layout = lay.sheet_boxes()
        else:
            if debug:
                print(f"  ⚠️  copy_id={copy_id} détecté mais absent du calage "
                      f"(disponibles={available}) → repli sur copie 1")
            copy_id = 1
            copy_src = "default"
    if debug and page_code is not None:
        print(f"  code imprimé : copie {page_code[0]}, page {page_code[1]}")

    # Cases QCM (lettres) vs colonnes du code étudiant (chiffres) vs colonnes
    # de la grille `copie` (chiffres aussi) — distinguées via les tags du calage.
    copy_cols = set(lay.copy_id_columns())
    etu_cols = set(lay.student_id_columns())
    by_q: dict[int, list] = {}
    for b in layout:
        by_q.setdefault(b.question, []).append(b)
    # Repli heuristique pour les sujets dont le calage n'a pas de question_names :
    # toute colonne dont tous les chars sont des chiffres et qui n'est pas une
    # colonne `copie[N]` est traitée comme une colonne d'ID étudiant.
    if not etu_cols and not copy_cols:
        etu_cols = {q for q, bs in by_q.items() if _is_id_column(bs)}
    qcm_questions = sorted(q for q in by_q if q not in copy_cols and q not in etu_cols)
    id_columns = sorted(q for q in by_q if q in etu_cols)
    qcm_set = set(qcm_questions)

    # ratios par box
    fills = []
    for b in layout:
        off = offsets_by_q.get(b.question, (0, 0))
        r = box_fill_ratio(warped, b, shrink=shrink, offset=off)
        fills.append((b, r))

    # Classifieur GBM + copy baseline pour ses features
    clf_bundle = load_cell_classifier()
    all_ratios = [r for b, r in fills if b.question in qcm_set]
    copy_baseline = compute_copy_baseline(all_ratios)

    # Questions QCM : réponses
    # Décision finale d'une case = le GBM (qui tourne sur TOUTES les cases).
    # Flagging « levier 2 » : on signale une case douteuse quand des estimateurs
    # INDÉPENDANTS divergent ou sont incertains —
    #   E1 = seuil adaptatif sur le masked_ratio (noirceur hors encre imprimée),
    #   E2 = seuil adaptatif sur le fill_ratio shrink (mesure brute),
    #   E3 = décision du GBM,  E4 = predict_proba du GBM,
    #   E6 = contrainte structurelle (question `single` ⇒ exactement 1 cochée).
    # cf. masked_detect.py et FLAGGING_PLAN.md.
    qtypes = {}
    try:
        from sujet_store import effective_spec
        for q in qcm_questions:
            try:
                qtypes[q] = effective_spec(q).get("type", "mult")
            except Exception:
                qtypes[q] = "mult"
    except Exception:
        qtypes = {q: "mult" for q in qcm_questions}

    answers: dict[int, list[str]] = {}
    confidences = {}
    thresholds_used = {}
    ambiguous = []  # cases douteuses (liste de dicts → _ambiguous_cells)
    ml_overrides = 0
    n_frame_fail = 0  # cases QCM dont le cadre n'a pas été détecté dans le scan
    n_cells = 0       # total de cases QCM (dénominateur pour le ratio frame_fail)

    # Pré-calcul par question : tri, seuils, features masquées + features GBM.
    # On collecte les features de TOUTES les cases (toutes questions confondues)
    # pour faire ensuite UN SEUL appel `predict_proba` matriciel (vs 1 par case).
    # Bénéfice mesuré : sklearn HistGradientBoosting facture un overhead de
    # ~0.3 ms / appel quelle que soit la taille du batch → ~50× sur une page.
    q_data: dict = {}
    feat_rows: list = []  # 1 ligne par case, dans l'ordre (q, char)
    cell_keys: list = []  # (q, char) parallèle à feat_rows
    feat_cols = clf_bundle["feature_cols"] if clf_bundle is not None else None
    for q in qcm_questions:
        q_boxes = [(b, r) for b, r in fills if b.question == q]
        q_boxes.sort(key=lambda x: x[0].char)  # ordre canonique par lettre
        ratios = [r for _, r in q_boxes]
        t_q = adaptive_threshold(ratios)
        thresholds_used[q] = round(t_q, 3)
        off_q = offsets_by_q.get(q, (0, 0))
        # Estimateur E2 (flagging seulement, pas une feature) : même règle de
        # seuil adaptatif, mais sur la mesure relative au papier — un scan pâle
        # ne doit pas faire diverger E2 de E1/E3 sur toute la page.
        ratios_rel = [box_dark_ratio(warped, b, paper, shrink=shrink, offset=off_q)
                      for b, _r in q_boxes]
        t_q_rel = adaptive_threshold(ratios_rel)
        # features masquées par case — 1 seul calcul, réutilisé pour E1 et le GBM
        mfeats: dict = {}
        for b, _r in q_boxes:
            if ref_img is not None:
                mfeats[b.char] = masked_detect.masked_features(
                    warped, ref_img, b, ref_frames.get((q, b.answer)), off_q)
            else:
                mfeats[b.char] = {k: float("nan") for k in MASKED_FEATURE_COLS}
        q_data[q] = {"q_boxes": q_boxes, "ratios": ratios, "t_q": t_q,
                     "off_q": off_q, "mfeats": mfeats,
                     "ratios_rel": ratios_rel, "t_q_rel": t_q_rel}
        if clf_bundle is not None:
            q_st = question_stats(ratios)
            for b, r_b in q_boxes:
                # `r_b` EST le ratio shrink 0.18 de cette case (mesuré dans
                # `fills`) — sauf si l'appelant a changé `shrink`.
                feats = extract_features(warped, b, ratios, copy_baseline,
                                         offset=off_q, masked_feats=mfeats[b.char],
                                         q_stats=q_st,
                                         r18=r_b if shrink == 0.18 else None)
                feat_rows.append([feats[k] for k in feat_cols])
                cell_keys.append((q, b.char))

    # 1 seul predict_proba pour toute la page → vecteur de probas indexable par (q, char)
    if clf_bundle is not None and feat_rows:
        X = np.asarray(feat_rows, dtype=np.float64)
        probas_vec = clf_bundle["clf"].predict_proba(X)[:, 1]
        proba_by_cell = {key: float(probas_vec[i]) for i, key in enumerate(cell_keys)}
    else:
        proba_by_cell = {}

    for q in qcm_questions:
        d = q_data[q]
        q_boxes, t_q, mfeats = d["q_boxes"], d["t_q"], d["mfeats"]
        ratios_rel, t_q_rel = d["ratios_rel"], d["t_q_rel"]
        sel = []
        cells = []  # (b, r, mf, e1, e2, e3, proba)
        for k, (b, r) in enumerate(q_boxes):
            mf = mfeats[b.char]
            mr = mf["masked_ratio_e5"]
            # E1 : la détection masquée voit-elle de l'encre ? Seuil ABSOLU (case
            # vide ≈ 0). Indépendant de la calibration du GBM et du seuil adaptatif
            # → repère les marques pâles que E2/E3 ratent ensemble.
            e1 = (mr > MASKED_INK_ABS) if mr == mr else None
            e2 = ratios_rel[k] > t_q_rel                  # estimateur seuil brut (relatif papier)
            if clf_bundle is not None:
                proba = proba_by_cell[(q, b.char)]
                e3 = proba >= 0.5                         # décision GBM (finale)
            else:
                proba = None
                e3 = e2                                   # repli pur seuil
            if e3 != e2:
                ml_overrides += 1
            if e3:
                sel.append(b.char)
            cells.append((b, r, mf, e1, e2, e3, proba))
        answers[q] = sel
        confidences[q] = {b.char: round(r, 3) for b, r in q_boxes}

        # --- flagging : on signale toute case où les estimateurs divergent ---
        bad_struct = qtypes.get(q, "mult") == "single" and len(sel) != 1
        for b, r, mf, e1, e2, e3, proba in cells:
            n_cells += 1
            if mf["frame_detected"] == 0.0:
                n_frame_fail += 1
            reasons = []
            decs = [d for d in (e1, e2, e3) if d is not None]
            if len(set(decs)) > 1:
                reasons.append("disagree")          # E1/E2/E3 divergent
            if proba is not None and 0.30 <= proba <= 0.70:
                reasons.append("uncertain")         # GBM peu sûr (E4)
            if bad_struct:
                reasons.append("structural")        # contrainte `single` violée (E6)
            if reasons:
                mr = mf["masked_ratio_e5"]
                ambiguous.append({
                    "q": q, "char": b.char, "decision": bool(e3),
                    "ratio": round(r, 3),
                    "masked": round(mr, 3) if mr == mr else None,
                    "proba": round(proba, 3) if proba is not None else None,
                    "reasons": reasons,
                })

    # Code étudiant : une colonne de chiffres par position du numéro
    # Mesure relative au papier (pas une feature du GBM) : un scan pâle ne
    # doit pas rendre un code « ? » ou, pire, un faux chiffre.
    id_digits = []
    for col in id_columns:
        off = offsets_by_q.get(col, (0, 0))
        col_boxes = [(b, box_dark_ratio(warped, b, paper, shrink=shrink, offset=off))
                     for b in layout if b.question == col]
        col_boxes.sort(key=lambda x: x[0].char)  # '0'..'9'
        ratios = [r for _, r in col_boxes]
        if not ratios:
            id_digits.append("?")
            continue
        idx = int(np.argmax(ratios))
        max_r = ratios[idx]
        # confiance : différence entre max et second max
        sorted_r = sorted(ratios, reverse=True)
        gap = sorted_r[0] - (sorted_r[1] if len(sorted_r) > 1 else 0)
        if max_r < 0.15 or gap < 0.05:
            id_digits.append("?")
        else:
            id_digits.append(col_boxes[idx][0].char)
    student_id = "".join(id_digits)

    # notes : résumé lisible ; le détail par case vit dans _ambiguous_cells.
    notes = f"method={method}; mires={'ok' if mires is not None else 'FAIL'}"
    if align_info is not None and align_info["ok"]:
        notes += (f"; recalage=cadres({align_info['n_frames']}/{align_info['n_boxes']},"
                  f"resid={align_info['resid']:.1f})")
    # `copy_id` est noté dès qu'on a su l'identifier — le code imprimé existe
    # même sur les sujets à un seul exemplaire, où il donne aussi le n° de page.
    if copy_cols or copy_src == "printed":
        notes += f"; copy_id={copy_id}({copy_src})"
        if page_no is not None:
            notes += f"; page={page_no}"
    if clf_bundle is not None:
        notes += f"; ml=on(overrides={ml_overrides})"
    if ambiguous:
        cells_str = " ".join(f"Q{a['q']}_{a['char']}" for a in ambiguous)
        notes += f"; ambigu({len(ambiguous)}): {cells_str}"
    if n_frame_fail:
        notes += f"; frame_fail={n_frame_fail}"
    if "?" in student_id:
        notes += f"; ID_INCOMPLET ({student_id})"

    # --- Feature B : lecture HTR des cases freeform -----------------------
    # Sauté si : `htr` indisponible, pas de `sujet/open_zones.json`, ou
    # page_index inconnu (mode in-memory sans nom de fichier).
    open_answers = _grade_freeform_open_answers(warped, page_index) if page_index else {}

    # Détection « cette page est-elle une feuille de réponses ? » — utile quand
    # une copie fait plusieurs pages (recto sujet + verso réponses) : la page
    # sans grille de cases voit ~tous ses cadres échouer. Signal robuste, indépen-
    # dant de ce que l'étudiant a coché. Si la référence (PDF sujet) est absente,
    # frame_detected n'est jamais évalué (n_frame_fail=0) → on garde la page.
    is_answer_sheet = not (n_cells > 0 and n_frame_fail >= ANSWER_SHEET_FRAME_FAIL_FRAC * n_cells)

    return {
        "student_name": "",
        "student_id": student_id,
        "answers": {q: answers[q] for q in qcm_questions},
        "open_answers": open_answers,
        "notes": notes,
        "_cv_confidences": confidences,
        "_cv_method": method,
        "_ambiguous_cells": ambiguous,
        "_copy_id": copy_id,
        # Provenance du numéro de copie : "printed" = code imprimé en haut de
        # page (aucune saisie de l'étudiant, validé par checksum), "grid" =
        # grille manuelle, "default" = rien de lisible, copie 1 par défaut.
        "_copy_id_source": copy_src,
        # Numéro de page lu dans ce même code (None si code illisible/absent).
        "_page_no": page_no,
        "_n_frame_fail": n_frame_fail,
        "_n_cells": n_cells,
        "_is_answer_sheet": is_answer_sheet,
    }


# Cache module-level pour éviter de relire open_zones.json à chaque page.
_OPEN_ZONES_CACHE: dict | None = None
_OPEN_ZONES_MTIME: float = 0.0


def _load_open_zones() -> dict:
    """Charge `sujet/open_zones.json` (créé par `sujet_store.calibrate_open_zones`).

    Cache mtime → reload auto après une nouvelle compilation. Retourne `{}`
    si absent (pas de bloc freeform dans le sujet → HTR sauté).
    """
    global _OPEN_ZONES_CACHE, _OPEN_ZONES_MTIME
    path = ROOT / "sujet" / "open_zones.json"
    if not path.exists():
        _OPEN_ZONES_CACHE = {}
        _OPEN_ZONES_MTIME = 0.0
        return _OPEN_ZONES_CACHE
    mt = path.stat().st_mtime
    if _OPEN_ZONES_CACHE is not None and mt == _OPEN_ZONES_MTIME:
        return _OPEN_ZONES_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            _OPEN_ZONES_CACHE = json.load(f)
        _OPEN_ZONES_MTIME = mt
    except Exception:
        _OPEN_ZONES_CACHE = {}
    return _OPEN_ZONES_CACHE


def _grade_freeform_open_answers(warped: np.ndarray, page_index: int) -> dict:
    """Pour chaque case freeform dont la `page` calibrée matche `page_index`,
    crop le warped + lance le HTR + match contre `expected_answer`.

    Retourne `{<question_idx>: {raw_text, score, expected, match_mode,
    confidence, bid, tag, points}}` ou `{}` si HTR indispo / pas de zone.
    """
    zones = _load_open_zones()
    if not zones:
        return {}
    try:
        import htr  # import paresseux : pas chargé sans extra `[htr]`
    except ImportError:
        return {}
    if not htr.is_available():
        return {}
    # Conversion coords PDF (72dpi) → canonique (300dpi). Ratio fixe parce que
    # le warp_to_canonical normalise sur (page_w, page_h) en pixels 300dpi.
    SCALE = 300.0 / 72.0
    h_w, w_w = warped.shape[:2]
    out: dict = {}
    # Numéro de question = ordinal du bloc freeform parmi les blocs freeform,
    # commençant à 1 (pas le même index que les QCM).
    sorted_bids = sorted(zones.keys(), key=lambda b: (
        zones[b].get("page", 0), zones[b].get("ymin", 0)))
    for q_ord, bid in enumerate(sorted_bids, start=1):
        z = zones[bid]
        if int(z.get("page", 0)) != int(page_index):
            continue
        x1 = max(0, int(z["xmin"] * SCALE))
        y1 = max(0, int(z["ymin"] * SCALE))
        x2 = min(w_w, int(z["xmax"] * SCALE))
        y2 = min(h_w, int(z["ymax"] * SCALE))
        if x2 <= x1 or y2 <= y1:
            continue
        crop_gray = warped[y1:y2, x1:x2]
        crop_rgb = cv2.cvtColor(crop_gray, cv2.COLOR_GRAY2RGB)
        try:
            ocr = htr.recognize_text(crop_rgb)
        except Exception as e:  # noqa: BLE001
            out[str(q_ord)] = {"bid": bid, "tag": z.get("tag", ""),
                               "raw_text": "", "score": 0.0,
                               "expected": z.get("expected_answer", ""),
                               "match_mode": z.get("match_mode", "exact"),
                               "confidence": 0.0, "error": str(e),
                               "points": z.get("points", 1.0)}
            continue
        ok = htr.match_answer(ocr["text"],
                              z.get("expected_answer", ""),
                              mode=z.get("match_mode", "exact"),
                              numeric_tol=float(z.get("numeric_tol") or 0.01))
        score = float(z.get("points", 1.0)) if ok else 0.0
        out[str(q_ord)] = {
            "bid": bid,
            "tag": z.get("tag", ""),
            "raw_text": ocr["text"],
            "score": score,
            "expected": z.get("expected_answer", ""),
            "match_mode": z.get("match_mode", "exact"),
            "confidence": round(float(ocr["confidence"]), 3),
            "points": float(z.get("points", 1.0)),
        }
    return out


# Clés privées de `grade_image` qui DOIVENT survivre dans raw_responses_cv/ :
# le seed les propage vers raw_responses/ et le serveur les lit (`copy_id_of`
# pour noter avec la bonne carte case↔lettre, `_is_answer_sheet` pour écarter
# une page sans grille). Les autres clés `_…` (confidences, méthode) restent
# internes.
_JSON_PRIVATE_KEYS = ("_ambiguous_cells", "_is_answer_sheet", "_n_frame_fail",
                      "_n_cells", "_copy_id", "_copy_id_source", "_page_no")


def result_to_json(r: dict) -> dict:
    """Le résultat de `grade_image` → dict à écrire dans raw_responses_cv/.

    Une seule définition pour les deux voies (`grade_many` et la voie
    fusionnée) : elles produisaient deux fichiers différents, et aucune des
    deux n'écrivait `_copy_id` — le numéro de copie était donc perdu entre le
    grade et le serveur.
    """
    to_save = {k: v for k, v in r.items() if not k.startswith("_")}
    to_save["answers"] = {str(k): v for k, v in to_save["answers"].items()}
    to_save["_ambiguous_cells"] = r.get("_ambiguous_cells", [])
    for k in _JSON_PRIVATE_KEYS:
        if k in r:
            to_save[k] = r[k]
    return to_save


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _grade_and_write(img_path_str: str, out_path_str: str) -> dict:
    """Grade une image et écrit son JSON. Retourne un résumé léger pour IPC.

    Module-level (picklable) → utilisable par ProcessPoolExecutor.
    """
    img_path = Path(img_path_str)
    r = grade_image(img_path)
    _write_json(Path(out_path_str), result_to_json(r))
    return {
        "method": r["_cv_method"],
        "student_id": r["student_id"],
        "n_cells": sum(len(v) for v in r["answers"].values()),
    }


def grade_many(targets, out_dir: Path,
               workers: int | None = None, on_progress=None) -> list:
    """Grade plusieurs pages (en parallèle si workers>1, sinon série).

    `targets` : liste de `(batch, img_path)`. `out_dir` : dossier des JSON.
    `workers=None`/0 → auto (cpu_count, cap à len(targets)). `workers=1` →
    boucle série (zéro overhead Pool, débuggable). `on_progress(done, total,
    batch, page_name, summary_or_exception)` appelé après chaque page.

    Retourne `[(batch, img_path, summary_or_exception), …]`.
    """
    targets = list(targets)
    w = _resolve_workers(workers, len(targets))
    results: list = []
    out_dir = Path(out_dir)

    def _out_for(batch: str, img_path: Path) -> Path:
        return out_dir / batch / img_path.with_suffix(".json").name

    if w == 1:
        for i, (batch, img_path) in enumerate(targets):
            try:
                r = _grade_and_write(str(img_path), str(_out_for(batch, img_path)))
                results.append((batch, img_path, r))
            except Exception as e:
                results.append((batch, img_path, e))
                r = e
            if on_progress:
                on_progress(i + 1, len(targets), batch, img_path.name, r)
        return results

    from concurrent.futures import ProcessPoolExecutor, as_completed
    import multiprocessing as _mp
    # `spawn` plutôt que `fork` (défaut Linux) : `fork` après import de
    # sklearn/cv2/numpy hérite des verrous OpenMP/BLAS de l'état du parent —
    # déjà observé : deadlock total du Pool après une 1ère exécution dans le
    # même process. `spawn` repart d'un interpréteur propre. Coût : ~200 ms
    # par worker au démarrage, négligeable sur >30 pages.
    ctx = _mp.get_context("spawn")
    with _single_thread_env(), ProcessPoolExecutor(
            max_workers=w, mp_context=ctx, initializer=_worker_init) as ex:
        futures = {
            ex.submit(_grade_and_write, str(img_path),
                      str(_out_for(batch, img_path))):
            (batch, img_path)
            for batch, img_path in targets
        }
        done = 0
        for fut in as_completed(futures):
            batch, img_path = futures[fut]
            try:
                r = fut.result()
                results.append((batch, img_path, r))
            except Exception as e:
                results.append((batch, img_path, e))
                r = e
            done += 1
            if on_progress:
                on_progress(done, len(targets), batch, img_path.name, r)
    return results


def _fused_worker(args):
    """Worker pipeline fusionné : PDF page → pixmap → grade + JPG.

    Reçoit `(pdf_path, page_idx, dpi, quality, jpg_path, json_path)`.
    Ouvre le PDF, rend UNE page en pixmap, écrit le JPG (pour l'UI) et grade en
    mémoire directement depuis le gray (skip cv2.imread + décodage JPEG).
    Retourne un summary léger pour IPC.
    """
    (pdf_path_str, page_idx, dpi, quality,
     jpg_path_str, json_path_str) = args
    import fitz  # PyMuPDF, importé dans le worker

    pdf_path = Path(pdf_path_str)
    jpg_path = Path(jpg_path_str)
    json_path = Path(json_path_str)
    jpg_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    try:
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = doc[page_idx].get_pixmap(matrix=mat)
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, 3)
        # JPG pour l'UI via cv2.imwrite (libjpeg-turbo C natif → 2× plus rapide
        # que PIL avec `optimize=True`). cv2 attend BGR, donc 1 cvtColor RGB→BGR.
        # Le gris pour le grade est extrait du RGB en parallèle (cvtColor C).
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(jpg_path), bgr,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    finally:
        doc.close()

    # `page_index` : n° de page dans le PDF, comme le nom `page_NNN.jpg` — sert
    # au HTR freeform (Feature B), qui reste sauté sans clé API.
    r = grade_image(gray=gray, page_index=page_idx + 1)
    _write_json(json_path, result_to_json(r))

    return {
        "pdf": pdf_path.stem, "page": page_idx + 1,
        "method": r["_cv_method"], "student_id": r["student_id"],
        "n_cells": sum(len(v) for v in r["answers"].values()),
    }


def grade_pdfs_fused(pdfs, jpg_root: Path, json_root: Path,
                     dpi: int = 300, quality: int = 85,
                     workers: int | None = None, on_progress=None) -> list:
    """Pipeline fusionné PDF → (JPG d'UI + JSON CV) en 1 seule passe parallèle.

    Pour chaque page de chaque PDF, 1 future : render pixmap, écrit le JPG dans
    `jpg_root/<pdf_stem>/page_NNN.jpg` (pour la vue dans l'UI) ET grade en
    mémoire (sans relire le JPG) → `json_root/<pdf_stem>/page_NNN.json`.

    Gain vs `extract_many + grade_many` : on évite `cv2.imread` + le décodage
    JPEG côté grade (~80-130 ms/page), et la perte de qualité JPG ne contamine
    plus la mesure CV (qui voit le pixmap brut).

    `on_progress(done, total, pdf_stem, page_num, summary_or_exception)`.
    Retourne `[(pdf_stem, page_num, summary_or_exception), …]`.
    """
    # Découvre les pages : 1 entrée par (pdf, page_idx).
    import fitz
    tasks = []
    for pdf in pdfs:
        pdf = Path(pdf)
        doc = fitz.open(str(pdf))
        try:
            n_pages = doc.page_count
        finally:
            doc.close()
        for i in range(n_pages):
            jpg = jpg_root / pdf.stem / f"page_{i + 1:03d}.jpg"
            jsn = json_root / pdf.stem / f"page_{i + 1:03d}.json"
            tasks.append((str(pdf), i, dpi, quality, str(jpg), str(jsn)))

    w = _resolve_workers(workers, len(tasks))
    results: list = []

    if w == 1:
        for i, args in enumerate(tasks):
            pdf_stem = Path(args[0]).stem
            page_num = args[1] + 1
            try:
                summary = _fused_worker(args)
                results.append((pdf_stem, page_num, summary))
            except Exception as e:
                results.append((pdf_stem, page_num, e))
                summary = e
            if on_progress:
                on_progress(i + 1, len(tasks), pdf_stem, page_num, summary)
        return results

    from concurrent.futures import ProcessPoolExecutor, as_completed
    import multiprocessing as _mp
    ctx = _mp.get_context("spawn")
    with _single_thread_env(), ProcessPoolExecutor(
            max_workers=w, mp_context=ctx, initializer=_worker_init) as ex:
        futures = {ex.submit(_fused_worker, args):
                   (Path(args[0]).stem, args[1] + 1)
                   for args in tasks}
        done = 0
        for fut in as_completed(futures):
            pdf_stem, page_num = futures[fut]
            try:
                summary = fut.result()
                results.append((pdf_stem, page_num, summary))
            except Exception as e:
                results.append((pdf_stem, page_num, e))
                summary = e
            done += 1
            if on_progress:
                on_progress(done, len(tasks), pdf_stem, page_num, summary)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--failed-only", action="store_true")
    ap.add_argument("--out-dir", default=None,
                    help="dossier où écrire les JSONs (défaut: raw_responses_cv/)")
    ap.add_argument("--workers", type=int, default=0,
                    help="0 = auto (cpu_count). 1 = série (debug / mono-coeur).")
    args = ap.parse_args()

    if args.image:
        r = grade_image(Path(args.image), debug=args.debug)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "raw_responses_cv")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.failed_only:
        cap = config.amc_data_dir() / "capture.sqlite"
        if not cap.exists():
            print(f"capture.sqlite introuvable ({cap}) — mode --failed-only "
                  f"indisponible sans analyse AMC. Utilise --all.")
            return
        c = sqlite3.connect("file:%s?mode=ro" % cap, uri=True)
        failed = c.execute("SELECT filename FROM capture_failed").fetchall()
        c.close()
        targets = []
        for (fn,) in failed:
            m = re.search(r"(\d+)_.*pdf-page-(\d+)-", fn)
            if not m:
                continue
            img = PAGES_DIR / f"batch{m.group(1)}" / f"page_{int(m.group(2)):03d}.jpg"
            if img.exists():
                targets.append((f"batch{m.group(1)}", img))
    elif args.all:
        targets = []
        for d in sorted((ROOT / "pages").iterdir()):
            if not d.is_dir():
                continue
            for p in sorted(d.glob("page_*.jpg")):
                targets.append((d.name, p))
    else:
        ap.print_help()
        return

    print(f"À traiter: {len(targets)} pages (workers={_resolve_workers(args.workers, len(targets))})")
    n_ok = 0
    n_no_mires = 0
    n_failed = 0

    def _progress(done, total, batch, page_name, summary):
        nonlocal n_ok, n_no_mires, n_failed
        if isinstance(summary, Exception):
            n_failed += 1
            print(f"  [{done}/{total}] ✘ {batch}/{page_name}: {summary}")
            return
        tag = "OK" if summary["method"] == "cv_full" else "NO_MIRES"
        if tag == "OK":
            n_ok += 1
        else:
            n_no_mires += 1
        print(f"  [{done}/{total}] [{tag:8s}] {batch}/{page_name}  "
              f"id={summary['student_id']}  cells={summary['n_cells']}")

    grade_many(targets, out_dir=out_dir, workers=args.workers, on_progress=_progress)

    print(f"\nFini. OK={n_ok}  no_mires={n_no_mires}  fail={n_failed}")
    print(f"JSONs dans: {out_dir}")


if __name__ == "__main__":
    main()
