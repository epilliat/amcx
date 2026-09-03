"""Seed auto_grading/raw_responses/ à partir des lectures CV (et d'AMC si dispo).

Modèle de données par JSON :
  - answers          = lecture CV (source de vérité primaire, éditable)
  - _cv_answers      = lecture CV originale, immuable (référence)
  - _amc_answers     = lecture AMC ground truth — **uniquement si capture.sqlite existe**
  - _amc_copy        = ID AMC de la copie, si AMC
  - _amc_validated_cells = nb de cases avec manual ∈ {0,1} (validation explicite)
  - _cv_amc_diff     = liste {q, char, cv, amc} des cases qui divergent (si AMC)
  - _source          = "cv"
  - _flags           = ["cv_differs_amc(N)", "amc_unvalidated", "ambiguous",
                        "id_incomplet", "no_mires", "manually_edited", "validated",
                        "id_corrige", "open_answer_edited"]
  - _student_override / _cv_student_id = identité relue à la main (préservées)

Sans `capture.sqlite` (examen non analysé par AMC) → mode **CV-seul** : les
champs/flags `_amc_*` sont simplement omis.

Usage:
  python seed_raw_responses.py [--preserve-manual]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # install : pour importer config/layout_store
import config
import layout_store

ROOT = config.project_root()  # projet actif : pour les données (pages, raw_responses, …)
RAW_DIR = ROOT / "raw_responses"
CV_DIR = ROOT / "raw_responses_cv"

AMC_SEUIL = 0.5  # seuil de noirceur AMC (options.xml <seuil>)

# Flags qui marquent du travail de relecture utilisateur dans raw_responses/.
# Toute copie qui en porte un est préservée par --preserve-manual : c'est la
# source de vérité de l'utilisateur, jamais écrasée par un re-grading (piège #3
# du CLAUDE.md). `id_corrige` / `open_answer_edited` manquaient → une copie
# seulement ré-identifiée (drag&drop dans /identites) était réécrite.
USER_FLAGS = frozenset({"manually_edited", "validated", "id_corrige",
                        "open_answer_edited"})


def src_to_batch_page(src: str) -> tuple[str, int] | None:
    m = re.search(r"(\d+)_.*pdf-page-(\d+)-", src)
    if not m:
        return None
    return f"batch{m.group(1)}", int(m.group(2))


def layout_maps() -> tuple[dict, list, dict, int]:
    """(idx_to_char, qcm_questions, options_par_question, page_feuille) du calage."""
    lay = layout_store.get_layout()
    idx_to_char, by_q = {}, {}
    for b in lay.sheet_boxes():
        idx_to_char[(b.question, b.answer)] = b.char
        by_q.setdefault(b.question, []).append(b.char)
    qcm, options = [], {}
    for q, chs in sorted(by_q.items()):
        nonempty = [c for c in chs if c]
        if nonempty and all(c.isdigit() for c in nonempty):
            continue  # colonne du code étudiant
        qcm.append(q)
        options[q] = "".join(sorted(nonempty))
    return idx_to_char, qcm, options, lay.answer_sheet_page


def load_amc_data(idx_to_char: dict, qcm: list, sheet_page: int) -> dict:
    """{copy: {batch, page, student_id, answers, n_manual}} depuis capture.sqlite.

    Retourne {} si l'examen n'a pas été analysé par AMC (pas de capture.sqlite)."""
    data_dir = config.amc_data_dir()
    cap_path = data_dir / "capture.sqlite"
    if not cap_path.exists():
        return {}
    qcm_set = set(qcm)
    c_cap = sqlite3.connect("file:%s?mode=ro" % cap_path, uri=True)
    sco_path = data_dir / "scoring.sqlite"
    c_sco = (sqlite3.connect("file:%s?mode=ro" % sco_path, uri=True)
             if sco_path.exists() else None)

    out = {}
    for copy, src in c_cap.execute(
            "SELECT copy, src FROM capture_page WHERE page=?", (sheet_page,)):
        bp = src_to_batch_page(src)
        if not bp:
            continue
        student_id = "????"
        if c_sco is not None:
            row = c_sco.execute(
                "SELECT value FROM scoring_code WHERE copy=? AND code='etu'",
                (copy,)).fetchone()
            if row:
                student_id = row[0]

        answers = {q: [] for q in qcm}
        n_manual = 0
        for q, a, total, black, manual in c_cap.execute(
                "SELECT id_a, id_b, total, black, manual "
                "FROM capture_zone WHERE student=1 AND copy=? AND type=4", (copy,)):
            if q not in qcm_set or (q, a) not in idx_to_char:
                continue
            ticked = False
            if manual == 1:
                ticked = True
                n_manual += 1
            elif manual == 0:
                ticked = False
                n_manual += 1
            elif manual == -1 and total:
                ticked = (black / total) >= AMC_SEUIL
            if ticked:
                answers[q].append(idx_to_char[(q, a)])
        for q in answers:
            answers[q] = sorted(answers[q])
        out[copy] = {"batch": bp[0], "page": bp[1], "student_id": student_id,
                     "answers": answers, "n_manual": n_manual}
    c_cap.close()
    if c_sco is not None:
        c_sco.close()
    return out


def load_cv_for_copy(batch: str, page: int) -> dict | None:
    p = CV_DIR / batch / f"page_{page:03d}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def compute_diff(cv_answers: dict, amc_answers: dict, options: dict) -> list:
    """Liste des cases où CV ≠ AMC."""
    diffs = []
    for q, opts in options.items():
        cv_sel = set(cv_answers.get(str(q), cv_answers.get(q, [])))
        amc_sel = set(amc_answers.get(q, amc_answers.get(str(q), [])))
        for ch in opts:
            cv_t, amc_t = ch in cv_sel, ch in amc_sel
            if cv_t != amc_t:
                diffs.append({"q": q, "char": ch, "cv": cv_t, "amc": amc_t})
    return diffs


def write_json(path: Path, data: dict):
    """Écriture atomique (tmp + os.replace) — cf. config.write_json_atomic."""
    config.write_json_atomic(path, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preserve-manual", action="store_true",
                    help="ne pas écraser les copies avec flag 'manually_edited'/'validated'")
    args = ap.parse_args()

    idx_to_char, qcm, options, sheet_page = layout_maps()
    amc_data = load_amc_data(idx_to_char, qcm, sheet_page)
    if amc_data:
        print(f"AMC copies disponibles: {len(amc_data)} (feuille = page {sheet_page})")
    else:
        print("Pas de capture.sqlite — mode CV-seul (aucune ground-truth AMC).")

    bp_to_amc = {(d["batch"], d["page"]): (copy, d) for copy, d in amc_data.items()}

    pages_dir = ROOT / "pages"
    all_pages = []
    if pages_dir.exists():
        for batch_dir in sorted(pages_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            for img in sorted(batch_dir.glob("page_*.jpg")):
                all_pages.append((batch_dir.name, int(img.stem.split("_")[1])))

    n_total = n_amc = n_diff = n_unvalidated = n_preserved = n_no_cv = n_not_sheet = 0

    for batch, page in all_pages:
        out_path = RAW_DIR / batch / f"page_{page:03d}.json"

        preserve_answers = False
        existing = None
        if args.preserve_manual and out_path.exists():
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
            if (USER_FLAGS & set(existing.get("_flags", []))
                    or existing.get("_student_override")
                    or existing.get("_cv_student_id")):
                preserve_answers = True
                n_preserved += 1

        cv = load_cv_for_copy(batch, page)
        if cv is None:
            n_no_cv += 1
            continue

        # Copies multi-pages : une page sans grille de cases (recto sujet, page
        # blanche…) n'est pas une copie. cv_grade la marque `_is_answer_sheet=False`.
        # On l'écarte — sauf si l'utilisateur l'a déjà relue (preserve_answers).
        # Idempotent : si une telle page avait été écrite par un seed antérieur,
        # on la supprime de raw_responses/ pour converger vers « 1 copie = 1 feuille ».
        if not preserve_answers and not cv.get("_is_answer_sheet", True):
            n_not_sheet += 1
            if out_path.exists():
                out_path.unlink()
            continue

        cv_answers = cv.get("answers", {})
        cv_answers_str = {str(k): v for k, v in cv_answers.items()}

        cv_notes = cv.get("notes", "")
        flags = []
        if "ambigu" in cv_notes:
            flags.append("ambiguous")
        student_id = cv.get("student_id", "????")
        if "?" in student_id:
            flags.append("id_incomplet")
        if "mires=FAIL" in cv_notes:
            flags.append("no_mires")

        amc_info = bp_to_amc.get((batch, page))
        amc_answers = None
        amc_copy_id = None
        n_validated = None
        diff = []
        if amc_info is not None:
            amc_copy_id, amc_d = amc_info
            amc_answers = {str(q): amc_d["answers"][q] for q in qcm}
            n_validated = amc_d["n_manual"]
            diff = compute_diff(cv_answers, amc_d["answers"], options)
            if n_validated == 0:
                flags.append("amc_unvalidated")
                n_unvalidated += 1
            if diff:
                flags.append(f"cv_differs_amc({len(diff)})")
                n_diff += 1
            n_amc += 1

        data = {
            "student_name": existing.get("student_name", "") if existing else "",
            "student_id": student_id,
            "answers": cv_answers_str,
            "notes": cv_notes,
            "_cv_answers": cv_answers_str,
            "_source": "cv",
            "_flags": flags,
        }
        if amc_answers is not None:
            data["_amc_answers"] = amc_answers
            data["_amc_copy"] = amc_copy_id
            data["_amc_validated_cells"] = n_validated
            data["_cv_amc_diff"] = diff
        # flagging levier 2 (cv_grade) — propagé tel quel de raw_responses_cv
        if cv.get("_ambiguous_cells"):
            data["_ambiguous_cells"] = cv["_ambiguous_cells"]
        # Numéro de copie (sujets randomisés) et sa provenance : le serveur
        # note avec la carte case↔lettre de CETTE copie (`copy_id_of`). Sans
        # ces clés, tout est noté avec la copie 1.
        for k in ("_copy_id", "_copy_id_source", "_page_no"):
            if k in cv:
                data[k] = cv[k]

        # Feature B (HTR) : transcription + auto-grade des questions freeform.
        # Propagé tel quel depuis raw_responses_cv (clé `open_answers`).
        if cv.get("open_answers"):
            data["open_answers"] = cv["open_answers"]
            data["_cv_open_answers"] = cv["open_answers"]  # baseline immuable

        if preserve_answers and existing is not None:
            data["answers"] = {str(k): v for k, v in existing.get("answers", {}).items()}
            data["student_name"] = existing.get("student_name", "")
            existing_user_flags = [f for f in existing.get("_flags", [])
                                   if f in USER_FLAGS]
            data["_flags"] = list(set(flags + existing_user_flags))
            # Identité relue à la main — `_student_override` (nom assigné dans
            # /identites) et le numéro corrigé chiffre par chiffre. La présence
            # de `_cv_student_id` signale que `student_id` a été édité au moins
            # une fois : la valeur utilisateur prime sur la relecture CV.
            if existing.get("_student_override"):
                data["_student_override"] = existing["_student_override"]
            if existing.get("_cv_student_id"):
                data["_cv_student_id"] = existing["_cv_student_id"]
                data["student_id"] = existing.get("student_id",
                                                  data["student_id"])
                if "?" not in data["student_id"] and "id_incomplet" in data["_flags"]:
                    data["_flags"].remove("id_incomplet")
            # Préserve aussi l'override manuel des open_answers s'il existe.
            if existing.get("open_answers"):
                data["open_answers"] = existing["open_answers"]

        write_json(out_path, data)
        n_total += 1

    print(f"\nÉcrits: {n_total}")
    print(f"  Avec AMC truth:              {n_amc}")
    print(f"  Avec diff CV/AMC > 0:        {n_diff}")
    print(f"  AMC non validé (manual=0):   {n_unvalidated}")
    print(f"  Préservés (manually_edited): {n_preserved}")
    print(f"  Sans CV (skip):              {n_no_cv}")
    print(f"  Pages sans grille (écartées):{n_not_sheet}")


if __name__ == "__main__":
    main()
