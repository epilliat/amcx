"""État global du logiciel AMCx : projet actif et liste des projets récents.

Un *projet AMCx* = un dossier sur disque qui contient au moins :
  - `sujet/exam.tex`    (source de vérité du sujet)
  - `config.json`       (config runtime, créée au 1er save)

Le projet **actif** est désigné par un fichier texte global :
  `~/.config/amcx/active_project`  (contient un chemin absolu).

La liste des projets **récents** est dans :
  `~/.config/amcx/recent.json`     (liste d'objets `{path, name, last_opened}`).

Ce module ne dépend ni de `config.py` ni de `front/`, pour éviter les cycles —
c'est `config.project_root()` qui appelle `active_project()` ici, pas l'inverse.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

APP_NAME = "AMCx"

STATE_DIR = Path.home() / ".config" / "amcx"
ACTIVE_FILE = STATE_DIR / "active_project"
RECENT_FILE = STATE_DIR / "recent.json"

DEFAULT_PROJECTS_ROOT = Path.home() / "Documents" / "AMCx"


# --- état global ------------------------------------------------------------

def ensure_state_dir() -> Path:
    """Crée `~/.config/amcx/` au besoin et retourne le chemin."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def ensure_default_root() -> Path:
    """Crée `~/Documents/AMCx/` au besoin et retourne le chemin."""
    DEFAULT_PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    return DEFAULT_PROJECTS_ROOT


