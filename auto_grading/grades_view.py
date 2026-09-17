"""Notes : colonnes, rescaling, agrégation, histogrammes, nuage de points.

Logique **pure** — zéro I/O, zéro état global, aucune notion de projet actif.
Tout ce qui dépend du sujet (barème, points d'une question) entre par
paramètre : c'est ce qui permet au même code de servir *un* examen et, plus
tard, un ensemble d'examens, sans deux implémentations qui finiraient par
diverger (cf. `review_state` pour le même principe).

⚠ `seuil` est la **normalisation** (le diviseur du rescaling), pas un seuil de
réussite. La clé de stockage garde son nom historique : la renommer casserait
les `grade_files[*].grade_cols[*].seuil` déjà écrits dans les config.json.
"""

from __future__ import annotations

import math

# Palette des séries (QCM = index 0, puis colonnes importées)
SERIES_COLORS = ["#0a6ed1", "#e8820c", "#1f9d57", "#9b59b6",
                 "#d6485a", "#0a9bb5", "#c0392b", "#7f8c8d"]


def series_stats(values: list) -> dict:
    """Statistiques d'une série de valeurs : n, moyenne, variance, σ, médiane, min, max."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": 0.0, "variance": 0.0, "std": 0.0,
                "median": 0.0, "min": 0.0, "max": 0.0}
    mean = sum(vals) / n
    variance = sum((v - mean) ** 2 for v in vals) / n   # variance population (÷n)
    std = variance ** 0.5
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"n": n, "mean": mean, "variance": variance, "std": std,
            "median": median, "min": vals[0], "max": vals[-1]}


def rescale(raw: float, seuil: float, max_: float) -> float:
    """Note d'une colonne : `min(brut ∕ normalisation, 1) × max`.

    La normalisation est le score qui vaut **tout** : « seuiller à 30 » sur un
    examen qui en vaut 31 donne 20/20 à qui obtient 30. Au-delà, on ne gagne
    plus rien.

    ⚠ **Le plafond s'applique ICI, avant la moyenne** — pas seulement sur la
    note finale. C'est une promesse faite aux étudiants (« 30 points
    suffisent »), pas un crédit transférable sur une autre note : sans ce
    plafond par colonne, un excédent sur l'une compenserait un manque sur
    l'autre. Mesuré sur un QCM de 31 points seuillé à 30 et ramené sur 20, plus
    un projet à 14/20 de poids égal : **17,0** avec le plafond par colonne,
    17,33 sans.

    ⚠ **Pas de plancher** : une note brute peut être négative (`mult = Σ b/m`),
    et l'écraser à 0 ici fausserait les moyennes. Le plancher à 0 est une
    décision d'affichage, prise au moment d'annoncer la note.

    ⚠ Au niveau d'UN examen, `normalisation = max = barème` et le brut ne
    dépasse jamais le barème : la fonction s'y réduit à l'identité. Le plafond
    ne se voit qu'en agrégeant.
    """
    if not seuil:
        return 0.0
    return min(raw / seuil, 1.0) * max_


def grade_columns(cfg: dict, imported: list, bareme: float) -> list:
    """Descripteurs unifiés des colonnes de note : QCM puis colonnes importées.

    Chacun : {key, label, color, seuil, auto_seuil, max, agg_weight, path?, idx?}.
    QCM lit qcm_* ; les colonnes importées lisent grade_files[*].grade_cols[*].

    ⚠ `seuil` est la **normalisation** (le diviseur du rescaling) : c'est son
    nom dans l'interface depuis qu'il vaut le barème du sujet par défaut. La
    clé de stockage garde son nom historique — la renommer casserait les
    `grade_files[*].grade_cols[*].seuil` déjà écrits dans les config.json.
    `auto_seuil` dit que la valeur vient du barème et non d'un choix explicite ;
    seul le QCM peut l'être (une colonne importée n'a pas de barème connu).

    `bareme` est la note maximale atteignable sur une copie : c'est la
    normalisation du QCM quand `qcm_seuil` vaut `None` (= auto). Elle entre
    par paramètre pour que ce module ne dépende ni du sujet ni du projet actif.
    """
    cols = [{
        "key": "qcm", "label": "QCM", "color": SERIES_COLORS[0],
        "seuil": float(cfg.get("qcm_seuil") or bareme or 20.0),
        "auto_seuil": not cfg.get("qcm_seuil"),
        "max": float(cfg.get("qcm_max", 20.0)),
        "agg_weight": float(cfg.get("qcm_agg_weight", 1.0)),
    }]
    params = {}
    for fc in cfg.get("grade_files", []):
        for gc in fc.get("grade_cols", []):
            params[(fc["path"], gc.get("idx"))] = gc
    for i, s in enumerate(imported):
        gc = params.get((s["file"], s["idx"]), {})
        cols.append({
            "key": f'{s["file"]}::{s["idx"]}',
            "path": s["file"], "idx": s["idx"], "label": s["name"],
            "color": SERIES_COLORS[(i + 1) % len(SERIES_COLORS)],
            "seuil": float(gc.get("seuil", 20.0)),
            "auto_seuil": False,
            "max": float(gc.get("max", 20.0)),
            "agg_weight": float(gc.get("agg_weight", 1.0)),
        })
    return cols


def build_calibration_series(copies: list, columns: list, imported: list) -> list:
    """Séries RESCALÉES pour le graphe de calibration : une par colonne de note."""
    by_key = {f'{s["file"]}::{s["idx"]}': s for s in imported}
    out = []
    for col in columns:
        if col["key"] == "qcm":
            vals = [rescale(c["score"], col["seuil"], col["max"]) for c in copies]
        else:
            vmap = (by_key.get(col["key"]) or {}).get("values", {})
            vals = [rescale(vmap[c["canonical_id"]], col["seuil"], col["max"])
                    for c in copies
                    if c["canonical_id"] and c["canonical_id"] in vmap]
        out.append({"name": col["label"], "color": col["color"], "values": vals,
                    "opacity": 0.5 if col["key"] == "qcm" else 0.45})
    return out


def copy_rescaled(copy: dict, columns: list, imported: list) -> list:
    """Notes rescalées d'une copie, par colonne présente : [(col, N*), …]."""
    by_key = {f'{s["file"]}::{s["idx"]}': s for s in imported}
    cid = copy.get("canonical_id")
    out = []
    for col in columns:
        if col["key"] == "qcm":
            out.append((col, rescale(copy["score"], col["seuil"], col["max"])))
        else:
            vmap = (by_key.get(col["key"]) or {}).get("values", {})
            if cid and cid in vmap:
                out.append((col, rescale(vmap[cid], col["seuil"], col["max"])))
    return out


