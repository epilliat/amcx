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
from threading import RLock

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
    "student_xlsx": "",       # liste étudiants (xlsx/csv ; vide tant que non fournie)
    # Colonnes par INDEX (-1 = aucune). C'est la forme qui fait foi : un index
    # survit à un intitulé changé, à un en-tête sur plusieurs lignes, et à deux
    # colonnes homonymes. Les clés `*_col` ci-dessous ne servent plus qu'à
    # relire une config antérieure (résolues en index au chargement).
    "xlsx_id_idx": -1,
    "xlsx_nom_idx": -1,
    "xlsx_prenom_idx": -1,
    "xlsx_data_start": 1,     # index de la 1re ligne de DONNÉES (en-tête au-dessus)
    "xlsx_id_col": "id_etudiant",       # legacy : intitulés (repli si *_idx = -1)
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
    "question_floor": None,   # plancher par question (points barème) ; None = aucun
    "question_ceiling": None, # plafond par question (points barème) ; None = aucun
    "total_floor": None,      # plancher du total QCM brut (points barème) ; None = aucun
    "show_score_range": False,# imprime « entre LO et HI pt » sous chaque QCM (compile)
    # --- IA (édition assistée de questions) --------------------------------
    "anthropic_api_key": "", # clé sk-ant-… stockée dans config.json du projet
    "ai_model":          "claude-sonnet-4-6",   # ou claude-opus-4-7
    # --- Banque(s) de questions (multi-banques, locales et/ou online) ------
    # `banks` est un dict {slug: entry}. Chaque entry est :
    #   {name: str, type: 'local'|'online',
    #    path: str (local) | supabase_url/supabase_anon_key/user_token/...
    #    refresh_token/user_id/user_email/token_expires_at (online)}
    # `active_bank` = slug de la banque actuellement sélectionnée.
    "active_bank":            "default",
    "banks":                  {},
    # --- Legacy V1 (single bank) — conservées pour rollback ---------------
    # Lors du 1er load après migration, _migrate_banks() recopie ces valeurs
    # dans `banks["default"]`. Les nouvelles écritures vont dans `banks[active]`
    # via `update_active_bank()`.
    "bank_mode":              "local",
    "bank_supabase_url":      "",
    "bank_supabase_anon_key": "",
    "bank_user_token":        "",
    "bank_refresh_token":     "",
    "bank_user_id":           "",
    "bank_user_email":        "",
    "bank_token_expires_at":  0,
}


# Path local par défaut quand aucune banque n'est configurée (V1 historique).
_DEFAULT_LOCAL_BANK_PATH = Path.home() / "Documents" / "AMCx-banque"


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
    """Retourne la config courante (DEFAULTS complétés par config.json si présent).

    Applique systématiquement la migration banques V1 → V2 (in-memory ; ne
    réécrit pas le disque tant que `save_config()` n'est pas appelé).
    """
    cfg = dict(DEFAULTS)
    path = _config_path()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return _migrate_banks(cfg)


# --------------------------------------------------------------------------
# Banques V1 → V2 : migration auto + helpers
# --------------------------------------------------------------------------

def _migrate_banks(cfg: dict) -> dict:
    """Si `banks` est vide, crée `banks['default']` depuis les anciennes clés
    flat (`bank_mode`, `bank_supabase_url`, …) ou le défaut `~/Documents/AMCx-banque`.

    Mutateur, in-memory : ne touche pas config.json (la mutation est persistée
    au prochain `save_config()`, qui purge alors les clés flat obsolètes —
    on les garde pour l'instant en DEFAULTS pour permettre un rollback en
    relançant l'ancienne version d'AMCx).
    """
    if cfg.get("banks"):
        return cfg
    mode = cfg.get("bank_mode") or "local"
    if mode == "online" and cfg.get("bank_supabase_url"):
        entry: dict = {"name": "Banque par défaut", "type": "online"}
        for k_old, k_new in [
            ("bank_supabase_url",      "supabase_url"),
            ("bank_supabase_anon_key", "supabase_anon_key"),
            ("bank_user_token",        "user_token"),
            ("bank_refresh_token",     "refresh_token"),
            ("bank_user_id",           "user_id"),
            ("bank_user_email",        "user_email"),
            ("bank_token_expires_at",  "token_expires_at"),
        ]:
            v = cfg.get(k_old)
            if v not in (None, "", 0):
                entry[k_new] = v
    else:
        env_path = os.environ.get("AMCX_BANK_DIR")
        path = (Path(env_path).expanduser().resolve() if env_path
                else _DEFAULT_LOCAL_BANK_PATH)
        entry = {"name": "Banque par défaut", "type": "local", "path": str(path)}
    cfg["banks"] = {"default": entry}
    if not cfg.get("active_bank"):
        cfg["active_bank"] = "default"
    return cfg


def active_bank_slug() -> str:
    """Slug de la banque active (cf. `active_bank_cfg()`)."""
    cfg = load_config()
    banks = cfg.get("banks") or {}
    slug = cfg.get("active_bank") or ""
    if slug in banks:
        return slug
    return next(iter(banks), "default")


def active_bank_cfg() -> dict:
    """Retourne le dict de config de la banque active.

    Fallback minimal local si aucune banque n'est définie."""
    cfg = load_config()
    banks = cfg.get("banks") or {}
    slug = cfg.get("active_bank") or next(iter(banks), "")
    entry = banks.get(slug) if slug else None
    if entry:
        return dict(entry)
    return {"name": "Banque par défaut", "type": "local",
            "path": str(_DEFAULT_LOCAL_BANK_PATH)}


def update_active_bank(updates: dict) -> dict:
    """Patch le dict de la banque active dans config.json (sans toucher
    aux autres banques). Retourne la config complète après écriture."""
    with _cfg_lock:
        cfg = load_config()
        banks = dict(cfg.get("banks") or {})
        slug = cfg.get("active_bank") or next(iter(banks), "default")
        entry = dict(banks.get(slug) or {})
        entry.update(updates)
        banks[slug] = entry
        return save_config({"banks": banks})


# Verrou des écritures de config : Flask sert en multi-thread et `save_config`
# fait un load-modify-write (le pipeline peut sauver `scan_pdfs_excluded`
# pendant que l'UI sauve les réglages du dashboard → une écriture perdue).
# RLock car `update_active_bank` enveloppe déjà un `save_config`.
_cfg_lock = RLock()


def write_json_atomic(path, data, *, indent: int = 2) -> None:
    """Écrit un JSON de façon atomique : fichier temporaire + `os.replace`.

    Une écriture directe (`open(w)` + `json.dump`) laisse un fichier tronqué si
    le process meurt en plein dump — or `raw_responses/` est la source de vérité
    de la relecture utilisateur, et `project_state` fait un `os._exit(0)` brutal
    au changement de projet. Le temporaire est créé dans le dossier cible pour
    que `os.replace` reste atomique (même système de fichiers).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_config(updates: dict) -> dict:
    """Fusionne `updates` dans la config et écrit config.json. Retourne la config complète.

    Ne persiste que les clés présentes dans DEFAULTS (les clés obsolètes sont
    purgées au prochain enregistrement)."""
    with _cfg_lock:
        cfg = load_config()
        for k, v in updates.items():
            if k in DEFAULTS:
                cfg[k] = v
        cfg = {k: v for k, v in cfg.items() if k in DEFAULTS}
        write_json_atomic(_config_path(), cfg)
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
