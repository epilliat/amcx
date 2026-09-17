"""Vue d'ensemble : plusieurs examens d'un même dossier, et leurs notes agrégées.

Un **ensemble** est un dossier qui contient un `cohorte.json` et, à côté, les
projets AMCx des examens qu'il rassemble :

    L3-2026/
      cohorte.json
      QCM1/          ← un projet AMCx
      rattrapage/    ← un autre

C'est le niveau où « seuiller à 30 » et « ramener sur 20 » ont un sens : un
examen seul se lit sur son propre barème (cf. *Onglet Évaluation*), comparer ou
agréger demande une échelle commune.

⚠ **Un examen est lu par SOUS-PROCESSUS** (`exam_results.py --project … --json`).
`config`, `sujet_store` et `server` figent leurs chemins à l'import : un seul
process ne peut pas lire deux projets sans mélanger le sujet de l'un et les
copies de l'autre, en silence. Mesuré : ~0,5 s par examen, lus en parallèle.

⚠ **Rien n'est inclus sans le dire.** Un projet trouvé dans le dossier mais
absent de `cohorte.json` est un *candidat*, pas un membre : un dossier d'essai
n'entre pas tout seul dans la moyenne d'une promotion.

    amcx cohort --dir ~/Documents/AMCx/L3-2026
    amcx cohort --dir … --json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import exam_results                                          # noqa: E402
import grades_view as gv                                     # noqa: E402

COHORT_FILE = "cohorte.json"

DEFAULTS = {
    "name": "",
    # [{path, label, seuil, max, agg_weight}] — `path` relatif au dossier de
    # l'ensemble. `seuil`/`max` à `null` = le barème de l'examen (« auto »).
    "exams": [],
    # Mêmes entrées qu'un projet (cf. grade_imports), chemins ABSOLUS.
    "grade_files": [],
    "final_threshold": 20.0,
    "pass_mark": 10.0,
    "hist_granularity": 1.0,
}


class CohortError(Exception):
    """Ce qu'on ne peut pas lire, et pourquoi."""


# --------------------------------------------------------------------------
# Le fichier
# --------------------------------------------------------------------------

def cohort_file(d: Path) -> Path:
    return Path(d) / COHORT_FILE


def is_cohort(d: Path) -> bool:
    return cohort_file(d).is_file()


def load(d: Path) -> dict:
    """`cohorte.json` complété par les défauts. Fichier absent = ensemble vide.

    ⚠ Un fichier corrompu **lève** au lieu de repartir des défauts : repartir
    de zéro effacerait silencieusement la composition de l'ensemble et les
    réglages de note à la première écriture.
    """
    cfg = dict(DEFAULTS)
    p = cohort_file(d)
    if p.is_file():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise CohortError(f"{p} illisible : {e}") from e
    cfg.setdefault("name", Path(d).name)
    return cfg


def save(d: Path, cfg: dict) -> None:
    """Écriture atomique (tmp + replace), comme `config.write_json_atomic`."""
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    p = cohort_file(d)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------
# Les membres
# --------------------------------------------------------------------------

def discover(d: Path) -> list[Path]:
    """Sous-dossiers de `d` qui sont des projets AMCx, triés par nom."""
    out = []
    for sub in sorted(Path(d).iterdir()):
        if not sub.is_dir():
            continue
        try:
            out.append(exam_results.resolve_project(sub))
        except exam_results.ResultsError:
            continue
    return out


def candidates(d: Path, cfg: dict) -> list[dict]:
    """Projets présents dans le dossier mais **pas** dans `cohorte.json`."""
    listed = {str((Path(d) / e["path"]).resolve()) for e in cfg.get("exams", [])}
    out = []
    for p in discover(d):
        # Le membre est désigné par le dossier nommé, pas par `auto_grading/`.
        top = p.parent if p.name == "auto_grading" else p
        if str(p.resolve()) in listed or str(top.resolve()) in listed:
            continue
        out.append({"path": os.path.relpath(top, d), "label": top.name})
    return out


