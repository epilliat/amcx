"""Prépare le dossier auto_grading/to_review/ pour relecture manuelle utilisateur.

Pour chaque copie problématique (38 failed AMC + 4 vérifications) :
- Copie l'image en `to_review/<batch>_page_<NNN>.jpg`
- Crée un JSON template à côté `to_review/<batch>_page_<NNN>.json`
  - Pré-rempli avec le contenu de raw_responses/ s'il existe
  - Sinon, template vide

Originaux dans pages/ NON supprimés.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import config

try:                                  # repli : absent sur un projet non initialisé
    from answer_key import ANSWER_KEY
except Exception:
    ANSWER_KEY = {}

ROOT = config.project_root()  # projet actif : pour les données
PAGES_DIR = ROOT / "pages"
RAW_DIR = ROOT / "raw_responses"
TO_REVIEW_DIR = ROOT / "to_review"
AMC_DATA_DIR = config.amc_data_dir()

# Les 4 verifications AMC mal cadrées
VERIFICATIONS = {
    18:  "batch1/page_029.jpg",   # GOULAMALY Zoeb
    66:  "batch2/page_027.jpg",   # RIBES Clément
    108: "batch2/page_070.jpg",   # LEMATTE Corentin
    112: "batch3/page_002.jpg",   # BEBIN Violette
}


def get_failed_files() -> list[str]:
    """Retourne la liste des fichiers en échec AMC au format 'batchN/page_NNN.jpg'."""
    c = sqlite3.connect(AMC_DATA_DIR / "capture.sqlite")
    failed = c.execute("SELECT filename FROM capture_failed").fetchall()
    c.close()
    out = []
    for (fn,) in failed:
        m = re.search(r"(\d+)_.*pdf-page-(\d+)-", fn)
        if not m:
            continue
        out.append(f"batch{m.group(1)}/page_{int(m.group(2)):03d}.jpg")
    return sorted(out)


def empty_template(image_rel: str, amc_status: str) -> dict:
    return {
        "_image": image_rel,
        "_amc_status": amc_status,
        "student_name": "",
        "student_id": "????",
        "answers": {str(q): [] for q in ANSWER_KEY},
        "notes": "",
    }


def load_existing_json(batch: str, page_num: int) -> dict | None:
    p = RAW_DIR / batch / f"page_{page_num:03d}.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


def prepare_one(image_rel: str, amc_status: str) -> tuple[str, bool]:
    """Copie image + JSON template. Retourne (target_name, prefilled?)."""
    batch, fname = image_rel.split("/")
    page_num = int(fname.replace("page_", "").replace(".jpg", ""))
    target_stem = f"{batch}_page_{page_num:03d}"

    src_img = PAGES_DIR / image_rel
    if not src_img.exists():
        print(f"  ⚠️  image absente: {src_img}")
        return target_stem, False
    dst_img = TO_REVIEW_DIR / f"{target_stem}.jpg"
    shutil.copy2(src_img, dst_img)

    dst_json = TO_REVIEW_DIR / f"{target_stem}.json"
    existing = load_existing_json(batch, page_num)
    if existing:
        # injecter les métadonnées sans casser le contenu existant
        merged = empty_template(image_rel, amc_status)
        merged.update({k: v for k, v in existing.items() if k not in ("_image", "_amc_status")})
        merged["_image"] = image_rel
        merged["_amc_status"] = amc_status
        # convertir les clés answers en str (par sûreté)
        merged["answers"] = {str(k): v for k, v in existing.get("answers", {}).items()}
        prefilled = True
    else:
        merged = empty_template(image_rel, amc_status)
        prefilled = False

    with open(dst_json, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return target_stem, prefilled


def write_readme():
    nb_opts = {q: len(spec["options"]) for q, spec in ANSWER_KEY.items()}
    qtype = {q: spec["type"] for q, spec in ANSWER_KEY.items()}
    lines = [
        "# Relecture manuelle — 42 copies",
        "",
        "## Workflow",
        "",
        "Pour chaque paire `batch{N}_page_NNN.jpg / .json` :",
        "1. Ouvre l'image dans ton visualiseur préféré.",
        "2. Édite le `.json` à côté (même nom) :",
        "   - `student_name` : si l'ID lu n'est pas fiable, écris le nom manuscrit ici.",
        "   - `student_id` : 4 chiffres lus dans la grille (utilise `?` pour un chiffre illisible).",
        "   - `answers` : pour chaque question, liste des lettres cochées (cases COMPLÈTEMENT noircies). Tipp-Ex compte comme NON cochée.",
        "   - `notes` : libre, pour signaler ambiguïtés.",
        "3. Sauvegarde.",
        "",
        "Champs `_image` et `_amc_status` : métadonnées, ne pas modifier.",
        "",
        "## Lettres autorisées par question",
        "",
        "| Question | Type | Lettres |",
        "|---|---|---|",
    ]
    for q, spec in ANSWER_KEY.items():
        lettres = ",".join(spec["options"])
        t = "single" if spec["type"] == "single" else "multi"
        lines.append(f"| Q{q} | {t} | {lettres} |")
    lines += [
        "",
        "## Quand tu as fini",
        "",
        "Dis-moi (ou lance `python auto_grading/import_reviewed.py`).",
        "Les JSONs validés seront copiés vers `auto_grading/raw_responses/<batch>/page_NNN.json`,",
        "puis `python auto_grading/batch_run.py` produira `auto_grading/results/students.csv`.",
        "",
    ]
    with open(TO_REVIEW_DIR / "README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    TO_REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Failed AMC
    failed = get_failed_files()
    print(f"Failed AMC: {len(failed)} fichiers")

    # 2. Verifications
    verif_files = list(VERIFICATIONS.values())
    print(f"Vérifications AMC mal cadrées: {len(verif_files)} fichiers")

    all_files = []
    for f in failed:
        all_files.append((f, "failed"))
    for amc_copy, f in VERIFICATIONS.items():
        all_files.append((f, f"verification_AMC_copy_{amc_copy}"))

    # déduplication
    seen = set()
    uniq = []
    for f, status in all_files:
        if f in seen:
            continue
        seen.add(f)
        uniq.append((f, status))
    all_files = uniq
    print(f"Total à préparer (après déduplication): {len(all_files)}")

    n_prefilled = 0
    for image_rel, status in sorted(all_files):
        stem, prefilled = prepare_one(image_rel, status)
        tag = "[pré-rempli]" if prefilled else "[vide]"
        print(f"  → {stem}  {tag}")
        if prefilled:
            n_prefilled += 1

    write_readme()
    print(f"\nDossier prêt: {TO_REVIEW_DIR}")
    print(f"  {len(all_files)} paires image+JSON ({n_prefilled} pré-remplies)")
    print(f"  + README.md")


if __name__ == "__main__":
    main()
