"""cv_regress.py — banc de non-régression du pipeline de détection des cases.

Grade toutes les pages du projet actif et fige, dans un dossier nommé, tout ce
qui doit rester identique quand on optimise sans changer la mesure :

  - `pages.json`     : le résultat de `grade_image` par page (answers, student_id,
                       notes, cases douteuses, copy_id…) ;
  - `features.npz`   : la matrice des 23 features du GBM, case par case, avec
                       ses clés (page, question, lettre) ;
  - `timings.json`   : ms par étape et par page.

Puis `compare` confronte deux instantanés bit à bit et liste exactement ce qui
a bougé : réponses, identités, cases douteuses, features (max |Δ| par colonne).
C'est l'outil qui rend une optimisation acceptable : « JSON identiques » est
une preuve, « ça a l'air pareil » n'en est pas une.

Usage :
  python cv_regress.py snapshot <nom> [--pages N] [--pattern GLOB]
  python cv_regress.py compare <nom_ref> <nom_new>
  python cv_regress.py list

Les instantanés vivent dans `<projet>/results/cv_regress/<nom>/` (hors dépôt).
Ne touche jamais `raw_responses/`.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import config
import cv_grade
import masked_detect

STAGES = {
    cv_grade: ["detect_mires", "warp_to_canonical", "compute_per_question_offsets",
               "box_fill_ratio", "extract_features", "decode_page_code",
               "detect_copy_id"],
    masked_detect: ["masked_features", "get_reference"],
}


def regress_dir() -> Path:
    return config.project_root() / "results" / "cv_regress"


def _install_timers(acc: dict):
    """Enveloppe les fonctions de STAGES pour cumuler leur temps dans `acc`."""
    originals = []
    for mod, names in STAGES.items():
        for name in names:
            f = getattr(mod, name)
            originals.append((mod, name, f))

            def make(f, name):
                @functools.wraps(f)
                def g(*a, **k):
                    t = time.perf_counter()
                    try:
                        return f(*a, **k)
                    finally:
                        s = acc.setdefault(name, [0.0, 0])
                        s[0] += time.perf_counter() - t
                        s[1] += 1
                return g
            setattr(mod, name, make(f, name))
    return originals


def _uninstall_timers(originals):
    for mod, name, f in originals:
        setattr(mod, name, f)


def _capture_features(bundle, sink: list):
    """Intercepte `predict_proba` pour récupérer la matrice de features telle
    que `grade_image` la construit — sans modifier `cv_grade`."""
    clf = bundle["clf"]
    orig = clf.predict_proba

    def pp(X):
        sink.append(np.asarray(X, dtype=np.float64).copy())
        return orig(X)
    clf.predict_proba = pp
    return lambda: setattr(clf, "predict_proba", orig)


def _cell_keys(result: dict) -> list:
    """(question, lettre) dans l'ordre des lignes de la matrice de features :
    questions QCM croissantes, lettres triées — l'ordre de `grade_image`."""
    keys = []
    for q in sorted(result["answers"]):
        for ch in sorted(result["_cv_confidences"][q]):
            keys.append((int(q), ch))
    return keys


def snapshot(name: str, max_pages: int | None, pattern: str) -> Path:
    out = regress_dir() / name
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = config.project_root() / "pages"
    files = sorted(pages_dir.rglob(pattern))
    if max_pages:
        files = files[:max_pages]
    if not files:
        sys.exit(f"aucune page dans {pages_dir} ({pattern})")

    cv2.setNumThreads(1)          # ce que voit un worker du pool
    bundle = cv_grade.load_cell_classifier()
    feat_cols = bundle["feature_cols"] if bundle else cv_grade.FEATURE_COLS

    # échauffement : modèle + référence masquée hors chrono
    g0 = cv2.imread(str(files[0]), cv2.IMREAD_GRAYSCALE)
    cv_grade.grade_image(gray=g0, page_index=1)

    acc: dict = {}
    originals = _install_timers(acc)
    sink: list = []
    restore = _capture_features(bundle, sink) if bundle else (lambda: None)
    results: dict = {}
    feats, keys, walls = [], [], []
    try:
        for i, f in enumerate(files, 1):
            gray = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            page_id = f"{f.parent.name}/{f.stem}"
            sink.clear()
            t = time.perf_counter()
            r = cv_grade.grade_image(gray=gray, page_index=int(f.stem.split("_")[1]))
            walls.append(time.perf_counter() - t)
            r = json.loads(json.dumps(r, default=str))   # clés str, valeurs sérialisables
            results[page_id] = r
            if sink:
                X = sink[-1]
                ck = _cell_keys(r)
                if len(ck) != X.shape[0]:
                    raise RuntimeError(f"{page_id}: {len(ck)} clés pour {X.shape[0]} lignes")
                feats.append(X)
                keys.extend((page_id, q, ch) for q, ch in ck)
            if i % 20 == 0 or i == len(files):
                print(f"  {i}/{len(files)}  {1000*np.mean(walls):.0f} ms/page", flush=True)
    finally:
        restore()
        _uninstall_timers(originals)

    (out / "pages.json").write_text(json.dumps(results, ensure_ascii=False, indent=1,
                                               sort_keys=True))
    if feats:
        np.savez_compressed(out / "features.npz", X=np.vstack(feats),
                            keys=np.array(keys, dtype=object), cols=np.array(feat_cols))
    n = len(files)
    timings = {"n_pages": n, "ms_per_page": 1000 * float(np.mean(walls)),
               "ms_min": 1000 * float(min(walls)), "ms_max": 1000 * float(max(walls)),
               "stages": {k: {"ms_per_page": 1000 * v[0] / n, "calls_per_page": v[1] / n}
                          for k, v in sorted(acc.items(), key=lambda kv: -kv[1][0])}}
    (out / "timings.json").write_text(json.dumps(timings, indent=1))
    print(f"\ninstantané « {name} » : {n} pages, {timings['ms_per_page']:.0f} ms/page")
    for k, v in timings["stages"].items():
        print(f"  {k:32} {v['ms_per_page']:7.1f} ms   ×{v['calls_per_page']:.0f}")
    print(f"→ {out}")
    return out


