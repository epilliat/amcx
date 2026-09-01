"""Met à jour les JSONs dans to_review/ avec les sorties CV (raw_responses_cv/).
   - Préserve _image et _amc_status (métadonnées)
   - Remplace student_id, answers, notes
   - PROTÈGE les fichiers déjà annotés par l'utilisateur (student_name non vide,
     ou notes ne commençant pas par 'method=cv_' ni par 'FLAG_AMBIGU' de mes lectures).

Skip explicite avec --skip <name1,name2,...>.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import config

ROOT = config.project_root()  # projet actif : pour les données
TO_REVIEW = ROOT / "to_review"
CV_DIR = ROOT / "raw_responses_cv"


def looks_user_edited(data: dict) -> bool:
    """Détecte si l'utilisateur a annoté le fichier (non-template, non-pré-rempli auto)."""
    # student_name non vide ET non égal à template = signe d'édition manuelle
    if data.get("student_name", "").strip():
        return True
    notes = data.get("notes", "")
    # mes notes auto contiennent souvent "FLAG_AMBIGU" ou "VÉRIFICATION"
    auto_markers = ("FLAG_AMBIGU", "VÉRIFICATION", "method=cv_", "Calibration validée")
    if any(m in notes for m in auto_markers):
        return False
    # notes non vides et sans marqueur auto → édition manuelle
    if notes.strip():
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="liste de stems à skip (csv, ex: batch1_page_002,batch3_page_001)")
    ap.add_argument("--force", action="store_true", help="écrase même les fichiers édités utilisateur")
    args = ap.parse_args()

    skip_set = {s.strip() for s in args.skip.split(",") if s.strip()}

    n_updated = 0
    n_skipped_user = 0
    n_skipped_explicit = 0
    n_no_cv = 0
    for tr_json in sorted(TO_REVIEW.glob("batch*_page_*.json")):
        stem = tr_json.stem
        m = re.match(r"(batch\d+)_page_(\d+)\.json", tr_json.name)
        if not m:
            continue
        batch, page_num = m.group(1), int(m.group(2))

        if stem in skip_set:
            print(f"  [SKIP explicit]  {stem}")
            n_skipped_explicit += 1
            continue

        cv_json = CV_DIR / batch / f"page_{page_num:03d}.json"
        if not cv_json.exists():
            print(f"  [✘ no CV     ]  {stem}")
            n_no_cv += 1
            continue

        with open(tr_json) as f:
            existing = json.load(f)

        if not args.force and looks_user_edited(existing):
            print(f"  [SKIP user-edited]  {stem}  (student_name='{existing.get('student_name')}')")
            n_skipped_user += 1
            continue

        with open(cv_json) as f:
            cv = json.load(f)

        existing["student_id"] = cv["student_id"]
        existing["answers"] = cv["answers"]
        existing["notes"] = cv["notes"]
        # student_name reste tel quel (souvent "" pour les non-édités)

        with open(tr_json, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"  [UPDATE      ]  {stem}  id={cv['student_id']}")
        n_updated += 1

    print()
    print(f"Mis à jour:        {n_updated}")
    print(f"Skip user-edited:  {n_skipped_user}")
    print(f"Skip explicite:    {n_skipped_explicit}")
    print(f"Pas de sortie CV:  {n_no_cv}")


if __name__ == "__main__":
    main()
