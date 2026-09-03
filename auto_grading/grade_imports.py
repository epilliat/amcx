"""Import de fichiers de notes externes (csv / xlsx) pour le dashboard.

Gère des fichiers à structure quelconque : pas forcément de colonne identifiant,
en-tête éventuellement éclaté sur plusieurs lignes, données ne démarrant pas en
ligne 0. Les colonnes sont repérées par INDEX. La jointure aux étudiants se fait
soit par identifiant, soit par nom (fuzzy, avec résolution manuelle possible).

⚠️ Ce module ne lit/écrit JAMAIS raw_responses/ : il ne touche pas les reviews.
La config des fichiers vit dans config.json (clé `grade_files`).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import openpyxl

from config import load_config, project_root, resolve_path, save_config

# Calculé à l'import (donc au démarrage du process) — un changement de projet
# implique un restart Flask, donc IMPORTS_DIR sera recalculé proprement.
IMPORTS_DIR = project_root() / "imports"

NAME_MIN = 0.70        # score fuzzy en-dessous duquel un nom est « non trouvé »
NAME_CONFIDENT = 0.90  # score fuzzy au-dessus duquel un nom est « sûr »


def ensure_imports_dir() -> Path:
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return IMPORTS_DIR


# --------------------------------------------------------------------------
# Lecture brute csv / xlsx → table de cellules (aucune hypothèse d'en-tête)
# --------------------------------------------------------------------------
def _sniff_delimiter(lines: list[str]) -> str:
    """Devine le séparateur d'un csv (';' fréquent sur les exports FR).

    ⚠ Sur PLUSIEURS lignes, pas seulement la première : un export de scolarité
    commence souvent par une ligne de titre sans aucun séparateur. En ne
    regardant qu'elle, on retombait sur la virgule et le fichier entier était lu
    comme **une seule colonne** — donc les index de colonnes choisis par
    l'utilisateur devenaient hors bornes, sans message compréhensible.

    Le bon séparateur est celui qui découpe le plus de lignes en un **même**
    nombre de champs : un séparateur qui n'en est pas produit des comptes
    erratiques.
    """
    best, best_score = ",", (0, 0)
    for d in (";", ",", "\t", "|"):
        counts = [ln.count(d) for ln in lines if ln.strip()]
        counts = [c for c in counts if c > 0]
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        score = (counts.count(modal), modal)
        if score > best_score:
            best, best_score = d, score
    return best


def _read_csv(path) -> list[list]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        content = f.read()
    head = content.split("\n")[:10]
    reader = csv.reader(io.StringIO(content), delimiter=_sniff_delimiter(head))
    return [[(c if (c is not None and c != "") else None) for c in row]
            for row in reader]


def _read_xlsx(path) -> list[list]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def read_table(path) -> list[list]:
    """Toutes les lignes d'un fichier csv/xlsx, paddées à la largeur max."""
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        rows = _read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        rows = _read_xlsx(path)
    else:
        raise ValueError(f"format non supporté: {ext}")
    ncol = max((len(r) for r in rows), default=0)
    return [list(r) + [None] * (ncol - len(r)) for r in rows]


def jsonable_cell(v):
    """Cellule sérialisable JSON (str/num/None ; le reste est stringifié)."""
    if v is None or isinstance(v, (int, float, str)) and not isinstance(v, bool):
        return v
    if isinstance(v, bool):
        return str(v)
    return str(v)


# --------------------------------------------------------------------------
# Normalisation des cellules
# --------------------------------------------------------------------------
def _coerce_id(raw) -> str:
    """Identifiant texte (mêmes règles que student_list.load_students)."""
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    if isinstance(raw, int):
        return str(raw)
    s = str(raw).strip()
    if s.endswith(".0") and s[:-2].isdigit():  # csv: "13021.0" -> "13021"
        return s[:-2]
    return s