def _load(name: str):
    d = regress_dir() / name
    if not d.is_dir():
        sys.exit(f"instantané introuvable : {d}")
    pages = json.loads((d / "pages.json").read_text())
    feats = None
    if (d / "features.npz").exists():
        z = np.load(d / "features.npz", allow_pickle=True)
        feats = (z["X"], [tuple(k) for k in z["keys"]], list(z["cols"]))
    timings = json.loads((d / "timings.json").read_text())
    return pages, feats, timings


def compare(ref: str, new: str) -> int:
    pr, fr, tr = _load(ref)
    pn, fn, tn = _load(new)
    common = sorted(set(pr) & set(pn))
    only_r, only_n = sorted(set(pr) - set(pn)), sorted(set(pn) - set(pr))
    if only_r or only_n:
        print(f"pages seulement dans {ref}: {len(only_r)} ; seulement dans {new}: {len(only_n)}")
    n_diff = 0
    fields = ["answers", "student_id", "_copy_id", "_copy_id_source", "_page_no",
              "_cv_method", "_is_answer_sheet", "_n_frame_fail", "notes",
              "_ambiguous_cells", "_cv_confidences"]
    by_field: dict = {f: [] for f in fields}
    for p in common:
        a, b = pr[p], pn[p]
        for f in fields:
            if a.get(f) != b.get(f):
                by_field[f].append(p)
    print(f"\n=== {ref} → {new} : {len(common)} pages communes ===")
    for f in fields:
        pages = by_field[f]
        if not pages:
            continue
        n_diff += len(pages)
        print(f"\n{f} : {len(pages)} page(s) diffèrent")
        for p in pages[:8]:
            a, b = pr[p].get(f), pn[p].get(f)
            if f == "answers":
                qs = [q for q in set(a) | set(b) if a.get(q) != b.get(q)]
                print(f"  {p}: " + " ".join(f"Q{q} {a.get(q)}→{b.get(q)}" for q in sorted(qs, key=int)))
            elif f == "_ambiguous_cells":
                ka = {(c['q'], c['char']) for c in a}; kb = {(c['q'], c['char']) for c in b}
                print(f"  {p}: {len(a)}→{len(b)} cases ; +{sorted(kb - ka)} −{sorted(ka - kb)}")
            elif f == "_cv_confidences":
                mx = max(abs(float(a[q][c]) - float(b[q][c]))
                         for q in a for c in a[q] if q in b and c in b[q])
                print(f"  {p}: max |Δ ratio| = {mx:.3f}")
            else:
                print(f"  {p}: {a!r} → {b!r}")
        if len(pages) > 8:
            print(f"  … {len(pages) - 8} autres")
    if fr is not None and fn is not None:
        Xr, kr, cols = fr
        Xn, kn, _ = fn
        idx_r = {k: i for i, k in enumerate(kr)}
        rows = [(idx_r[k], j) for j, k in enumerate(kn) if k in idx_r]
        if rows:
            ir, jn = zip(*rows)
            A, B = Xr[list(ir)], Xn[list(jn)]
            both_nan = np.isnan(A) & np.isnan(B)
            d = np.abs(A - B)
            d[both_nan] = 0.0
            d = np.nan_to_num(d, nan=np.inf)
            mx = d.max(axis=0)
            changed = [(cols[c], float(mx[c]), int((d[:, c] > 0).sum()))
                       for c in range(len(cols)) if mx[c] > 0]
            print(f"\nfeatures : {len(rows)} cases communes ; "
                  + ("IDENTIQUES au bit près" if not changed else f"{len(changed)} colonne(s) changent"))
            for name, m, n in changed:
                print(f"  {name:28} max |Δ| {m:10.4g}   sur {n} cases")
                n_diff += 1
    print(f"\ntemps : {tr['ms_per_page']:.0f} → {tn['ms_per_page']:.0f} ms/page "
          f"({100 * (tn['ms_per_page'] / tr['ms_per_page'] - 1):+.0f} %)")
    sr, sn = tr["stages"], tn["stages"]
    for k in sorted(set(sr) | set(sn), key=lambda k: -sr.get(k, sn.get(k))["ms_per_page"]):
        a = sr.get(k, {}).get("ms_per_page", 0.0)
        b = sn.get(k, {}).get("ms_per_page", 0.0)
        print(f"  {k:32} {a:7.1f} → {b:7.1f} ms")
    print("\n" + ("AUCUNE différence de résultat." if n_diff == 0
                  else f"{n_diff} champ(s)/colonne(s) diffèrent — à justifier un par un."))
    return 0 if n_diff == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("name")
    s.add_argument("--pages", type=int, default=None)
    s.add_argument("--pattern", default="page_*.jpg")
    c = sub.add_parser("compare"); c.add_argument("ref"); c.add_argument("new")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "snapshot":
        snapshot(a.name, a.pages, a.pattern)
    elif a.cmd == "compare":
        sys.exit(compare(a.ref, a.new))
    else:
        d = regress_dir()
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            t = json.loads((p / "timings.json").read_text())
            print(f"{p.name:24} {t['n_pages']:4d} pages  {t['ms_per_page']:.0f} ms/page")


if __name__ == "__main__":
    main()
