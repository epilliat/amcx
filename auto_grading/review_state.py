"""Ce qu'il reste à relire sur une copie — logique pure, zéro I/O.

Une case signalée par la correction automatique n'est pas un verdict : c'est une
question posée au relecteur. Tant que rien n'enregistre la réponse, une
relecture interrompue ne peut pas reprendre où elle s'est arrêtée, et un
compteur « à revoir » mesure ce que la correction a produit au lieu de ce qu'il
reste à faire. Ce module ajoute l'état manquant :

  - `_reviewed_cells`     : ["12_C", …]  cases explicitement traitées
  - `_reviewed_questions` : [12, …]      questions traitées au niveau question

⚠ **Le signalement structurel n'est pas stocké, il est recalculé** à chaque
affichage à partir de `answers` et du type de la question. C'est une fonction
pure des réponses courantes : le stocker le rendait périmé dès la première
correction (constaté sur EXAM_2026 — 5 cases portaient encore « une seule
réponse attendue » alors que la question en avait exactement une). Les
signalements de mesure (E1/E2/E3, GBM incertain, désaccord AMC), eux, sont
stockés : ils dépendent de l'image, qu'on n'a plus sous la main ici.

⚠ Une case **modifiée** compte comme traitée sans clic supplémentaire : basculer
une case EST la décision du relecteur. Sinon le drapeau redemanderait de
regarder ce qu'on vient de corriger.
"""

from __future__ import annotations

# Motifs au niveau de la question (recalculés).
Q_NO_ANSWER = "no_answer"        # `single` sans aucune réponse lue
Q_MULTI_ANSWER = "multi_answer"  # `single` avec plusieurs réponses lues

# Motifs au niveau de la case (stockés par cv_grade / seed).
C_DIFF = "diff"            # CV ≠ AMC
C_DISAGREE = "disagree"    # E1/E2/E3 divergent
C_UNCERTAIN = "uncertain"  # GBM entre 0.30 et 0.70

_STRUCTURAL = "structural"  # legacy : présent dans les JSON d'avant le recalcul


def cell_key(q, char: str) -> str:
    return f"{int(q)}_{char}"


def _answers(d: dict, field: str = "answers") -> dict[int, set]:
    return {int(k): set(v) for k, v in (d.get(field) or {}).items()}


def edited_cells(d: dict) -> set[str]:
    """Cases dont l'état courant diffère de la lecture CV d'origine."""
    cur, cv = _answers(d), _answers(d, "_cv_answers")
    out = set()
    for q in set(cur) | set(cv):
        for ch in cur.get(q, set()) ^ cv.get(q, set()):
            out.add(cell_key(q, ch))
    return out


def reviewed_cells(d: dict) -> set[str]:
    """Cases traitées : marquées explicitement, ou modifiées à la main."""
    return {str(k) for k in (d.get("_reviewed_cells") or [])} | edited_cells(d)


def reviewed_questions(d: dict) -> set[int]:
    out = set()
    for q in d.get("_reviewed_questions") or []:
        try:
            out.add(int(q))
        except (TypeError, ValueError):
            pass
    return out


def flagged_cells(d: dict) -> dict[str, dict]:
    """Signalements de mesure, par case, motifs FUSIONNÉS.

    Une case à la fois en désaccord avec AMC et douteuse portait auparavant le
    seul motif AMC (déduplication par `seen` dans la vue) : l'autre raison
    disparaissait de l'écran. Ici les deux sources sont unies.

    Le motif `structural` des JSON anciens est ignoré : il est recalculé.
    """
    out: dict[str, dict] = {}
    for item in d.get("_cv_amc_diff") or []:
        k = cell_key(item["q"], item["char"])
        out[k] = {"q": int(item["q"]), "char": item["char"], "reasons": [C_DIFF],
                  "cv": item.get("cv"), "amc": item.get("amc"),
                  "ratio": None, "masked": None, "proba": None}
    for a in d.get("_ambiguous_cells") or []:
        reasons = [r for r in (a.get("reasons") or []) if r != _STRUCTURAL]
        if not reasons:
            continue                      # structurel seul → recalculé, pas stocké
        k = cell_key(a["q"], a["char"])
        cur = out.get(k)
        if cur is None:
            cur = {"q": int(a["q"]), "char": a["char"], "reasons": [],
                   "cv": None, "amc": None}
            out[k] = cur
        cur["reasons"] = cur["reasons"] + [r for r in reasons
                                           if r not in cur["reasons"]]
        for f in ("ratio", "masked", "proba"):
            if a.get(f) is not None:
                cur[f] = a[f]
    for v in out.values():
        v.setdefault("ratio", None)
        v.setdefault("masked", None)
        v.setdefault("proba", None)
    return out