def weighted_final(pairs: list, final_threshold: float) -> float:
    """Moyenne pondérée de `[(colonne, note), …]`, plafonnée.

    ⚠ Une colonne **absente** pour cet étudiant ne compte ni au numérateur ni
    au dénominateur : la moyenne porte sur ce qui existe. Conséquence à
    connaître — un étudiant qui n'a passé qu'un examen sur deux obtient la note
    de celui qu'il a passé, pas une moyenne tirée vers le bas. C'est la règle
    du niveau examen depuis toujours ; à un niveau qui rassemble plusieurs
    examens elle devient visible, donc elle est **affichée** (« absent de … »)
    plutôt que subie.
    """
    num = den = 0.0
    for col, val in pairs:
        num += col["agg_weight"] * val
        den += col["agg_weight"]
    return min(num / den if den else 0.0, final_threshold)


def compute_aggregate(copy: dict, columns: list, imported: list,
                      final_threshold: float) -> float:
    """Note finale : moyenne pondérée des notes de colonne, plafonnée.

    Chaque note de colonne est déjà plafonnée à son `max` par `rescale` (cf. sa
    note) ; `final_threshold` est un second plafond, dur, sur le résultat. Une
    colonne absente pour cette copie ne compte ni au numérateur ni au
    dénominateur : la moyenne porte sur ce qui existe."""
    return weighted_final(copy_rescaled(copy, columns, imported),
                          final_threshold)


