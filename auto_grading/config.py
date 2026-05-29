"""Configuration runtime partagée (dossier de l'examen, liste étudiants, pondération).

Stockée dans `<project_root>/config.json`, créée au premier save. Toute valeur
absente est complétée par DEFAULTS. Importé par cv_grade.py, layout_store.py,
student_list.py, extract_pages.py, front/server.py…

Multi-projets : `project_root()` désigne le dossier du projet actif.
  - env `AMCX_PROJECT_DIR` (utile pour tests / dev)
  - sinon `project_state.active_project()` (pointeur global)
  - sinon `ROOT` (= dossier de l'installation — fallback dev install)

`amc_dir` est LA clé qui rend le pipeline réutilisable : c'est le dossier de l'examen
courant (PDF des copies scannées ; éventuellement un sous-dossier `data/` avec les
SQLite AMC et/ou un fichier `.xy` de calage). Tous les chemins relatifs sont résolus
depuis `project_root()`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Le package est plat (pas de __init__.py) et utilisé via `sys.path += [ROOT]`
# par les entry points (server.py, new_project.py…). On garantit l'import absolu.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
import project_state  # noqa: E402

# Dossier de l'installation (fallback quand aucun projet n'est défini).
ROOT = _THIS_DIR

DEFAULTS = {
    # --- examen courant -----------------------------------------------------
    "amc_dir": "../projet",   # dossier de l'examen (copies scannées, data/, .xy)
    "scan_pdfs": [],          # PDF de copies (vide = auto-découverte dans amc_dir)
    "scan_pdfs_excluded": [], # PDF à ignorer dans l'auto-découverte (UI "Retirer")
    "answer_sheet_page": 0,   # 0 = page de réponses dérivée du layout ; sinon forcée
    # --- liste étudiants ----------------------------------------------------
    "student_xlsx": "",       # xlsx liste étudiants (vide tant que non fournie)
    "xlsx_id_col": "id_etudiant",
    "xlsx_nom_col": "nom",
    "xlsx_prenom_col": "prenom_etat_civil",
    # --- notes importées + dashboard ---------------------------------------
    "export_template_xlsx": "",  # modèle xlsx scolarité (export_scolarite.py) ; "" = aucun
    "grade_files": [],        # fichiers de notes importés (csv/xlsx) — grade_imports.py
    "hist_granularity": 1.0,  # largeur d'une barre d'histogramme, en points
    "qcm_seuil": 32.0,        # note QCM maximale théorique (barème)
    "qcm_max": 20.0,          # échelle cible du QCM rescalé (QCM* = QCM × max / seuil)
    "qcm_agg_weight": 1.0,    # poids du QCM dans l'agrégation finale
    "final_threshold": 20.0,  # plafond dur appliqué à la note finale agrégée
    "pass_mark": 10.0,        # seuil de réussite (ligne verticale sur l'histo final)
    # --- IA (édition assistée de questions) --------------------------------
    "anthropic_api_key": "", # clé sk-ant-… stockée dans config.json du projet
    "ai_model":          "claude-sonnet-4-6",   # ou claude-opus-4-7
}


def project_root() -> Path:
    """Racine du projet actif.

    Précédence : `AMCX_PROJECT_DIR` (env) > pointeur global > installation (ROOT).
    """
    env = os.environ.get("AMCX_PROJECT_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    p = project_state.active_project()
    if p and p.exists():
        return p
    return ROOT


def _config_path() -> Path:
    """Chemin du fichier `config.json` du projet actif (calcul dynamique)."""
    return project_root() / "config.json"


# Conservé pour rétrocompat (certains scripts l'importent ; ne pas le retirer
# sans avoir purgé ses utilisations en aval). Pointe vers le projet **actif**
# au moment de l'import — peut être stale si le projet change. Préférer
# `_config_path()` ou `load_config()` pour toute lecture/écriture.
CONFIG_PATH = _config_path()


def load_config() -> dict:
    """Retourne la config courante (DEFAULTS complétés par config.json si présent)."""
    cfg = dict(DEFAULTS)
    path = _config_path()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(updates: dict) -> dict:
    """Fusionne `updates` dans la config et écrit config.json. Retourne la config complète.

    Ne persiste que les clés présentes dans DEFAULTS (les clés obsolètes sont
    purgées au prochain enregistrement)."""
    cfg = load_config()
    for k, v in updates.items():
        if k in DEFAULTS:
            cfg[k] = v
    cfg = {k: v for k, v in cfg.items() if k in DEFAULTS}
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def get(key: str):
    """Raccourci lecture d'une clé."""
    return load_config()[key]


def resolve_path(rel_or_abs: str) -> Path:
    """Résout un chemin de config (relatif → relatif au projet actif)."""
    p = Path(rel_or_abs)
    return p if p.is_absolute() else (project_root() / p)


def amc_dir() -> Path:
    """Dossier de l'examen courant (clé `amc_dir`)."""
    return resolve_path(load_config()["amc_dir"])


def amc_data_dir() -> Path:
    """Sous-dossier `data/` de l'examen (SQLite AMC, s'ils existent)."""
    return amc_dir() / "data"
