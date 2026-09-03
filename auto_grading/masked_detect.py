"""masked_detect.py — détection « masquée » de case cochée.

Une case vide n'est pas « propre » : son crop contient le **cadre imprimé** et une
**lettre imprimée** (A, B, C…) au centre, dont l'encre varie fortement d'une lettre
à l'autre. Mesurer la noirceur brute du crop biaise donc la décision par-lettre.

Ce module mesure la noirceur **uniquement hors de l'encre imprimée** :

1. Référence = rendu 300 dpi de la feuille de réponses du **PDF du sujet**
   (`render_reference`) — des cases vides parfaites, sans bruit de scan.
2. Calage par case : on détecte le **cadre carré** dans le scan **et** dans la
   référence (`detect_frame`), puis on estime une similarité réf→scan
   (translation + rotation + échelle) sur les 4 coins.
3. Masque = encre imprimée de la référence calée (`ref < 200`, dilatée).
4. Mesure = fraction de points sombres **hors masque**, à l'intérieur érodé de la
   case, relative au niveau du papier (p85 du crop). Aucune soustraction d'image.

API :
  - `get_reference(lay)`  → `(ref_img, ref_frames)`, mis en cache (mtime du PDF) ;
  - `masked_features(...)` → dict des features masquées (cf. `MASKED_FEATURE_COLS`),
    branché dans `cv_grade.extract_features` / `FEATURE_COLS`.

Spec/banc d'essai : `proto_mask_benchmark.py`.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import fitz
import numpy as np

import config

ROOT = Path(__file__).resolve().parent

# --- paramètres de la mesure masquée (réglés via proto_mask_benchmark.py) ----
MARGIN = 18              # marge (px) ajoutée autour de la case pour le crop de travail
_DARK_FRAC = 0.60        # un point est « sombre » s'il est < _DARK_FRAC × niveau papier
_MASK_DILATE = 4         # dilatation du masque d'encre imprimée (halo flou du scan)
_EROSIONS = (3, 5, 7)    # érosions de l'intérieur de la case → 3 mesures
_MIN_BG_PX = 25          # nb minimal de points hors masque pour une mesure fiable
_K3 = np.ones((3, 3), np.uint8)
# Dilater n fois par un carré 3×3 == dilater une fois par un carré (2n+1).
_DILATE_K = np.ones((2 * _MASK_DILATE + 1, 2 * _MASK_DILATE + 1), np.uint8)

# Features masquées ajoutées au classifieur GBM (ordre figé — cf. cv_grade.FEATURE_COLS).
MASKED_FEATURE_COLS = [
    "masked_ratio_e3", "masked_ratio_e5", "masked_ratio_e7",
    "frame_detected", "align_residual",
]


# ==========================================================================
# Référence : rendu du PDF du sujet
# ==========================================================================

def _subject_pdf() -> Path:
    """Chemin du PDF du sujet (feuille de réponses imprimée).

    Le PDF du sujet est une *donnée du projet* (produit par `compile_pdf` dans
    `project_root()/sujet/`), pas un artefact du code installé — on le cherche
    donc dans le projet actif, jamais dans le dossier d'installation.
    """
    for cand in (config.amc_dir() / "DOC-sujet.pdf",
                 config.project_root() / "sujet" / "DOC-sujet.pdf"):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "DOC-sujet.pdf introuvable (ni dans amc_dir, ni dans sujet/). "
        "Compile le sujet (onglet Sujet) pour produire sujet/DOC-sujet.pdf.")


def render_reference(lay, page: int | None = None) -> np.ndarray:
    """Rendu 300 dpi, en gris, d'une feuille de réponses du PDF du sujet.

    `page` : numéro de page AMC (1-based). Par défaut la feuille principale —
    un sujet dont les réponses débordent sur plusieurs feuilles en a une par
    page, et chacune a sa propre référence.
    """
    if page is None:
        page = lay.answer_sheet_page
    doc = fitz.open(str(_subject_pdf()))
    try:
        pix = doc[page - 1].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
    finally:
        doc.close()
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY) if pix.n >= 3 else a[:, :, 0].copy()


# ==========================================================================
# Détection du cadre carré d'une case
# ==========================================================================

def _order_corners(pts) -> np.ndarray:
    """Trie 4 points par angle autour de leur centre (ordre stable réf↔scan)."""
    pts = np.asarray(pts, dtype=np.float32)
    c = pts.mean(axis=0)
    return pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]


def ref_corners_global(box) -> np.ndarray:
    """4 coins du rectangle canonique de la case (repli si le cadre n'est pas détecté)."""
    cx, cy = (box.xmin + box.xmax) / 2.0, (box.ymin + box.ymax) / 2.0
    bw, bh = box.xmax - box.xmin, box.ymax - box.ymin
    return _order_corners(cv2.boxPoints(((cx, cy), (bw, bh), 0.0)))


def detect_frame(image: np.ndarray, box, margin: int = MARGIN):
    """Détecte le cadre carré de la case → (4 coins globaux, raison_échec).

    raison_échec ∈ {'', 'oob', 'no_contour', 'taille', 'forme'} ; coins=None si échec.
    Otsu + findContours + minAreaRect : on garde le contour ~carré de la bonne
    taille le plus centré. `image` peut être le scan warpé OU la référence.
    """
    bx1, by1 = int(box.xmin), int(box.ymin)
    bw, bh = int(box.xmax) - bx1, int(box.ymax) - by1
    x0, y0 = bx1 - margin, by1 - margin
    w, h = bw + 2 * margin, bh + 2 * margin
    if x0 < 0 or y0 < 0 or x0 + w > image.shape[1] or y0 + h > image.shape[0]:
        return None, "oob"
    crop = image[y0:y0 + h, x0:x0 + w].astype(np.uint8)
    _, binv = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(binv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    target = (bw + bh) / 2.0
    cx0, cy0 = w / 2.0, h / 2.0
    best, best_d, seen = None, 1e18, ""
    for c in cnts:
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
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
            best_d, best = d, ((cx, cy), (rw, rh), ang)
    if best is None:
        return None, (seen or "no_contour")
    return _order_corners(cv2.boxPoints(best) + np.array([x0, y0], np.float32)), ""


# ==========================================================================
# Mesure masquée
# ==========================================================================

def paper_p85(crop: np.ndarray) -> float:
    """85ᵉ centile d'un crop uint8 = niveau du papier, par histogramme.

    Donne EXACTEMENT ce que rend `np.percentile(crop, 85)` — même position
    virtuelle `(n-1)·0.85`, même interpolation linéaire, y compris sa forme
    inversée au-delà de la moitié — mais en 20 µs au lieu de 72 : le tri
    partiel de numpy coûtait à lui seul un quart de la mesure masquée.
    Vérifié sur 50 000 crops aléatoires, dont uniformes et minuscules.
    """
    n = crop.size
    if n == 0:
        return 0.0
    c = np.cumsum(np.bincount(crop.ravel(), minlength=256))
    v = (n - 1) * 0.85
    i = int(v)
    t = v - i
    lo = float(np.searchsorted(c, i + 1))
    hi = float(np.searchsorted(c, i + 2)) if i + 1 < n else lo
    d = hi - lo
    return (lo + d * t) if t < 0.5 else (hi - d * (1 - t))


def _align_residual(ref_corners: np.ndarray, scan_corners: np.ndarray,
                    M: np.ndarray | None = None) -> float:
    """MSE des 4 coins après la similarité réf→scan = qualité du calage.

    ~0 si le cadre détecté est une copie homothétique propre du cadre de
    référence ; élevé si la détection est mauvaise ou la copie distordue.

    `M` : la similarité, si l'appelant l'a déjà estimée. Sans elle, on la
    ré-estimait par case alors que `_masked_ratios` venait de le faire.
    """
    if M is None:
        M, _ = cv2.estimateAffinePartial2D(ref_corners, scan_corners)
    if M is None:
        return float("nan")
    proj = ref_corners @ M[:, :2].T + M[:, 2]
    return float(np.mean(np.sum((proj - scan_corners) ** 2, axis=1)))


def _masked_ratios(warped: np.ndarray, ref: np.ndarray, box,
                   ref_corners: np.ndarray, scan_corners: np.ndarray,
                   erosions=_EROSIONS, margin: int = MARGIN) -> tuple | None:
    """Fraction de points sombres hors masque, pour plusieurs érosions de l'intérieur.

    Retourne `({érosion: ratio|None}, M)` où `M` est la similarité réf→scan
    estimée au passage (l'appelant en a besoin pour `align_residual` et la
    ré-estimer coûtait une deuxième fois le prix) ; None si le crop est hors
    image ou la similarité non estimable. `ref_corners`/`scan_corners`
    viennent du même détecteur → la similarité est sans biais d'échelle.
    """
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
    # Le crop reste en uint8 : `np.percentile` y donne le MÊME résultat que sur
    # sa copie float32 (vérifié), et la conversion de 5 476 pixels par case
    # coûtait à elle seule un tiers de la mesure masquée.
    scan = warped[y0:y0 + h, x0:x0 + w]
    # M_d2s = repère LOCAL du crop du scan → repère GLOBAL de la référence
    # (scan-local +(x0,y0) → scan-global → ref-global via M⁻¹). C'est une carte
    # dst→src : warpAffine doit être en mode WARP_INVERSE_MAP.
    Mq3 = np.vstack([M, [0, 0, 1]]).astype(np.float64)
    T = np.array([[1, 0, x0], [0, 1, y0], [0, 0, 1]], np.float64)
    M_d2s = (np.linalg.inv(Mq3) @ T)[:2].astype(np.float32)
    ref_al = cv2.warpAffine(ref, M_d2s, (w, h),
                            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                            borderValue=255)
    # masque large : couvre le halo flou du cadre/lettre SCANNÉS (plus épais que le
    # rendu PDF net) — seuil haut + dilatation généreuse.
    # Dilater par un carré 3×3 n fois == dilater une fois par un carré (2n+1) :
    # un seul appel, résultat identique (vérifié).
    mask = cv2.dilate((ref_al < 200).astype(np.uint8), _DILATE_K)
    poly = (scan_corners - np.array([x0, y0], np.float32)).astype(np.int32)
    base_inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(base_inside, poly, 1)
    # seuil RELATIF au papier (p85 du crop) → robuste à l'exposition CamScanner.
    paper = max(paper_p85(scan), 1.0)
    # `np.float32` : la comparaison se fait alors dans le même type qu'avec un
    # crop float32, donc au bit près sur le même résultat.
    dark = scan < np.float32(_DARK_FRAC * paper)
    keep = mask == 0
    out: dict[int, float | None] = {}
    # Les érosions sont CHAÎNÉES : éroder de 3 puis de 2 revient à éroder de 5.
    # 7 itérations au total au lieu de 3 + 5 + 7 = 15.
    inside = base_inside
    done = 0
    for it in sorted(erosions):
        if it > done:
            inside = cv2.erode(inside, _K3, iterations=it - done)
            done = it
        bg = (inside == 1) & keep
        n_bg = int(np.count_nonzero(bg))
        # `dark[bg].mean()` recopie les points retenus ; deux comptages donnent
        # exactement le même quotient sans allouer.
        out[it] = (np.count_nonzero(dark & bg) / n_bg) if n_bg >= _MIN_BG_PX else None
    return out, M


def masked_features(warped: np.ndarray, ref: np.ndarray, box,
                    ref_corners, offset: tuple[int, int] = (0, 0)) -> dict:
    """Features masquées d'une case (cf. `MASKED_FEATURE_COLS`).

    `ref_corners` = cadre de la case détecté dans la référence (cf. `get_reference`).
    Repli si le cadre n'est pas détecté dans le scan : calage par translation seule
    (offset par-question) et `frame_detected=0` — le GBM apprend alors à se reposer
    sur les features de forme.
    """
    scan_corners, _why = detect_frame(warped, box)
    frame_detected = scan_corners is not None
    if ref_corners is None:
        ref_corners = ref_corners_global(box)
    ref_corners = np.asarray(ref_corners, dtype=np.float32)
    if not frame_detected:
        scan_corners = ref_corners + np.asarray(offset, dtype=np.float32)

    got = _masked_ratios(warped, ref, box, ref_corners, scan_corners)
    ratios, M = got if got is not None else (None, None)
    feats: dict[str, float] = {}
    for it in _EROSIONS:
        v = ratios.get(it) if ratios else None
        feats["masked_ratio_e%d" % it] = float(v) if v is not None else float("nan")
    feats["frame_detected"] = 1.0 if frame_detected else 0.0
    feats["align_residual"] = (_align_residual(ref_corners, scan_corners, M)
                               if frame_detected else float("nan"))
    return feats


# ==========================================================================
# Référence en cache
# ==========================================================================

# Une entrée par feuille de réponses : un sujet multi-feuilles en a plusieurs,
# et passer de l'une à l'autre au fil des pages scannées ne doit pas relancer
# un rendu PDF à chaque fois.
_REF_CACHE: dict = {}
_REF_MAX = 4


def get_reference(lay, page: int | None = None):
    """`(ref_img, ref_frames)` : rendu du PDF sujet + cadre détecté par case.

    `page` : la feuille de réponses concernée (défaut : la principale). Avec
    plusieurs feuilles, chacune a sa référence et ses cadres — les mélanger
    ferait chercher les cases de la feuille 2 aux positions de la feuille 1.

    `ref_frames` = `dict[(question, answer) → 4 coins | None]`. Mis en cache
    par (PDF, mtime, page) — recalculer à chaque case serait catastrophique.
    """
    if page is None:
        page = lay.answer_sheet_page
    pdf = _subject_pdf()
    key = (str(pdf), pdf.stat().st_mtime, page)
    hit = _REF_CACHE.get(key)
    if hit is None:
        ref = render_reference(lay, page)
        frames = {}
        for b in lay.sheet_boxes(page=page):
            sc, _ = detect_frame(ref, b)
            frames[(b.question, b.answer)] = sc
        if len(_REF_CACHE) >= _REF_MAX:
            _REF_CACHE.pop(next(iter(_REF_CACHE)))
        hit = _REF_CACHE[key] = (ref, frames)
    return hit


def invalidate_cache() -> None:
    """Force le re-rendu des références au prochain `get_reference()`."""
    _REF_CACHE.clear()


# ==========================================================================
# Sanity check
# ==========================================================================

def main():
    import layout_store
    lay = layout_store.get_layout()
    ref, frames = get_reference(lay)
    ok = sum(v is not None for v in frames.values())
    print(f"PDF sujet     : {_subject_pdf()}")
    print(f"Référence     : {ref.shape} | feuille page {lay.answer_sheet_page} "
          f"(feuilles de réponses : {list(lay.answer_sheet_pages)})")
    print(f"Cadres réf    : {ok}/{len(frames)} détectés")
    # mesure masquée sur quelques cases de la référence elle-même (toutes vides) :
    sample = lay.sheet_boxes(page=lay.answer_sheet_page)[:8]
    print("\nmasked_features sur la référence (cases vides → ratios attendus bas) :")
    for b in sample:
        f = masked_features(ref, ref, b, frames.get((b.question, b.answer)))
        print(f"  Q{b.question}{b.char or '?'}: "
              f"e5={f['masked_ratio_e5']:.3f} frame={f['frame_detected']:.0f} "
              f"resid={f['align_residual']:.2f}")


if __name__ == "__main__":
    main()
