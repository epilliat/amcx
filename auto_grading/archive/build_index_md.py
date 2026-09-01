"""Génère to_review/INDEX.md avec images embarquées + liens de navigation entre paires."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
TO_REVIEW_DIR = ROOT / "to_review"


def main():
    images = sorted(TO_REVIEW_DIR.glob("batch*_page_*.jpg"))
    print(f"{len(images)} images à indexer")

    lines = ["# Copies à relire — index navigable",
             "",
             f"{len(images)} paires. Pour chaque section :",
             "- Le lien `JSON` ouvre le fichier de réponses à éditer.",
             "- Image inline ci-dessous. Les flèches ↑↓ sautent à la précédente/suivante.",
             "",
             "## Table des matières",
             ""]

    stems = [p.stem for p in images]
    for stem in stems:
        lines.append(f"- [{stem}](#{stem.replace('_','-')})")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, stem in enumerate(stems):
        prev_anchor = f"#{stems[i-1].replace('_','-')}" if i > 0 else "#table-des-matieres"
        next_anchor = f"#{stems[i+1].replace('_','-')}" if i < len(stems) - 1 else "#table-des-matieres"
        lines.append(f"## <a id=\"{stem.replace('_','-')}\"></a>{stem}")
        lines.append("")
        lines.append(f"[📝 JSON]({stem}.json) · [⬅️ Préc]({prev_anchor}) · [➡️ Suiv]({next_anchor}) · [🔝 Top](#table-des-matieres)")
        lines.append("")
        lines.append(f"![{stem}]({stem}.jpg)")
        lines.append("")
        lines.append("---")
        lines.append("")

    out = TO_REVIEW_DIR / "INDEX.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Écrit: {out}  ({out.stat().st_size // 1024} Ko)")


if __name__ == "__main__":
    main()