def _coerce_float(raw) -> float | None:
    """Note flottante ; None si vide ou illisible (cellule ignorée, pas 0.0)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Analyse d'un fichier : devine colonnes, mode de jointure, début des données
# --------------------------------------------------------------------------
def _first_run(row_indices: list[int], runlen: int = 3) -> int:
    """1er index démarrant un run de `runlen` lignes consécutives présentes."""
    if not row_indices:
        return 0
    rs = sorted(set(row_indices))
    sset = set(rs)
    for start in rs:
        if all((start + k) in sset for k in range(runlen)):
            return start
    return rs[0]


def _col_label(rows: list[list], ci: int, data_start: int) -> str:
    """Étiquette d'une colonne = dernière cellule texte au-dessus des données."""
    for ri in range(min(data_start, len(rows)) - 1, -1, -1):
        v = rows[ri][ci] if ci < len(rows[ri]) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return f"Colonne {ci + 1}"


def analyze_table(rows: list[list], matcher) -> dict:
    """Devine la structure du fichier pour pré-remplir le formulaire d'import."""
    ncol = max((len(r) for r in rows), default=0)
    if ncol == 0:
        return {"ncol": 0, "nrow": 0, "columns": [],
                "suggested": {"join_mode": "name", "join_col": 0,
                              "data_start": 0, "grade_cols": []}}
    full_ids = {s.id for s in matcher.students}
    # Suffixes de 4 chiffres : un fichier de notes de scolarité ne porte pas
    # forcément l'identifiant complet. Le rattachement réel passe par
    # `matcher.by_id`, qui accepte n'importe quelle largeur — ceci ne sert
    # qu'à repérer la colonne de jointure.
    last4_ids = {s.id[-4:] for s in matcher.students if len(s.id) >= 4}

    # passe 1 : par colonne, repérer matches nom / id (numérique = _coerce_float ok,
    # car les cellules csv arrivent en texte « 12,5 »)
    name_hit, id_hit = [0] * ncol, [0] * ncol
    name_rows = [[] for _ in range(ncol)]
    id_rows = [[] for _ in range(ncol)]
    numlike, nonnull = [0] * ncol, [0] * ncol
    for ri, r in enumerate(rows):
        for ci in range(ncol):
            v = r[ci]
            if v is None or v == "":
                continue
            nonnull[ci] += 1
            if _coerce_float(v) is not None:
                numlike[ci] += 1
            cid = _coerce_id(v)
            if cid and (cid in full_ids or (len(cid) == 4 and cid in last4_ids)):
                id_hit[ci] += 1
                id_rows[ci].append(ri)
    # match nom seulement sur colonnes peu numériques (perf)
    for ci in range(ncol):
        if nonnull[ci] < 3 or numlike[ci] > nonnull[ci] * 0.5:
            continue
        for ri, r in enumerate(rows):
            v = r[ci]
            if isinstance(v, str) and v.strip():
                s, _ = matcher.by_name(v, cutoff=NAME_MIN)
                if s is not None:
                    name_hit[ci] += 1
                    name_rows[ci].append(ri)

    best_name = max(range(ncol), key=lambda c: name_hit[c])
    best_id = max(range(ncol), key=lambda c: id_hit[c])
    if id_hit[best_id] >= 5 and id_hit[best_id] >= name_hit[best_name]:
        join_mode, join_col = "id", best_id
        data_start = _first_run(id_rows[best_id])
    else:
        join_mode, join_col = "name", best_name
        data_start = _first_run(name_rows[best_name])

    # passe 2 : stats par colonne sur la région de données
    region = rows[data_start:]
    columns, grade_cols = [], []
    for ci in range(ncol):
        nn = num = 0
        sample = []
        for r in region:
            v = r[ci]
            if v is None or v == "":
                continue
            nn += 1
            if _coerce_float(v) is not None:
                num += 1
            if len(sample) < 3:
                sample.append(jsonable_cell(v))
        num_ratio = num / nn if nn else 0.0
        looks_grade = ci != join_col and nn >= 3 and num_ratio >= 0.6
        if looks_grade:
            grade_cols.append(ci)
        columns.append({
            "idx": ci,
            "label": _col_label(rows, ci, data_start),
            "numeric_ratio": round(num_ratio, 2),
            "nonnull": nn,
            "looks_id": id_hit[ci] >= 3,
            "looks_name": name_hit[ci] >= 3,
            "looks_grade": looks_grade,
            "sample": sample,
        })
    return {
        "ncol": ncol, "nrow": len(rows), "columns": columns,
        "suggested": {"join_mode": join_mode, "join_col": join_col,
                      "data_start": data_start, "grade_cols": grade_cols},
    }


