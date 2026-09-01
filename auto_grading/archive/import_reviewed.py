"""Importe les JSONs annotés depuis to_review/ vers raw_responses/.

Pour chaque to_review/<batch>_page_<NNN>.json :
- Valide le contenu (lettres dans options autorisées, ID 4 chiffres, etc.)
- Copie vers raw_responses/<batch>/page_<NNN>.json
- Génère un récap des copies importées + des éventuels warnings

À lancer après que l'utilisateur a fini de remplir to_review/.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from answer_key import ANSWER_KEY
import config

ROOT = config.project_root()  # projet actif : pour les données
TO_REVIEW_DIR = ROOT / "to_review"
RAW_DIR = ROOT / "raw_responses"


def is_empty_template(data: dict) -> bool:
    """Détecte un JSON pas encore rempli par l'utilisateur."""
    if data.get("student_id") == "????":
        # ID encore au template
        return all(not v for v in data.get("answers", {}).values()) and not data.get("student_name")
    return False


def validate(data: dict) -> tuple[dict, list[str]]:
    """Nettoie + valide. Retourne (data_clean, warnings)."""
    warnings = []
    out = {
        "student_name": str(data.get("student_name", "")).strip(),
        "student_id": str(data.get("student_id", "")).strip(),
        "answers": {},
        "notes": str(data.get("notes", "")).strip(),
    }
    # ID
    if out["student_id"] and not re.fullmatch(r"[\d?]{4}", out["student_id"]):
        warnings.append(f"student_id mal formé: {out['student_id']!r} (attendu 4 chiffres ou ?)")

    raw = data.get("answers", {})
    for q, spec in ANSWER_KEY.items():
        sel = raw.get(str(q), raw.get(q, []))
        if not isinstance(sel, list):
            warnings.append(f"Q{q} non-liste: {sel!r}")
            sel = []
        clean = []
        for letter in sel:
            L = str(letter).upper().strip()
            if L in spec["options"]:
                clean.append(L)
            else:
                warnings.append(f"Q{q} lettre invalide {letter!r} (autorisé: {spec['options']})")
        out["answers"][q] = clean
    return out, warnings


def main():
    if not TO_REVIEW_DIR.exists():
        print(f"Dossier absent: {TO_REVIEW_DIR}")
        return

    json_files = sorted(TO_REVIEW_DIR.glob("batch*_page_*.json"))
    print(f"Fichiers JSON dans to_review/: {len(json_files)}")

    imported = 0
    skipped_empty = 0
    warnings_total = 0
    for jp in json_files:
        m = re.match(r"(batch\d+)_page_(\d+)\.json", jp.name)
        if not m:
            print(f"  ⚠️  nom inattendu: {jp.name}")
            continue
        batch, page_num = m.group(1), int(m.group(2))

        with open(jp, encoding="utf-8") as f:
            data = json.load(f)

        if is_empty_template(data):
            skipped_empty += 1
            print(f"  [SKIP vide]  {jp.name}")
            continue

        clean, warnings = validate(data)
        warnings_total += len(warnings)
        if warnings:
            print(f"  [⚠️  {len(warnings)} warnings]  {jp.name}")
            for w in warnings:
                print(f"      - {w}")

        # écrire dans raw_responses/
        out_dir = RAW_DIR / batch
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"page_{page_num:03d}.json"

        # sérialiser avec clés str pour les answers
        to_save = {
            "student_name": clean["student_name"],
            "student_id": clean["student_id"],
            "answers": {str(k): v for k, v in clean["answers"].items()},
            "notes": clean["notes"],
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        imported += 1

    print()
    print(f"Importés vers raw_responses/: {imported}")
    print(f"Templates vides ignorés: {skipped_empty}")
    print(f"Warnings total: {warnings_total}")
    print()
    print("Pour générer le CSV final:")
    print(f"  cd {ROOT.parent}")
    print("  .venv/bin/python auto_grading/batch_run.py")


if __name__ == "__main__":
    main()