def active_project() -> Path | None:
    """Chemin absolu du projet actif (ou None si non défini / invalide).

    Précédence : variable d'env `AMCX_PROJECT_DIR` > fichier `ACTIVE_FILE`.
    Aucun fallback ici vers le dossier d'installation — c'est `config.project_root()`
    qui le gère.
    """
    env = os.environ.get("AMCX_PROJECT_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.exists() else None
    if not ACTIVE_FILE.exists():
        return None
    try:
        raw = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.exists() else None


def is_valid_project(path: Path | None) -> bool:
    """Un dossier est un projet AMCx s'il contient `sujet/exam.tex`."""
    if not path:
        return False
    p = Path(path)
    return (p / "sujet" / "exam.tex").exists()


def display_name(path: Path) -> str:
    """Nom lisible d'un projet — masque le sous-dossier technique `auto_grading/`.

    Pour cette itération, `new_project.py` crée la structure
    `<projet>/auto_grading/{sujet,…}`. Le projet *vu* par l'utilisateur est le
    parent — c'est lui qu'on affiche.
    """
    p = Path(path)
    if p.name == "auto_grading":
        return p.parent.name
    return p.name


def set_active_project(path: Path) -> None:
    """Écrit le pointeur actif et met à jour `recent.json`."""
    ensure_state_dir()
    p = Path(path).expanduser().resolve()
    ACTIVE_FILE.write_text(str(p), encoding="utf-8")
    _touch_recent(p)


def clear_active_project() -> None:
    """Supprime le pointeur actif (utilisé pour repasser à l'onboarding)."""
    if ACTIVE_FILE.exists():
        ACTIVE_FILE.unlink()


# --- récents ---------------------------------------------------------------

def _load_recent() -> list[dict]:
    if not RECENT_FILE.exists():
        return []
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and "path" in e]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_recent(entries: list[dict]) -> None:
    ensure_state_dir()
    with open(RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _touch_recent(path: Path) -> None:
    """Pousse `path` en tête de la liste des récents (dedup)."""
    entries = [e for e in _load_recent() if Path(e["path"]) != path]
    entries.insert(0, {
        "path": str(path),
        "name": display_name(path),
        "last_opened": datetime.now().isoformat(timespec="seconds"),
    })
    _save_recent(entries[:20])


def recent_projects() -> list[dict]:
    """Liste des projets récents (filtre ceux qui n'existent plus)."""
    out: list[dict] = []
    for e in _load_recent():
        p = Path(e["path"])
        if is_valid_project(p):
            out.append({
                "path": str(p),
                "name": e.get("name") or display_name(p),
                "last_opened": e.get("last_opened", ""),
            })
    return out


def forget_project(path: Path) -> None:
    p = Path(path).expanduser().resolve()
    entries = [e for e in _load_recent() if Path(e["path"]) != p]
    _save_recent(entries)


def discover_projects(root: Path | None = None) -> list[dict]:
    """Liste les dossiers du `root` (défaut: `~/Documents/AMCx/`).

    Chaque entrée : `{path, name, complete, dir}` où :
      - `path` : chemin du `auto_grading/` à ouvrir si `complete=True`.
      - `name` : nom du sous-dossier visible par l'utilisateur.
      - `complete` : True si `sujet/exam.tex` est présent (projet ouvrable).
      - `dir` : chemin du dossier parent (utile pour la suppression).

    Les entrées incomplètes restent listées pour que l'utilisateur puisse les
    supprimer depuis l'UI (sans avoir à passer par le file manager).
    """
    if root is None:
        root = DEFAULT_PROJECTS_ROOT
    if not root.exists() or not root.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        complete_path = None
        for cand in (entry / "auto_grading", entry):
            if is_valid_project(cand):
                complete_path = cand
                break
        out.append({
            "path": str(complete_path) if complete_path else str(entry),
            "name": entry.name,
            "complete": complete_path is not None,
            "dir": str(entry),
        })
    return out


def delete_folder_under_root(path: Path, root: Path | None = None) -> None:
    """Supprime récursivement `path`, en exigeant qu'il soit DANS `root`.

    Garde-fou de sécurité : on refuse de supprimer un chemin hors de
    `~/Documents/AMCx/` — c'est conçu pour le bouton « ✕ » des projets
    incomplets dans la modale Ouvrir, pas pour un usage général.
    """
    import shutil
    if root is None:
        root = DEFAULT_PROJECTS_ROOT
    root = Path(root).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(
            f"Refus : {target} n'est pas dans {root}.")
    if target == root:
        raise PermissionError(f"Refus : ne supprime pas {root} lui-même.")
    if not target.exists():
        return
    shutil.rmtree(target)


# --- restart ---------------------------------------------------------------

def restart_server_with_project(new_project: Path) -> NoReturn:
    """Définit le projet actif puis relance le process Python avec le **même
    invocation** (`sys.executable` + `sys.argv`).

    On NE peut PAS faire `os.execv` directement : la socket d'écoute Flask reste
    héritée par le nouveau process, qui échoue alors avec `Address already in use`.
    À la place : on lance un sous-process détaché qui attend la libération du port
    de listening, puis exec le serveur — et le process courant s'arrête via
    `os._exit(0)` (ferme le socket → libère le port).
    """
    set_active_project(new_project)
    sys.stdout.flush(); sys.stderr.flush()
    _spawn_relauncher()
    os._exit(0)


def _spawn_relauncher() -> None:
    """Lance un sous-process détaché qui attend la libération du port puis
    relance la même commande Python.

    Le port est devisé depuis `sys.argv` (recherche `--port <N>`, défaut 5000)
    — c'est suffisant pour le serveur Flask AMCx (un seul argument utile).
    """
    import subprocess
    import shlex

    # Code Python que le watcher exécute (détaché).
    #
    # Le relancement doit marcher dans les deux modes de lancement :
    #   - `python auto_grading/front/server.py --port N`  → argv[0] est un .py,
    #     on relance via l'interpréteur ;
    #   - commande console `amcx --port N` (uv tool / pipx / pip) → argv[0] est
    #     un script généré, voire un .exe sous Windows : le passer à `python`
    #     échouerait. On ré-exécute alors argv[0] directement.
    code = (
        "import socket, sys, time, os\n"
        f"argv = {sys.argv!r}\n"
        f"exe = {sys.executable!r}\n"
        # Devine le port (--port N) ; défaut 5000.
        "port = 5000\n"
        "for i, a in enumerate(argv):\n"
        "    if a == '--port' and i + 1 < len(argv):\n"
        "        try: port = int(argv[i+1])\n"
        "        except: pass\n"
        # Attend que le port soit libéré (max ~8 s).
        "for _ in range(80):\n"
        "    try:\n"
        "        s = socket.socket(); s.bind(('127.0.0.1', port)); s.close(); break\n"
        "    except OSError:\n"
        "        time.sleep(0.1)\n"
        # Exec la commande d'origine — process unique, hérite stdio si parent en a.
        "prog = argv[0] if argv else ''\n"
        "if prog.endswith('.py'):\n"
        "    os.execv(exe, [exe] + argv)\n"
        "else:\n"
        "    os.execv(prog, argv)\n"
    )

    # Détacher le watcher du process courant, qui va mourir juste après.
    # `start_new_session` est POSIX seulement ; Windows a ses propres drapeaux.
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