# --------------------------------------------------------------------------
# Jointure aux étudiants
# --------------------------------------------------------------------------
def _resolve_join_cell(cell, join_mode: str, matcher, overrides: dict) -> str | None:
    """Cellule de jointure → identifiant canonique d'un étudiant, ou None."""
    if cell is None or cell == "":
        return None
    if join_mode == "id":
        cid = _coerce_id(cell)
        if not cid:
            return None
        s = matcher.by_full_id(cid)
        if s is None:
            s = matcher.by_id(cid)   # suffixe de n'importe quelle largeur
        return s.id if s is not None else None
    raw = str(cell).strip()
    if not raw:
        return None
    if raw in overrides:
        return overrides[raw] or None        # "" / null → ligne ignorée
    s, _ = matcher.by_name(raw, cutoff=NAME_MIN)
    return s.id if s is not None else None


def build_series_map(file_cfg: dict, idx: int, matcher=None) -> dict[str, float]:
    """{identifiant_canonique: note} pour une colonne de notes d'un fichier."""
    try:
        rows = read_table(resolve_path(file_cfg["path"]))
    except (OSError, ValueError):
        return {}
    join_col = file_cfg.get("join_col", 0)
    data_start = file_cfg.get("data_start", 0)
    join_mode = file_cfg.get("join_mode", "name")
    overrides = file_cfg.get("name_overrides", {})
    out: dict[str, float] = {}
    for r in rows[data_start:]:
        if idx >= len(r) or join_col >= len(r):
            continue
        val = _coerce_float(r[idx])
        if val is None:
            continue
        canon = _resolve_join_cell(r[join_col], join_mode, matcher, overrides)
        if canon:
            out[canon] = val
    return out


def build_all_series(cfg: dict | None = None, matcher=None) -> list[dict]:
    """Liste de séries importées : une entrée par colonne de note de chaque fichier.

    Chaque série : {name, weight, values:{id:note}, file, idx}.
    """
    cfg = cfg or load_config()
    series: list[dict] = []
    for fc in cfg.get("grade_files", []):
        for gc in fc.get("grade_cols", []):
            idx = gc.get("idx")
            if idx is None:
                continue
            series.append({
                "name": gc.get("label") or f"Colonne {idx + 1}",
                "values": build_series_map(fc, idx, matcher),
                "file": fc.get("path", ""),
                "idx": idx,
            })
    return series