def _read_one(d: Path, entry: dict) -> dict:
    """Lit un examen par sous-processus. Une panne est **portée**, pas levée :
    un examen illisible ne doit pas faire disparaître les autres de la page."""
    path = (Path(d) / entry["path"])
    out = {"path": entry["path"], "label": entry.get("label") or path.name,
           "ok": False, "error": "", "bareme": 0.0, "students": [],
           "summary": {}}
    try:
        r = subprocess.run(
            [sys.executable, str(_HERE / "exam_results.py"),
             "--project", str(path), "--json"],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
        return out
    if r.returncode != 0:
        out["error"] = (r.stderr or "").strip().splitlines()[-1:] or ["échec"]
        out["error"] = out["error"][0].lstrip("✘ ")
        return out
    try:
        data = json.loads(r.stdout)
    except ValueError as e:
        out["error"] = f"sortie illisible : {e}"
        return out
    out.update(ok=True, bareme=data["bareme"], students=data["students"],
               summary=data["summary"])
    out["label"] = entry.get("label") or data["project"]
    return out


def read_members(d: Path, cfg: dict, workers: int = 4) -> list[dict]:
    """Tous les examens de l'ensemble, lus en parallèle."""
    entries = cfg.get("exams") or []
    if not entries:
        return []
    with ThreadPoolExecutor(max(1, min(workers, len(entries)))) as ex:
        return list(ex.map(lambda e: _read_one(d, e), entries))


# --------------------------------------------------------------------------
# Les colonnes de note
# --------------------------------------------------------------------------

def exam_key(entry: dict) -> str:
    return f'exam::{entry["path"]}'


def columns(cfg: dict, members: list, series: list) -> list:
    """Descripteurs de colonne, même forme que `grades_view.grade_columns`.

    ⚠ `seuil` et `max` à `null` valent le **barème de l'examen** : c'est le
    défaut demandé (« le seuil c'est le max du barème et on ramène sur le
    max »), et il rend la note de la colonne égale au score brut. Combiner deux
    examens de barèmes différents sans toucher à `max` donne donc une moyenne
    dominée par le plus gros barème — `scale_warnings()` le dit.
    """
    cols = []
    by_path = {m["path"]: m for m in members}
    for i, e in enumerate(cfg.get("exams") or []):
        m = by_path.get(e["path"]) or {}
        bareme = float(m.get("bareme") or 0.0)
        cols.append({
            "key": exam_key(e),
            "kind": "exam",
            "path": e["path"],
            "label": e.get("label") or m.get("label") or e["path"],
            "color": gv.SERIES_COLORS[i % len(gv.SERIES_COLORS)],
            "seuil": float(e.get("seuil") or bareme or 1.0),
            "max": float(e.get("max") or bareme or 1.0),
            "auto_seuil": not e.get("seuil"),
            "auto_max": not e.get("max"),
            "agg_weight": float(e.get("agg_weight", 1.0)),
            "bareme": bareme,
            "ok": bool(m.get("ok")),
            "error": m.get("error", ""),
        })
    params = {}
    for fc in cfg.get("grade_files", []):
        for gc in fc.get("grade_cols", []):
            params[(fc["path"], gc.get("idx"))] = gc
    n = len(cols)
    for i, s in enumerate(series):
        gc = params.get((s["file"], s["idx"]), {})
        cols.append({
            "key": f'{s["file"]}::{s["idx"]}',
            "kind": "file",
            "path": s["file"], "idx": s["idx"],
            "label": s["name"],
            "color": gv.SERIES_COLORS[(n + i) % len(gv.SERIES_COLORS)],
            "seuil": float(gc.get("seuil", 20.0)),
            "max": float(gc.get("max", 20.0)),
            "auto_seuil": False, "auto_max": False,
            "agg_weight": float(gc.get("agg_weight", 1.0)),
            "ok": True, "error": "",
        })
    return cols


def scale_warnings(columns_: list) -> list[str]:
    """Ce qui rendrait la moyenne trompeuse, dit avant qu'elle ne soit lue.

    ⚠ Deux colonnes ramenées sur des échelles différentes ne se moyennent pas :
    un QCM sur 33 et un projet sur 20, à poids égal, donnent un « /26,5 » que
    personne n'a demandé. C'est le défaut silencieux que le niveau examen
    évitait en n'ayant qu'une colonne.
    """
    live = [c for c in columns_ if c["ok"]]
    out = []
    maxes = {round(c["max"], 6) for c in live}
    if len(maxes) > 1:
        out.append("les colonnes ne sont pas ramenées sur la même échelle ("
                   + ", ".join(f'{c["label"]} sur {c["max"]:g}' for c in live)
                   + ") : la moyenne pondérée n'a pas d'échelle lisible")
    for c in columns_:
        if not c["ok"]:
            out.append(f'{c["label"]} : {c["error"] or "illisible"}')
    return out


# --------------------------------------------------------------------------
# La table
# --------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    from student_list import _norm
    return _norm(s or "")


def identity_map(people: dict) -> tuple[dict, list[str]]:
    """`{identifiant lu → identifiant canonique}` + les rapprochements refusés.

    ⚠ **Le même étudiant ne porte pas le même identifiant d'un examen à
    l'autre.** Mesuré sur deux vrais examens : `3017` d'un côté, `13017` de
    l'autre — la même personne, une liste portant le numéro complet et l'autre
    ses quatre derniers chiffres. **36 étudiants sur 39** apparaissaient en
    double, chacun avec la moitié de ses notes. C'est exactement ce que
    `StudentMatcher.by_id` résout déjà pour rattacher une copie à sa liste.

    On rapproche donc un identifiant d'un autre dont il est le **suffixe**, à
    deux conditions, toutes deux vérifiées :

    - le rapprochement est **unique** : si deux identifiants longs finissent
      par le même suffixe, il ne désigne personne et on ne rapproche rien —
      fondre deux étudiants est pire que d'en afficher un en double ;
    - les **noms concordent** (ou l'un des deux est inconnu) : un suffixe
      partagé par hasard entre deux promotions ne doit pas fusionner deux
      personnes.

    Ce qui est refusé est **listé**, jamais tu : c'est la seule façon de voir
    qu'une ligne en double vient d'un numéro ambigu.
    """
    ids = sorted(people)
    numeric = [i for i in ids if i.isdigit()]
    parent = {i: i for i in ids}
    refused: list[str] = []
    for short in sorted(numeric, key=len):
        longer = [l for l in numeric if len(l) > len(short) and l.endswith(short)]
        if not longer:
            continue
        if len(longer) > 1:
            refused.append(f"« {short} » peut désigner {', '.join(sorted(longer))}"
                           f" — les lignes restent séparées")
            continue
        target = longer[0]
        a, b = _norm_name(people[short]["nom_prenom"]), _norm_name(people[target]["nom_prenom"])
        if a and b and a != b:
            refused.append(f"« {short} » ({people[short]['nom_prenom']}) et "
                           f"« {target} » ({people[target]['nom_prenom']}) ont le même"
                           f" suffixe mais pas le même nom — lignes séparées")
            continue
        parent[short] = target

    def root(i):
        seen = set()
        while parent[i] != i and i not in seen:
            seen.add(i)
            i = parent[i]
        return i

    return {i: root(i) for i in ids}, refused


def build_table(members: list, columns_: list, series: list,
                final_threshold: float) -> tuple[list[dict], list[str]]:
    """Une ligne par étudiant, toutes colonnes confondues, triée par nom.

    Le rattachement se fait par **identifiant**. La population est la réunion
    de celles des examens : exiger une liste d'étudiants propre à l'ensemble
    ajouterait une étape sans rien apprendre, et un identifiant vu dans un
    examen est par construction un étudiant de l'ensemble.

    ⚠ Une copie **non reliée** (aucun identifiant) ne peut être recollée à rien
    d'un examen à l'autre : elle est comptée à part, jamais fondue dans une
    ligne au hasard.
    """
    by_key = {m["path"]: m for m in members}
    people: dict[str, dict] = {}
    raw: dict[str, dict[str, float]] = {}
    unlinked = 0
    for col in columns_:
        if col["kind"] != "exam":
            continue
        m = by_key.get(col["path"])
        if not m or not m["ok"]:
            continue
        for st in m["students"]:
            sid = st["id"]
            if not sid:
                if not st["absent"]:
                    unlinked += 1
                continue
            p = people.setdefault(sid, {"id": sid, "nom_prenom": st["nom_prenom"],
                                        "courriel": "", "absent_in": []})
            # Le nom et le courriel du premier examen qui les porte : un examen
            # sans liste d'étudiants rend « ? », il ne doit pas l'imposer.
            if p["nom_prenom"] in ("", "?") and st["nom_prenom"] not in ("", "?"):
                p["nom_prenom"] = st["nom_prenom"]
            if not p["courriel"] and st["courriel"]:
                p["courriel"] = st["courriel"]
            if st["absent"]:
                p["absent_in"].append(col["label"])
            else:
                raw.setdefault(sid, {})[col["key"]] = st["brut"]

    vals_by_key = {f'{s["file"]}::{s["idx"]}': s.get("values") or {}
                   for s in series}
    for col in columns_:
        if col["kind"] != "file":
            continue
        for sid, v in vals_by_key.get(col["key"], {}).items():
            people.setdefault(sid, {"id": sid, "nom_prenom": sid,
                                    "courriel": "", "absent_in": []})
            raw.setdefault(sid, {})[col["key"]] = v

    # Replie les identifiants d'un même étudiant (cf. `identity_map`) AVANT de
    # construire les lignes : sinon il apparaît deux fois, avec la moitié de
    # ses notes chacune, et sa note finale est fausse des deux côtés.
    canon, refused = identity_map(people)
    folded: dict[str, dict] = {}
    folded_raw: dict[str, dict] = {}
    for sid, p in people.items():
        c = canon[sid]
        tgt = folded.setdefault(c, {"id": c, "nom_prenom": "", "courriel": "",
                                    "absent_in": [], "also_id": []})
        if tgt["nom_prenom"] in ("", "?") and p["nom_prenom"] not in ("", "?"):
            tgt["nom_prenom"] = p["nom_prenom"]
        if not tgt["courriel"] and p["courriel"]:
            tgt["courriel"] = p["courriel"]
        tgt["absent_in"] += [x for x in p["absent_in"] if x not in tgt["absent_in"]]
        if sid != c:
            tgt["also_id"].append(sid)
        folded_raw.setdefault(c, {}).update(raw.get(sid, {}))
    people, raw = folded, folded_raw

    rows = []
    for sid, p in people.items():
        notes, pairs = {}, []
        for col in columns_:
            v = raw.get(sid, {}).get(col["key"])
            if v is None:
                notes[col["key"]] = None
                continue
            note = gv.rescale(v, col["seuil"], col["max"])
            notes[col["key"]] = round(note, 3)
            pairs.append((col, note))
        rows.append({**p,
                     "raw": {k: round(v, 2) for k, v in raw.get(sid, {}).items()},
                     "notes": notes,
                     "n_columns": len(pairs),
                     "final": round(gv.weighted_final(pairs, final_threshold), 2)
                     if pairs else None})
    rows.sort(key=lambda r: r["nom_prenom"])
    if unlinked:
        rows.append({"id": "", "nom_prenom": f"({unlinked} copie(s) non reliée(s))",
                     "courriel": "", "absent_in": [], "also_id": [], "raw": {},
                     "notes": {}, "n_columns": 0, "final": None,
                     "unlinked": unlinked})
    return rows, refused


def calibration_series(rows: list, columns_: list) -> list:
    """Une série par colonne, pour l'histogramme de calibration.

    Ce sont les notes **de colonne** (déjà plafonnées à leur `max`), pas les
    scores bruts : superposer des bruts d'échelles différentes ne compare rien.
    """
    out = []
    for c in columns_:
        vals = [r["notes"][c["key"]] for r in rows
                if r["notes"].get(c["key"]) is not None]
        out.append({"name": c["label"], "color": c["color"], "values": vals,
                    "opacity": 0.45})
    return out


def scatter_data(rows: list, columns_: list) -> dict:
    """Points du nuage : une note par colonne + la note finale, par étudiant."""
    variables = [{"id": c["key"], "label": c["label"]} for c in columns_]
    variables.append({"id": "__final__", "label": "Note finale"})
    points = []
    for r in rows:
        if not r["id"]:
            continue
        v = {c["key"]: r["notes"].get(c["key"]) for c in columns_}
        v["__final__"] = r["final"]
        points.append({"name": r["nom_prenom"], "v": v})
    return {"variables": variables, "points": points}


def export_rows(rows: list, columns_: list) -> tuple[list[str], list[list]]:
    """En-tête + lignes du CSV d'ensemble — export ET compte rendu.

    ⚠ Une colonne vide et un `ABS` ne disent pas la même chose : `ABS` = cet
    étudiant était **attendu** à cet examen et n'a pas composé ; vide = cet
    examen ne le concernait pas (il n'est pas dans sa liste). Les confondre
    ferait passer une promotion entière pour absente à l'examen de l'autre
    demi-journée.
    """
    # ⚠ `id_canonique`, `nom_prenom`, `courriel` et `note_finale` portent les
    # noms qu'attend `mail_results.load_recipients` : ce fichier est donc
    # directement envoyable, sans second format à maintenir.
    header = ["id_canonique", "nom_prenom", "courriel"]
    for c in columns_:
        header += [f'{c["label"]}_brut', c["label"]]
    header += ["note_finale", "absent_de"]
    out = []
    for r in rows:
        if not r["id"]:
            continue
        line = [r["id"], r["nom_prenom"], r["courriel"]]
        for c in columns_:
            note = r["notes"].get(c["key"])
            if note is None:
                mark = exam_results.ABSENT_MARK if c["label"] in r["absent_in"] else ""
                line += [mark, mark]
            else:
                line += [r["raw"].get(c["key"], ""), note]
        line += ["" if r["final"] is None else r["final"],
                 ";".join(r["absent_in"])]
        out.append(line)
    return header, out


def summary(rows: list, columns_: list) -> dict:
    finals = [r["final"] for r in rows if r.get("final") is not None]
    return {
        "n_students": sum(1 for r in rows if r["id"]),
        "n_columns": len(columns_),
        "n_complete": sum(1 for r in rows if r["id"]
                          and r["n_columns"] == len(columns_)),
        "n_partial": sum(1 for r in rows if r["id"]
                         and 0 < r["n_columns"] < len(columns_)),
        "n_unlinked": sum(r.get("unlinked", 0) for r in rows),
        "stats": gv.series_stats(finals),
    }


def report(d: Path, cfg: dict | None = None) -> dict:
    """Tout ce qu'une page ou la ligne de commande veut : membres, colonnes,
    table, avertissements. Une seule construction."""
    d = Path(d)
    cfg = cfg or load(d)
    members = read_members(d, cfg)
    series = _series(cfg, rows_roster(members))
    cols = columns(cfg, members, series)
    thr = float(cfg.get("final_threshold", 20.0))
    rows, refused = build_table(members, cols, series, thr)
    return {"name": cfg.get("name") or d.name, "path": str(d),
            "config": cfg, "members": members, "columns": cols,
            "warnings": scale_warnings(cols) + refused, "students": rows,
            "candidates": candidates(d, cfg),
            "summary": summary(rows, cols)}


def rows_roster(members: list) -> list:
    """Les étudiants connus des examens, pour rattacher un fichier de notes.

    ⚠ C'est une liste d'`student_list.Student` bâtie à la volée : un ensemble
    n'a pas de liste à lui, et en réclamer une n'apprendrait rien de plus.
    """
    from student_list import Student
    seen: dict[str, Student] = {}
    for m in members:
        if not m.get("ok"):
            continue
        for st in m["students"]:
            if st["id"] and st["id"] not in seen:
                nom, _, prenom = (st["nom_prenom"] or "").partition(" ")
                seen[st["id"]] = Student(id=st["id"], nom=nom, prenom=prenom,
                                         email=st["courriel"] or "")
    return list(seen.values())


def _series(cfg: dict, roster: list) -> list:
    """Séries des fichiers de notes importés, rattachées au roster de l'ensemble."""
    if not cfg.get("grade_files"):
        return []
    from grade_imports import build_all_series
    from student_list import StudentMatcher
    return build_all_series(cfg, StudentMatcher(roster))


# --------------------------------------------------------------------------
# Ligne de commande
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, help="dossier de l'ensemble")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = Path(a.dir).expanduser().resolve()
    if not d.is_dir():
        print(f"✘ {d} n'existe pas", file=sys.stderr)
        return 2
    try:
        rep = report(d)
    except CohortError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    s = rep["summary"]
    print(f'{rep["name"]}  —  {len(rep["columns"])} colonne(s), '
          f'{s["n_students"]} étudiant(s)')
    for c in rep["columns"]:
        state = "" if c["ok"] else f'  ✘ {c["error"]}'
        print(f'  · {c["label"]:<20} seuil {c["seuil"]:>6g} → sur {c["max"]:>5g}'
              f'  poids {c["agg_weight"]:g}{state}')
    for w in rep["warnings"]:
        print(f"  ⚠ {w}")
    if not rep["columns"]:
        print("  (aucun examen dans cohorte.json)")
    for c in rep["candidates"]:
        print(f'  + candidat non inclus : {c["label"]}  ({c["path"]})')
    st = s["stats"]
    if st["n"]:
        print(f'  note finale : μ {st["mean"]:.2f} · σ {st["std"]:.2f} · '
              f'médiane {st["median"]:.1f} · {st["min"]:.1f}–{st["max"]:.1f}')
    print(f'  {s["n_complete"]} complet(s) · {s["n_partial"]} partiel(s) · '
          f'{s["n_unlinked"]} copie(s) non reliée(s)')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
