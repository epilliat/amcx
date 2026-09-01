"""Régénère answer_key.py depuis le sujet (`sujet/exam.tex`) et le calage.

answer_key.py n'est plus qu'un **repli structurel** : la source de vérité du
corrigé et du barème est `sujet/exam.tex` (lu par `sujet_store` / `score.py`).
Ce script matérialise un answer_key.py cohérent pour les modules qui itèrent
encore `ANSWER_KEY` (batch_run.py…).

Le calage (positions/lettres des cases) doit être disponible : soit
`<amc_dir>/data/layout.sqlite`, soit un `.xy` (compile le sujet d'abord).

Usage:
   python regen_answer_key.py                    # imprime sur stdout
   python regen_answer_key.py --out answer_key.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sujet_store import effective_spec, get_bareme, parse_tex

ROOT = Path(__file__).resolve().parent


def build_answer_key() -> dict:
    """{q: {type, options, correct, tag, (b, m)}} dérivé d'exam.tex + calage."""
    qs = parse_tex()
    bareme = get_bareme()
    out = {}
    for q in sorted(qs):
        spec = effective_spec(q)
        entry = {"type": spec["type"], "options": spec["options"],
                 "correct": spec["correct"], "tag": spec["tag"]}
        if spec["type"] == "mult":
            chars = (bareme.get(q, {}) or {}).get("chars", {}) or {}
            correct = set(spec["correct"])
            b_vals = [p for c, p in chars.items() if c in correct]
            m_vals = [p for c, p in chars.items() if c not in correct]
            entry["b"] = round(b_vals[0], 6) if b_vals else 0.0
            entry["m"] = round(m_vals[0], 6) if m_vals else 0.0
        out[q] = entry
    return out


def format_entry(q: int, d: dict) -> str:
    if d["type"] == "single":
        return (f'    {q}: {{"type": "single", "options": {d["options"]!r:18}, '
                f'"correct": {d["correct"]!r:8}, "tag": {d["tag"]!r}}},')
    return (f'    {q}: {{"type": "mult",   "options": {d["options"]!r:18}, '
            f'"correct": {d["correct"]!r:8}, "b": {d["b"]:.4f}, "m": {d["m"]:.4f}, '
            f'"tag": {d["tag"]!r}}},')


def render(ak: dict) -> str:
    body = "\n".join(format_entry(q, d) for q, d in sorted(ak.items()))
    return ('"""Clé de correction — REGÉNÉRÉE par regen_answer_key.py.\n\n'
            "Repli structurel ; la source de vérité est sujet/exam.tex.\n"
            'Ne pas éditer à la main.\n"""\n\n'
            "ANSWER_KEY = {\n" + body + "\n}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="écrire ce fichier au lieu de stdout (ex. answer_key.py)")
    args = ap.parse_args()

    ak = build_answer_key()
    if not ak:
        print("Aucune question trouvée — vérifie sujet/exam.tex et le calage.")
        return
    text = render(ak)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Écrit: {args.out} ({len(ak)} questions)")
    else:
        print(text)


if __name__ == "__main__":
    main()