def match_report(file_cfg: dict, matcher) -> dict:
    """Diagnostic de la colonne de jointure : reconnus / ambigus / non trouvés."""
    empty = {"n_matched": 0, "n_ambiguous": 0, "n_unmatched": 0,
             "n_ignored": 0, "problems": [], "resolved": []}
    try:
        rows = read_table(resolve_path(file_cfg["path"]))
    except (OSError, ValueError):
        return empty
    join_col = file_cfg.get("join_col", 0)
    data_start = file_cfg.get("data_start", 0)
    join_mode = file_cfg.get("join_mode", "name")
    overrides = file_cfg.get("name_overrides", {})
    n_matched = n_amb = n_unm = n_ign = 0
    problems, resolved, seen_override = [], [], set()
    for ri, r in enumerate(rows[data_start:], start=data_start):
        if join_col >= len(r):
            continue
        cell = r[join_col]
        if cell is None or str(cell).strip() == "":
            continue
        if join_mode == "id":
            if _resolve_join_cell(cell, "id", matcher, {}):
                n_matched += 1
            else:
                n_unm += 1
                problems.append({"row": ri, "raw": str(cell), "current": "",
                                 "auto": None, "candidates": []})
            continue
        raw = str(cell).strip()
        if raw in overrides:
            ov = overrides[raw]
            if ov:
                n_matched += 1
            else:
                n_ign += 1
            if raw not in seen_override:
                seen_override.add(raw)
                st = matcher.by_full_id(ov) if ov else None
                resolved.append({"raw": raw,
                                 "target": st.full if st else "(ignoré)"})
            continue
        s, sc = matcher.by_name(raw, cutoff=NAME_MIN)
        if s is not None and sc >= NAME_CONFIDENT:
            n_matched += 1
            continue
        cands = [{"id": st.id, "name": st.full, "score": round(c, 2)}
                 for st, c in matcher.candidates(raw, n=5)]
        if s is not None:                    # NAME_MIN ≤ score < NAME_CONFIDENT
            n_amb += 1
            problems.append({"row": ri, "raw": raw, "current": s.id,
                             "auto": {"id": s.id, "name": s.full,
                                      "score": round(sc, 2)},
                             "candidates": cands})
        else:
            n_unm += 1
            problems.append({"row": ri, "raw": raw, "current": "",
                             "auto": None, "candidates": cands})
    return {"n_matched": n_matched, "n_ambiguous": n_amb, "n_unmatched": n_unm,
            "n_ignored": n_ign, "problems": problems, "resolved": resolved}


# --------------------------------------------------------------------------
# Gestion de la liste `grade_files` dans config.json
# --------------------------------------------------------------------------
def list_grade_files(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    return list(cfg.get("grade_files", []))


GRADE_COL_DEFAULTS = {"seuil": 20.0, "max": 20.0, "agg_weight": 1.0}


def add_grade_file(entry: dict) -> dict:
    """Ajoute (ou remplace, si même `path`) un fichier dans config.json.

    Préserve, si le fichier était déjà configuré : `name_overrides` et les
    paramètres par colonne (`seuil`/`max`/`agg_weight`, repérés par `idx`).
    Les colonnes neuves reçoivent `GRADE_COL_DEFAULTS`.
    """
    cfg = load_config()
    files, old = [], None
    for f in cfg.get("grade_files", []):
        if f.get("path") == entry["path"]:
            old = f
        else:
            files.append(f)
    entry.setdefault("name_overrides",
                     old.get("name_overrides", {}) if old else {})
    old_cols = {gc.get("idx"): gc for gc in old.get("grade_cols", [])} if old else {}
    for gc in entry.get("grade_cols", []):
        prev = old_cols.get(gc.get("idx"), {})
        for k, dflt in GRADE_COL_DEFAULTS.items():
            if k not in gc:
                gc[k] = prev.get(k, dflt)
    files.append(entry)
    save_config({"grade_files": files})
    return entry


def remove_grade_file(path: str, unlink: bool = True) -> bool:
    """Retire un fichier de la config (et supprime le fichier sur disque)."""
    cfg = load_config()
    files = cfg.get("grade_files", [])
    kept = [f for f in files if f.get("path") != path]
    changed = len(kept) != len(files)
    if changed:
        save_config({"grade_files": kept})
        if unlink:
            try:
                resolve_path(path).unlink(missing_ok=True)
            except OSError:
                pass
    return changed


def set_name_override(path: str, raw_name: str, value) -> bool:
    """Pose/retire une résolution manuelle de nom pour un fichier.

    value : identifiant étudiant → forcer ce match ; None → ignorer la ligne ;
            "" → retirer l'override (revenir au match automatique).
    """
    cfg = load_config()
    files = cfg.get("grade_files", [])
    for fc in files:
        if fc.get("path") == path:
            ov = dict(fc.get("name_overrides", {}))
            if value == "":
                ov.pop(raw_name, None)
            else:
                ov[raw_name] = value
            fc["name_overrides"] = ov
            save_config({"grade_files": files})
            return True
    return False