def structural_reason(qtype: str, n_selected: int) -> str | None:
    """Le signalement de structure d'une question — pure fonction des réponses.

    Une question à choix unique laissée vide est le cas le plus banal d'un QCM :
    c'est UN signal, au niveau de la question. Le signaler case par case
    remplissait la file de relecture (496 des 885 cases signalées d'EXAM_2026,
    soit 56 %, pour des questions que l'étudiant avait simplement laissées
    blanches) et noyait les vraies divergences de mesure.
    """
    if qtype != "single":
        return None
    if n_selected == 0:
        return Q_NO_ANSWER
    if n_selected > 1:
        return Q_MULTI_ANSWER
    return None


def _uncertainty(proba) -> float:
    """0 = le modèle est catégorique, 1 = il hésite à 50/50."""
    if proba is None:
        return 0.5
    return 1.0 - abs(2.0 * float(proba) - 1.0)


def copy_review(d: dict, spec_of, qcm_questions) -> dict:
    """Décrit la relecture d'une copie, groupée PAR QUESTION.

    Le regroupement par question n'est pas cosmétique : le signalement de
    structure est une propriété de la question, et juger « rien n'est coché ici »
    demande de voir toutes ses cases ensemble. Les cases non signalées sont
    rendues quand même, en contexte.

    `spec_of(q)` rend `{type, tag, correct, options}` ; `qcm_questions` est la
    liste ordonnée des numéros de question QCM.

    Retour : `{items, n_flagged, n_open, risk}` où chaque item est
    `{q, tag, type, correct, reason, reviewed, n_open, cells:[…]}`.
    """
    cur = _answers(d)
    flags = flagged_cells(d)
    seen_cells = reviewed_cells(d)
    seen_qs = reviewed_questions(d)
    # « Relue en entier » couvre tous les signalements de la copie, par
    # définition : ce drapeau ne se pose que depuis une vue qui montre TOUTES
    # les cases. Sans ça, une copie relue de bout en bout garderait ses halos et
    # rentrerait dans la file — la relecture ne convergerait jamais.
    fully = "validated" in (d.get("_flags") or [])
    items, n_flagged, n_open = [], 0, 0
    risk = 0.0
    for q in qcm_questions:
        spec = spec_of(q)
        options = list(spec.get("options") or [])
        sel = cur.get(q, set())
        reason = structural_reason(spec.get("type", "mult"), len(sel & set(options)))
        q_reviewed = fully or q in seen_qs
        cells, q_open = [], 0
        for ch in options:
            k = cell_key(q, ch)
            f = flags.get(k)
            reviewed = fully or k in seen_cells
            if f is not None:
                n_flagged += 1
                if not reviewed:
                    q_open += 1
                    risk += 1.0 + _uncertainty(f.get("proba"))
            cells.append({
                "char": ch, "selected": ch in sel,
                "correct": ch in (spec.get("correct") or []),
                "flagged": f is not None,
                "reasons": (f or {}).get("reasons", []),
                "reviewed": reviewed,
                "ratio": (f or {}).get("ratio"),
                "masked": (f or {}).get("masked"),
                "proba": (f or {}).get("proba"),
                "cv": (f or {}).get("cv"), "amc": (f or {}).get("amc"),
            })
        if reason is not None:
            n_flagged += 1
            if not q_reviewed:
                q_open += 1
                risk += 1.0
        if reason is None and q_open == 0 and not any(c["flagged"] for c in cells):
            continue
        n_open += q_open
        items.append({"q": q, "tag": spec.get("tag", ""),
                      "type": spec.get("type", "mult"),
                      "correct": "".join(spec.get("correct") or []),
                      "reason": reason, "reviewed": q_reviewed,
                      "n_open": q_open, "cells": cells})
    return {"items": items, "n_flagged": n_flagged, "n_open": n_open,
            "risk": round(risk, 3)}