def _nice_ceil(v: float) -> float:
    """Arrondi vers le haut à une graduation lisible (1, 2, 5 × 10^k)."""
    if v <= 0:
        return 1.0
    base = 10.0 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * base * 1.0000001:
            return m * base
    return 10 * base


def slider_ranges(cfg: dict, columns: list, bareme: float,
                  question_max: float = 0.0) -> dict:
    """Bornes des curseurs de réglage, calées sur l'échelle des notes.

    ⚠ Rien n'est codé en dur : un curseur « note de 0 à 20 » n'a aucun sens sur
    un QCM qui vaut 5 points — c'est le même défaut que la normalisation figée
    à 10. Les bornes se dérivent donc du barème du sujet et de l'échelle cible.

    `bareme` = note maximale d'une copie, `question_max` = points de la question
    la mieux dotée (ce que bornent plancher/plafond par question) ; les deux
    entrent par paramètre pour garder ce module indépendant du sujet.
    """
    scale = max([c["max"] for c in columns] + [float(cfg.get("final_threshold", 20.0))])
    scale = max(scale, 1.0)
    qmax = _nice_ceil(max(question_max, 1.0))
    ref = bareme or scale

    def rng(lo, hi, step, fallback=None):
        return {"min": lo, "max": hi, "step": step,
                "fallback": lo if fallback is None else fallback}

    out = {
        # Le barème sert de libellé au mode « auto » de la normalisation.
        "bareme": bareme,
        # Largeur d'une barre : de « très fin » à « un quart de l'échelle ».
        "granularity": rng(0.1, max(0.5, _nice_ceil(scale / 4)), 0.1, 1.0),
        "pass_mark": rng(0, _nice_ceil(scale), 0.25, scale / 2),
        "final_threshold": rng(1, _nice_ceil(scale * 2), 0.5, scale),
        "question_floor": rng(-qmax, qmax, 0.25, 0),
        "question_ceiling": rng(-qmax, qmax, 0.25, qmax),
        "total_floor": rng(-_nice_ceil(ref), _nice_ceil(ref), 0.5, 0),
        # ⚠ La liste de colonnes peut être VIDE — un ensemble qu'on vient de
        # créer n'en a aucune. `max()` sur une séquence vide lève, et la page
        # d'un ensemble neuf répondait 400 : le premier écran après « créer »
        # était une erreur.
        "weight": rng(0, max(3, _nice_ceil(max([c["agg_weight"] for c in columns]
                                               + [1.0]))), 0.1, 1),
    }
    # Normalisation et échelle cible : une paire de bornes PAR colonne, la
    # référence d'une colonne importée (barème inconnu) étant sa propre valeur.
    out["cols"] = {}
    for c in columns:
        base = ref if c["key"] == "qcm" else c["seuil"]
        hi = _nice_ceil(max(base, c["seuil"]) * 2)
        out["cols"][c["key"]] = {
            "seuil": rng(0.5, hi, 0.25, base or 1),
            "max": rng(0.5, _nice_ceil(max(c["max"], 20.0) * 2), 0.5, 20),
        }
    return out


def build_formula(columns: list, final_threshold: float) -> dict:
    """Structure de la formule de la note finale (affichée pour les étudiants)."""
    terms = [{"label": c["label"], "weight": c["agg_weight"],
              "seuil": c["seuil"], "max": c["max"]} for c in columns]
    return {"terms": terms, "denom": sum(c["agg_weight"] for c in columns),
            "threshold": final_threshold}


