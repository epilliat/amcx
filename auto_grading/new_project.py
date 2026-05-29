"""new_project.py — crée un projet AMCx vierge, prêt à l'emploi.

Usage CLI :
    python new_project.py <chemin_du_nouveau_projet> [--template examen_minimal]

API (utilisée par l'UI) :
    from new_project import create_project, list_templates
    create_project(dest, template="examen_minimal")
    create_project(dest, template="from_amc", source_tex=Path("…/exam.tex"))

Le dossier `templates/<id>/exam.tex` contient le sujet de démarrage (LaTeX
canonique avec marqueurs `%%QCM-…`). Un projet vierge = code copié + config
par défaut + ce sujet copié au bon endroit. Le PDF, le `.xy` et les
`raw_responses/` seront produits ensuite par le pipeline.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # auto_grading/ (source du code)
PROJECT_ROOT = ROOT.parent                      # dossier projet source
TEMPLATES_DIR = ROOT / "templates"               # templates fournis

# Dossiers de données / artefacts générés — jamais copiés vers un projet vierge.
_SKIP_DIRS = {"pages", "raw_responses", "raw_responses_cv", "results", "imports",
              "compte_rendu", "_removed", "to_review", "__pycache__",
              "zoom_cache", ".git", ".venv", "sujet"}
# Fichiers spécifiques à un examen — recréés vierges.
_SKIP_FILES = {"config.json", "answer_key.py", "student_list.xlsx",
               "student_list_pending.xlsx", ".env", "exam.tex",
               "DOC-sujet.pdf", "cell_clf.pkl"}

CONFIG_TEMPLATE = {
    "amc_dir": "../projet",
    "scan_pdfs": [],
    "answer_sheet_page": 0,
    "student_xlsx": "",
    "xlsx_id_col": "id_etudiant",
    "xlsx_nom_col": "nom",
    "xlsx_prenom_col": "prenom_etat_civil",
    "export_template_xlsx": "",
    "grade_files": [],
    "hist_granularity": 1.0,
    "qcm_seuil": 10.0,
    "qcm_max": 20.0,
    "qcm_agg_weight": 1.0,
    "final_threshold": 20.0,
    "pass_mark": 10.0,
}


# --- Templates -------------------------------------------------------------

def list_templates() -> list[dict]:
    """Liste les templates disponibles dans `templates/<id>/{exam.tex,meta.json}`."""
    out: list[dict] = []
    if not TEMPLATES_DIR.exists():
        return out
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        tex_path = d / "exam.tex"
        if not (meta_path.exists() and tex_path.exists()):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": meta.get("id", d.name),
            "name": meta.get("name", d.name),
            "description": meta.get("description", ""),
        })
    return out


def _read_template_tex(template_id: str) -> str:
    """Lit `templates/<template_id>/exam.tex`. Lève KeyError si introuvable."""
    p = TEMPLATES_DIR / template_id / "exam.tex"
    if not p.exists():
        raise KeyError(f"Template inconnu : {template_id}")
    return p.read_text(encoding="utf-8")


# --- Copie du code ---------------------------------------------------------

def _ignore(dirpath: str, names: list[str]) -> set[str]:
    skip = set()
    for n in names:
        if n in _SKIP_DIRS or n in _SKIP_FILES:
            skip.add(n)
        elif n.endswith((".pyc", ".xy")):
            skip.add(n)
    return skip


def _copy_install(dest_ag: Path) -> None:
    """Copie le code d'`auto_grading/` (hors données) vers `dest_ag`."""
    shutil.copytree(ROOT, dest_ag, ignore=_ignore)


# --- Création d'un projet ---------------------------------------------------

