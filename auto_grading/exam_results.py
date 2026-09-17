"""Les résultats d'un examen : une ligne par étudiant, et rien d'autre.

Logique **pure** — zéro I/O, zéro Flask, aucune notion de projet actif. Les
copies, les absents et la note entrent par paramètre.

⚠ **C'est la seule construction de cette table.** `/export.csv` et le
`compte_rendu/notes.csv` que lisent les courriels la bâtissaient chacun de leur
côté, avec deux tris, deux façons de marquer un absent et deux jeux de
colonnes : deux fichiers censés dire la même chose, écrits par deux codes. Un
troisième consommateur — la vue d'ensemble, qui lira chaque projet par
sous-processus — aurait fait un troisième.

Utilisation en ligne de commande, pour lire un projet **sans passer par le
serveur** (c'est ainsi qu'un niveau supérieur rassemblera plusieurs examens) :

    python auto_grading/exam_results.py                      # projet actif
    python auto_grading/exam_results.py --json
    python auto_grading/exam_results.py --project ~/Documents/AMCx/QCM1 --json
"""

from __future__ import annotations

from pathlib import Path

# ⚠ Marqueur des absents : une CHAÎNE, jamais 0. Un absent n'a pas eu zéro, il
# n'a pas composé — les confondre fausse toute moyenne recalculée en aval sur
# le fichier exporté. Il était déclaré trois fois (serveur, export scolarité,
# courriels), chaque copie renvoyant aux deux autres en commentaire.
ABSENT_MARK = "ABS"


class ResultsError(Exception):
    """Ce qu'on ne peut pas lire, et pourquoi.

    ⚠ Un dossier qui n'est pas un projet rendait « 0 copie, barème 0 » avec un
    code de sortie 0, et un chemin inexistant retombait sur le dossier
    d'installation — un examen vide se serait glissé dans un relevé
    d'ensemble sans que rien ne le signale.
    """


def student_rows(copies: list, absents: list, note_of=None) -> list[dict]:
    """Une ligne par copie **et une par absent**, triées par nom.

    `note_of(copy)` rend la note de l'examen ; par défaut le score brut. Le
    paramètre existe pour que ce module ne décide pas de la note — c'est
    `server.exam_columns()` + `grades_view.compute_aggregate` qui la définit,
    et une seconde définition ici finirait par les contredire.

    ⚠ Un absent a une ligne, à sa place alphabétique. Sans elle, il disparaît
    du fichier remis à la scolarité et « absent » devient indiscernable de
    « oublié dans l'export ». Ses colonnes issues de la copie valent
    `ABSENT_MARK` ; ses éventuelles notes venues d'ailleurs sont conservées —
    il peut très bien avoir rendu un projet sans venir au QCM.
    """
    note_of = note_of or (lambda c: c["score"])
    rows = [{
        "batch":      c["batch"],
        "page":       c["page"],
        "id":         c["canonical_id"],
        "nom_prenom": c["canonical_name"],
        "courriel":   c["canonical_email"],
        "id_lu":      c["student_id"],
        "brut":       round(c["score"], 2),
        "note":       round(note_of(c), 2),
        "validee":    bool(c["validated"]),
        "flags":      list(c["flags"]),
        "absent":     False,
    } for c in copies]
    rows += [{
        "batch": "", "page": "", "id": st.id,
        "nom_prenom": f"{st.nom} {st.prenom}", "courriel": st.email,
        "id_lu": "", "brut": None, "note": None,
        "validee": None, "flags": ["absent"], "absent": True,
    } for st in absents]
    return sorted(rows, key=lambda r: r["nom_prenom"])


def _cell(v):
    """Une valeur de note absente s'écrit `ABS`, jamais vide ni zéro."""
    return ABSENT_MARK if v is None else v


# ⚠ `note_sur_32` et `QCM_brut_sur_32` gardent leurs noms historiques : 32
# était le barème d'EXAM_2026, pas une constante, mais des scripts de la
# scolarité et l'onglet Courriels (`mail_score_col`) lisent ces intitulés.
EXPORT_HEADER = ["batch", "page", "id_canonique", "nom_prenom", "courriel",
                 "id_lu", "note_sur_32", "note_finale", "validee", "flags"]
REPORT_HEADER = ["batch", "page", "id_canonique", "nom_prenom", "courriel",
                 "QCM_brut_sur_32", "note_finale", "validee"]


def export_csv_rows(rows: list[dict]) -> list[list]:
    """Lignes de `/export.csv` (téléchargement)."""
    return [[r["batch"], r["page"], r["id"], r["nom_prenom"], r["courriel"],
             r["id_lu"], _cell(r["brut"]), _cell(r["note"]),
             "" if r["absent"] else ("oui" if r["validee"] else "non"),
             ";".join(r["flags"])] for r in rows]


