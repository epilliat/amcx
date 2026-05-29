"""Découpe les PDF des copies scannées en JPEG (1 fichier / page).

Les PDF sont cherchés dans le dossier de l'examen (`config.amc_dir`) : soit la
liste explicite `config.scan_pdfs`, soit auto-découverte de tous les `*.pdf`
(hors PDF compilés du sujet : exam.pdf, DOC-*.pdf, amc-compiled*.pdf…).

Chaque PDF `<nom>.pdf` est extrait dans `pages/<nom>/page_NNN.jpg`.

Rasterisation via PyMuPDF (wheels pures, pas de dépendance système poppler),
encodage JPEG via Pillow pour garder le contrôle quality/optimize.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

import config


def _resolve_workers(workers: int | None, n_jobs: int) -> int:
    """`workers=None` ou 0 → auto (cpu_count, cap à n_jobs). Toujours ≥ 1."""
    if not workers:
        workers = os.cpu_count() or 1
    return max(1, min(workers, max(1, n_jobs)))

ROOT = Path(__file__).resolve().parent
PAGES_DIR = ROOT / "pages"

# PDF compilés du sujet (à ne PAS confondre avec des copies scannées).
# Couvre : exam.pdf, DOC-*, amc-compiled*, et tout ce qui contient
# `-corrige`/`_corrige`, `-solution`/`_solution`, `-sujet`/`_sujet`,
# `-calage`/`_calage` quelque part dans le nom (préfixe, suffixe ou milieu).
_ARTIFACT_RE = re.compile(
    r"^(?:exam|DOC-.*|amc-compiled.*"
    r"|(?:corrige|solution|sujet|calage)(?:[-_].*)?"
    r"|.*[-_](?:corrige|solution|sujet|calage)(?:[-_].*)?)$",
    re.IGNORECASE)


def discover_pdfs() -> list[Path]:
    """PDF des copies scannées à traiter (config `scan_pdfs`, sinon auto-découverte).

    Exclut les fichiers listés dans `config.scan_pdfs_excluded` (UI « Retirer »).
    """
    cfg = config.load_config()
    amc = config.amc_dir()
    excluded = set(cfg.get("scan_pdfs_excluded") or [])
    explicit = cfg.get("scan_pdfs") or []
    if explicit:
        out = []
        for p in explicit:
            pp = Path(p)
            if pp.name in excluded:
                continue
            out.append(pp if pp.is_absolute() else (amc / pp))
        return out
    if not amc.is_dir():
        return []
    return [p for p in sorted(amc.glob("*.pdf"))
            if not _ARTIFACT_RE.match(p.stem) and p.name not in excluded]


def extract(pdf_path: Path, dpi: int = 300, quality: int = 85,
            first: int | None = None, last: int | None = None) -> list[Path]:
    src = Path(pdf_path)
    if not src.exists():
        raise FileNotFoundError(src)
    out_dir = PAGES_DIR / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{src.stem}] conversion {src} -> {out_dir} (dpi={dpi})...", flush=True)
    doc = fitz.open(str(src))
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)  # PDF base = 72 dpi
    lo = (first - 1) if first else 0
    hi = last if last else doc.page_count
    paths = []
    for i in range(lo, hi):
        pix = doc[i].get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        p = out_dir / f"page_{i + 1:03d}.jpg"
        img.save(p, "JPEG", quality=quality, optimize=True)
        paths.append(p)
    doc.close()
    print(f"[{src.stem}] {len(paths)} pages écrites.", flush=True)
    return paths


def _extract_worker(pdf_str: str, dpi: int, quality: int,
                    first: int | None, last: int | None) -> int:
    """Worker pour ProcessPoolExecutor (module-level → picklable)."""
    return len(extract(Path(pdf_str), dpi=dpi, quality=quality, first=first, last=last))


def extract_many(pdfs, dpi: int = 300, quality: int = 85,
                 first: int | None = None, last: int | None = None,
                 workers: int | None = None, on_progress=None) -> dict:
    """Extrait plusieurs PDF (en parallèle si workers>1, sinon série).

    `workers=None`/0 → auto (cpu_count, cap à len(pdfs)). `workers=1` → boucle
    série (zéro overhead Pool, utile mono-coeur ou debug). `on_progress(done,
    total, pdf_name)` appelé après chaque PDF terminé.

    Retourne `{str(pdf): n_pages_or_exception}`.
    """
    pdfs = list(pdfs)
    w = _resolve_workers(workers, len(pdfs))
    results: dict = {}
    if w == 1:
        for i, pdf in enumerate(pdfs):
            try:
                results[str(pdf)] = len(extract(pdf, dpi=dpi, quality=quality,
                                                first=first, last=last))
            except Exception as e:
                results[str(pdf)] = e
            if on_progress:
                on_progress(i + 1, len(pdfs), str(pdf))
        return results
    from concurrent.futures import ProcessPoolExecutor, as_completed
    import multiprocessing as _mp
    # `spawn` : repart d'un interpréteur propre. PyMuPDF/Pillow loadés dans le
    # parent peuvent garder des locks internes que `fork` hériterait → deadlock.
    ctx = _mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=w, mp_context=ctx) as ex:
        futures = {ex.submit(_extract_worker, str(pdf), dpi, quality, first, last): pdf
                   for pdf in pdfs}
        done = 0
        for fut in as_completed(futures):
            pdf = futures[fut]
            try:
                results[str(pdf)] = fut.result()
            except Exception as e:
                results[str(pdf)] = e
            done += 1
            if on_progress:
                on_progress(done, len(pdfs), str(pdf))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", nargs="+", default=None,
                    help="PDF explicites à extraire (défaut : auto-découverte dans amc_dir)")
    ap.add_argument("--dpi", type=int, default=300,
                    help="300 = résolution canonique du pipeline")
    ap.add_argument("--first", type=int, default=None, help="numéro de première page (1-indexé)")
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument("--workers", type=int, default=0,
                    help="0 = auto (cpu_count). 1 = série (debug / mono-coeur).")
    args = ap.parse_args()

    pdfs = [Path(p) for p in args.pdfs] if args.pdfs else discover_pdfs()
    if not pdfs:
        print("Aucun PDF de copies à traiter (vérifie config amc_dir / scan_pdfs, "
              "ou passe --pdfs).", file=sys.stderr)
        sys.exit(1)

    def _progress(done, total, pdf_name):
        print(f"  [{done}/{total}] {Path(pdf_name).name}", flush=True)

    results = extract_many(pdfs, dpi=args.dpi, first=args.first, last=args.last,
                           workers=args.workers, on_progress=_progress)
    total = 0
    for pdf_str, r in results.items():
        if isinstance(r, Exception):
            print(f"  -> {Path(pdf_str).name}: {r}", file=sys.stderr)
        else:
            total += r
    print(f"Total: {total} pages.")


if __name__ == "__main__":
    main()