def create_project(
    dest: Path,
    template: str = "examen_minimal",
    source_tex: Path | None = None,
    try_migrate: bool = True,
) -> Path:
    """Crée un projet AMCx à l'emplacement `dest`.

    - `template == "from_amc"` + `source_tex` : importe un sujet AMC existant
      (le copie vers `sujet/exam.tex`). Si `try_migrate`, tente de le passer en
      mode canonique (best-effort — si la migration échoue, le projet reste
      utilisable en mode legacy).
    - Sinon `template` désigne un dossier de `templates/<template>/` (avec
      `exam.tex` + `meta.json`).

    Retourne le chemin du sous-dossier `auto_grading/` du nouveau projet
    (= le `project_root()` à utiliser pour ouvrir ce projet).

    Si une étape échoue après la création de `dest/`, on rollback en supprimant
    `dest/` — pour ne pas laisser de coquille incomplète qui rend la modale
    « Ouvrir » incohérente.
    """
    dest = Path(dest).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} existe déjà et n'est pas vide.")

    # Validation amont : on vérifie le template AVANT de toucher au disque,
    # comme ça en cas d'erreur on ne laisse pas de demi-projet derrière.
    template_tex: str | None = None
    if template == "from_amc":
        if not source_tex or not Path(source_tex).exists():
            raise FileNotFoundError("source_tex requis pour template='from_amc'.")
    else:
        # Alias historique : "blank" → "examen_minimal".
        if template == "blank":
            template = "examen_minimal"
        template_tex = _read_template_tex(template)  # lève KeyError si inconnu

    dest.mkdir(parents=True, exist_ok=True)
    ag = dest / "auto_grading"
    try:
        # 1. copier le code (sans les données) — conserve models/cell_clf_full.pkl
        _copy_install(ag)

        # 2. fichiers à la racine du projet
        for fn in ("pyproject.toml", "CLAUDE.md", "README.md"):
            src = PROJECT_ROOT / fn
            if src.exists():
                shutil.copy(src, dest / fn)

        # 3. dossier de l'examen (PDF scannés + xlsx liste à y déposer plus tard)
        (dest / "projet").mkdir(parents=True, exist_ok=True)

        # 4. config + sujet vierges
        (ag / "config.json").write_text(
            json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        sujet_dir = ag / "sujet"
        sujet_dir.mkdir(parents=True, exist_ok=True)

        # 5. exam.tex : depuis fichier importé OU depuis template (déjà lu)
        if template == "from_amc":
            shutil.copy(source_tex, sujet_dir / "exam.tex")
            if try_migrate:
                _try_migrate_to_canonical(ag)
        else:
            (sujet_dir / "exam.tex").write_text(template_tex, encoding="utf-8")
    except Exception:
        # Rollback : on supprime ce qui a été créé pour ne pas laisser de
        # coquille qui empêcherait une nouvelle création du même nom.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    return ag


def _try_migrate_to_canonical(ag: Path) -> tuple[bool, str]:
    """Tente la migration du sujet importé en pointant `AMCX_PROJECT_DIR` vers
    `ag` (le nouveau projet) et en appelant `sujet_store.migrate_to_canonical()`
    dans un sous-process — évite de polluer les caches du process appelant.

    Retourne `(ok, message)`. Best-effort : un échec laisse le sujet en legacy.
    """
    import subprocess
    cmd = [
        sys.executable, "-c",
        "import sys, os; sys.path.insert(0, os.environ['AG_DIR']); "
        "from sujet_store import migrate_to_canonical; "
        "migrate_to_canonical()"
    ]
    env = {**__import__('os').environ, "AMCX_PROJECT_DIR": str(ag), "AG_DIR": str(ag)}
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "Migration vers canonique réussie."
        return False, f"Migration échouée (code {r.returncode}) : {r.stderr.strip()[:300]}"
    except Exception as e:
        return False, f"Migration impossible : {e}"


# --- CLI -------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Crée un projet AMCx vierge.")
    ap.add_argument("dest", help="Chemin du nouveau projet")
    ap.add_argument("--template", default="examen_minimal",
                    help="ID du template (défaut: examen_minimal)")
    ap.add_argument("--from-amc", metavar="EXAM.TEX",
                    help="Importer un fichier AMC existant à la place du template")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    if args.from_amc:
        ag = create_project(dest, template="from_amc",
                            source_tex=Path(args.from_amc).resolve())
    else:
        ag = create_project(dest, template=args.template)

    print(f"✓ Projet vierge créé : {dest}")
    print(f"  (dossier auto_grading : {ag})")
    print("  Étapes suivantes :")
    print(f"    cd {dest}")
    print("    python auto_grading/front/server.py        # UI → onglet « Sujet »")
    print("    (éditer puis COMPILER le sujet ; imprimer ; faire passer l'examen)")
    print("    (déposer les PDF scannés + le xlsx liste dans  projet/)")
    print("    python auto_grading/extract_pages.py")
    print("    python auto_grading/cv_grade.py --all")
    print("    python auto_grading/front/seed_raw_responses.py")


if __name__ == "__main__":
    main()
