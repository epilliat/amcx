"""Recalcule students.csv depuis raw_responses/ (mode --cache-only).

Idempotent: si raw_responses/<batch>/<page>.json existe déjà, on skippe l'appel API
et on relit le JSON. Permet itération rapide.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from score import question_set, score_copy
from sujet_store import effective_spec
from student_list import StudentMatcher
import config
# `grade_image` (voie Claude-vision) est importé paresseusement dans process() :
# il tire anthropic + dotenv, inutiles en mode --cache-only.

ROOT = config.project_root()  # projet actif : pour les données
PAGES_DIR = ROOT / "pages"
RAW_DIR = ROOT / "raw_responses"
RESULTS_DIR = ROOT / "results"
CSV_PATH = RESULTS_DIR / "students.csv"


def list_pages(batches: list[str] | None,
               page_filter: list[int] | None = None) -> list[tuple[str, Path]]:
    if not batches:  # auto-découverte : tous les sous-dossiers de pages/
        batches = (sorted(d.name for d in PAGES_DIR.iterdir() if d.is_dir())
                   if PAGES_DIR.exists() else [])
    items = []
    for batch in batches:
        bdir = PAGES_DIR / batch
        if not bdir.exists():
            print(f"[skip] {bdir} (pas extrait)", file=sys.stderr)
            continue
        for p in sorted(bdir.glob("page_*.jpg")):
            n = int(p.stem.split("_")[1])
            if page_filter is not None and n not in page_filter:
                continue
            items.append((batch, p))
    return items


def csv_header() -> list[str]:
    cols = ["batch", "page",
            "id_canonical", "nom_canonical", "prenom_canonical", "match_method", "match_flag",
            "id_lu", "nom_lu"]
    for q in question_set():
        cols += [f"Q{q}_selected", f"Q{q}_correct", f"Q{q}_score"]
    cols += ["total_qcm", "model_notes", "warnings"]
    return cols


def csv_row(batch: str, page_num: int, data: dict, scores: dict, match: dict) -> list:
    s = match["matched"]
    row = [batch, page_num,
           s.id if s else "",
           s.nom if s else "",
           s.prenom if s else "",
           match["method"], match["flag"],
           data["student_id"], data["student_name"]]
    copy = int(data.get("_copy_id", 1) or 1)
    answers = data.get("answers", {}) or {}
    for q in question_set():
        # Les clés JSON sont des chaînes ; accepter les deux formes.
        sel = "".join(answers.get(str(q), answers.get(q, [])))
        cor = effective_spec(q, copy=copy)["correct"]
        row += [sel, cor, scores["per_question"].get(q, "")]
    row += [scores["total"], data.get("notes", ""), " | ".join(data.get("warnings", []))]
    return row


def process(batch: str, image_path: Path, force: bool = False, cache_only: bool = False) -> dict | None:
    """Si cache_only=True et pas de JSON existant: retourne None (skip).
    Sinon comportement standard (lit cache OU appelle l'API)."""
    page_num = int(image_path.stem.split("_")[1])
    raw_path = RAW_DIR / batch / f"page_{page_num:03d}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    if raw_path.exists() and not force:
        with open(raw_path) as f:
            data = json.load(f)
        # rebuild answers keys as int (perdues au JSON dump)
        data["answers"] = {int(k): v for k, v in data["answers"].items()}
        cached = True
    elif cache_only:
        return None
    else:
        # Voie Claude-vision : déplacée dans archive/ (câblée en dur sur
        # EXAM_2026). `--cache-only` n'en a pas besoin.
        try:
            from grader import grade_image  # import paresseux → anthropic seulement si besoin
        except ImportError as e:
            raise SystemExit(
                "La voie Claude-vision est archivée (auto_grading/archive/grader.py) "
                "et n'est plus branchée sur le sujet du projet. Utilise le pipeline "
                "OpenCV : cv_grade.py --all puis batch_run.py --cache-only."
            ) from e
        data = grade_image(image_path)
        # sérialiser
        to_save = dict(data)
        to_save["answers"] = {str(k): v for k, v in data["answers"].items()}
        with open(raw_path, "w") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        cached = False

    scores = score_copy(data["answers"])
    return {"data": data, "scores": scores, "cached": cached, "page_num": page_num}


def parse_pages_filter(s: str | None) -> list[int] | None:
    if not s:
        return None
    out = set()
    for chunk in s.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(chunk))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", nargs="+", default=None,
                    help="sous-dossiers de pages/ à traiter (défaut : tous)")
    ap.add_argument("--pages", default=None,
                    help="filtre pages, ex: '1-3,7,12' (s'applique à chaque batch)")
    ap.add_argument("--force", action="store_true", help="ignorer le cache raw_responses")
    ap.add_argument("--cache-only", action="store_true",
                    help="ne traiter que les pages avec un JSON déjà présent (pas d'appel API)")
    ap.add_argument("--csv", default=str(CSV_PATH))
    args = ap.parse_args()

    page_filter = parse_pages_filter(args.pages)
    items = list_pages(args.batches, page_filter)
    if not items:
        print("Aucune page à traiter.", file=sys.stderr)
        sys.exit(1)

    print(f"À traiter: {len(items)} pages.", flush=True)
    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    matcher = StudentMatcher()
    t0 = time.time()
    total_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}

    rows = []
    n_skipped_no_cache = 0
    for i, (batch, path) in enumerate(items, 1):
        try:
            r = process(batch, path, force=args.force, cache_only=args.cache_only)
            if r is None:
                n_skipped_no_cache += 1
                continue
            match = matcher.resolve(r["data"]["student_id"], r["data"]["student_name"])
            row = csv_row(batch, r["page_num"], r["data"], r["scores"], match)
            rows.append(row)
            usage = r["data"].get("usage") or {}
            total_usage["input"] += usage.get("input_tokens", 0)
            total_usage["output"] += usage.get("output_tokens", 0)
            total_usage["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            total_usage["cache_create"] += usage.get("cache_creation_input_tokens", 0) or 0
            elapsed = time.time() - t0
            eta = elapsed / i * (len(items) - i)
            tag = "cache" if r["cached"] else "API"
            who = (f"{match['matched'].nom} {match['matched'].prenom}"
                   if match["matched"] else f"<NO MATCH {r['data']['student_id']!r}>")
            print(f"  [{i:3d}/{len(items)}] {batch}/page_{r['page_num']:03d} "
                  f"[{tag}] {who[:30]:30s} "
                  f"total={r['scores']['total']:.2f}  "
                  f"({elapsed:.0f}s, ETA {eta:.0f}s)",
                  flush=True)
        except Exception as e:  # noqa
            print(f"  [{i:3d}/{len(items)}] {batch}/page_{path.stem} ERREUR: {e}", flush=True)
            err_row = [batch, int(path.stem.split("_")[1]), "", "", "", "error", str(e), "", ""] \
                + [""] * (3 * len(question_set())) + ["", "", str(e)]
            rows.append(err_row)

    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(csv_header())
        w.writerows(rows)

    print(f"\nÉcrit: {args.csv} ({len(rows)} lignes)")
    if args.cache_only:
        print(f"Pages skippées (pas de cache): {n_skipped_no_cache}")
    else:
        print(f"Tokens: input={total_usage['input']}  output={total_usage['output']}  "
              f"cache_read={total_usage['cache_read']}  cache_create={total_usage['cache_create']}")


if __name__ == "__main__":
    main()