def report_csv_rows(rows: list[dict]) -> list[list]:
    """Lignes de `compte_rendu/notes.csv` — c'est ce fichier que lisent les
    courriels : le supprimer ou le tronquer, c'est un envoi qui ne part pas."""
    return [[r["batch"], r["page"], r["id"], r["nom_prenom"], r["courriel"],
             _cell(r["brut"]), _cell(r["note"]),
             "" if r["absent"] else ("oui" if r["validee"] else "non")]
            for r in rows]


def summary(rows: list[dict]) -> dict:
    """Ce qu'un niveau supérieur veut savoir d'un examen sans le détailler."""
    notes = [r["note"] for r in rows if not r["absent"]]
    return {
        "n_students": len(rows),
        "n_copies":   len(notes),
        "n_absent":   sum(1 for r in rows if r["absent"]),
        "n_validated": sum(1 for r in rows if r["validee"]),
        "n_unlinked": sum(1 for r in rows if not r["absent"] and not r["id"]),
    }


def is_project(root: Path) -> bool:
    """Un projet AMCx porte `sujet/exam.tex` (même règle que `project_state`)."""
    return (root / "sujet" / "exam.tex").exists()


def resolve_project(path: Path) -> Path:
    """Le dossier de projet désigné par `path`, ou son sous-dossier canonique.

    ⚠ `new_project.create_project(dest)` rend `dest/auto_grading` : c'est LUI
    le projet, et c'est lui que pointe `~/.config/amcx/active_project`. Or ce
    qu'on nomme « le projet » est le dossier parent — celui qui porte son nom,
    et celui qu'une vue d'ensemble trouvera en énumérant un dossier. On essaie
    donc les deux, dans cet ordre.

    Ce n'est pas une devinette : la règle est déterministe, elle ne s'applique
    que si le parent n'est pas lui-même un projet, et le chemin réellement lu
    est rendu dans la sortie (`path`).
    """
    if is_project(path):
        return path
    if is_project(path / "auto_grading"):
        return path / "auto_grading"
    raise ResultsError(
        f"{path} n'est pas un projet AMCx : ni {path}/sujet/exam.tex "
        f"ni {path}/auto_grading/sujet/exam.tex n'existe.")


def _check_project(root: Path) -> None:
    if not is_project(root):
        raise ResultsError(f"{root} n'est pas un projet AMCx "
                           f"(pas de sujet/exam.tex).")


# --------------------------------------------------------------------------
# Ligne de commande — lire UN projet sans serveur
# --------------------------------------------------------------------------

def _reexec_if_other_project(project: str | None, argv: list[str]) -> None:
    """Ré-exécute ce script sur un autre projet, ou ne fait rien.

    ⚠ `config`, `sujet_store` et `server` figent leurs chemins **à l'import** :
    changer de projet dans le process courant lirait le sujet de l'un et les
    copies de l'autre, en silence. Un process = un projet.

    ⚠ L'argv de remplacement est **reconstruit**, pas recopié de `sys.argv` :
    appelé par `amcx results`, celui-ci porte le mot « results » que ce script
    ne connaît pas — il repartait alors en erreur d'arguments.
    """
    import os
    import sys
    if not project:
        return
    target = str(resolve_project(Path(project).expanduser().resolve()))
    if os.environ.get("AMCX_PROJECT_DIR") == target:
        return
    os.environ["AMCX_PROJECT_DIR"] = target
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *argv])


def load() -> dict:
    """Résultats du projet **actif** (pointeur global ou `AMCX_PROJECT_DIR`)."""
    import sys
    here = Path(__file__).resolve().parent
    for p in (here, here / "front"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import project_state
    import server                                    # 0,3 s, Flask non démarré

    _check_project(server.config.project_root())
    copies = server.list_all_copies()
    cols, thr = server.exam_columns(), server.exam_threshold()
    rows = student_rows(
        copies, server.absent_students(copies),
        note_of=lambda c: server.compute_aggregate(c, cols, [], thr))
    root = server.config.project_root()
    return {"project": project_state.display_name(root),
            "path": str(root),
            "bareme": server.subject_total_max(),
            "summary": summary(rows),
            "students": rows}


def main(argv=None) -> int:
    import argparse
    import json
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", help="dossier du projet (défaut : projet actif)")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    a = ap.parse_args(argv)
    try:
        _reexec_if_other_project(a.project, ["--json"] if a.json else [])
        data = load()
    except ResultsError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    s = data["summary"]
    print(f'{data["project"]}  —  barème {data["bareme"]:g}')
    print(f'  {s["n_copies"]} copie(s) · {s["n_absent"]} absent(s) · '
          f'{s["n_validated"]} validée(s) · {s["n_unlinked"]} non reliée(s)')
    for r in data["students"]:
        note = ABSENT_MARK if r["absent"] else f'{r["note"]:g}'
        print(f'  {r["nom_prenom"]:<34} {r["id"]:>8}  {note:>6}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