def build_scatter(copies: list, columns: list, imported: list,
                  finals: list) -> dict:
    """Données du nuage de points : variables sélectionnables + 1 point par copie.

    Valeurs = notes rescalées (note*) par colonne + note finale. `None` si la
    copie n'a pas cette note."""
    variables = [{"id": c["key"], "label": c["label"]} for c in columns]
    variables.append({"id": "__final__", "label": "Note finale"})
    points = []
    for c, fin in zip(copies, finals):
        resc = {col["key"]: val for col, val in copy_rescaled(c, columns, imported)}
        v = {col["key"]: (round(resc[col["key"]], 3) if col["key"] in resc else None)
             for col in columns}
        v["__final__"] = round(fin, 3)
        points.append({"name": c["canonical_name"], "v": v})
    return {"variables": variables, "points": points}


def compute_multi_series_stats(series: list, granularity: float) -> dict:
    """Bins de largeur fixe `granularity` (alignés sur des multiples), partagés."""
    g = granularity if (granularity and granularity > 0) else 1.0
    all_vals = [v for s in series for v in s["values"]]
    if all_vals:
        lo = math.floor(min(0.0, min(all_vals)) / g) * g
        hi = math.ceil(max(all_vals) / g) * g
    else:
        lo, hi = 0.0, g
    if hi <= lo:
        hi = lo + g
    nbins = max(1, min(400, round((hi - lo) / g)))
    hi = lo + nbins * g
    w = (hi - lo) / nbins
    out_series, max_count = [], 0
    for s in series:
        counts = [0] * nbins
        for v in s["values"]:
            idx = min(nbins - 1, max(0, int((v - lo) / w)))
            counts[idx] += 1
        max_count = max(max_count, max(counts, default=0))
        out_series.append({"name": s["name"], "color": s["color"],
                           "opacity": s.get("opacity", 0.45), "counts": counts,
                           "stats": series_stats(s["values"])})
    return {"lo": lo, "hi": hi, "nbins": nbins, "series": out_series,
            "max_count": max_count}


def multi_histogram_geometry(mstats: dict, width: int = 560, height: int = 210,
                             mean_line: bool = False, vline: float | None = None) -> dict:
    """Géométrie SVG : barres superposées (une couleur/alpha par série) + ticks.

    `vline` : si fourni, ajoute une ligne verticale (x clampé au cadre)."""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 10, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_t + plot_h
    nbins = mstats["nbins"]
    lo, hi = mstats["lo"], mstats["hi"]
    span = (hi - lo) or 1.0
    max_c = mstats["max_count"] or 1
    slot = plot_w / nbins

    def sx(value):
        return pad_l + (value - lo) / span * plot_w

    series_geo = []
    for s in mstats["series"]:
        bars = []
        for i, c in enumerate(s["counts"]):
            bh = (c / max_c) * plot_h
            bars.append({
                "x": pad_l + i * slot + slot * 0.06, "w": slot * 0.88,
                "y": base_y - bh, "h": bh, "count": c,
                "cx": pad_l + i * slot + slot / 2,
                "lo": round(lo + i * span / nbins, 2),
                "hi": round(lo + (i + 1) * span / nbins, 2),
            })
        series_geo.append({"name": s["name"], "color": s["color"],
                           "opacity": s["opacity"], "bars": bars, "stats": s["stats"]})
    tick_vals = [round(lo + (hi - lo) * f, 1) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    xticks = [{"x": sx(v), "label": f"{v:g}"} for v in tick_vals]
    geo = {"width": width, "height": height, "base_y": base_y, "pad_t": pad_t,
           "series": series_geo, "xticks": xticks,
           "legend": [{"name": s["name"], "color": s["color"]} for s in series_geo]}
    if mean_line and series_geo:
        st = series_geo[0]["stats"]
        band_lo = max(lo, st["mean"] - st["std"])
        band_hi = min(hi, st["mean"] + st["std"])
        geo["mean_x"] = sx(st["mean"])
        geo["band_x"] = sx(band_lo)
        geo["band_w"] = sx(band_hi) - sx(band_lo)
        geo["mean"] = st["mean"]
    if vline is not None:
        geo["vline_x"] = min(max(sx(vline), pad_l), pad_l + plot_w)
        geo["vline_val"] = vline
    return geo


