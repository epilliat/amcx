"""Commande `amcx` — point d'entrée console.

    amcx                 démarre le serveur (http://localhost:5050)
    amcx --port 5051     ... sur un autre port
    amcx --version       version installée
    amcx doctor          diagnostic d'installation
    amcx update          met à jour AMCx (détecte uv / pipx / clone git)
    amcx where           où vivent le code, les projets, la configuration

Sans sous-commande, tout argument est passé au serveur : `amcx --port 5051`
équivaut à l'ancien `python auto_grading/front/server.py --port 5051`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Les modules d'AMCx s'importent à plat (`import config`) : on garantit que le
# dossier d'installation est sur sys.path, quel que soit le répertoire courant
# depuis lequel la commande est lancée.
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE / "front")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def get_version() -> str:
    """Version installée. `importlib.metadata` d'abord (fait foi une fois
    installé), repli sur le module `_version` pour un lancement depuis un clone
    non installé."""
    try:
        from importlib.metadata import version
        return version("amcx")
    except Exception:                                   # noqa: BLE001
        try:
            from _version import __version__
            return f"{__version__}+local"
        except Exception:                               # noqa: BLE001
            return "inconnue"


# --------------------------------------------------------------------------
# Mise à jour
# --------------------------------------------------------------------------

def install_kind() -> tuple[str, Path]:
    """Comment AMCx a été installé : ('uv'|'pipx'|'git'|'venv', chemin).

    Détecté par les fichiers-marqueurs que les outils déposent à la racine de
    l'environnement — `uv-receipt.toml` pour uv, `pipx_metadata.json` pour
    pipx. Plus fiable qu'une reconnaissance du chemin, qui dépend de la
    configuration de l'utilisateur (UV_TOOL_DIR, PIPX_HOME…).
    """
    prefix = Path(sys.prefix).resolve()
    if (prefix / "uv-receipt.toml").exists():
        return ("uv", prefix)
    if (prefix / "pipx_metadata.json").exists() or (
            prefix.parent / "pipx_metadata.json").exists():
        return ("pipx", prefix)
    repo = _HERE.parent
    if (repo / ".git").exists():
        return ("git", repo)
    # Repli : reconnaissance par chemin, si un outil change ses marqueurs.
    parts = {x.lower() for x in prefix.parts}
    if "uv" in parts and "tools" in parts:
        return ("uv", prefix)
    if "pipx" in parts:
        return ("pipx", prefix)
    return ("venv", prefix)


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print("  $ " + " ".join(cmd))
    try:
        return subprocess.call(cmd, cwd=str(cwd) if cwd else None)
    except FileNotFoundError:
        print(f"  ✘ commande introuvable : {cmd[0]}")
        return 127


def cmd_update(_argv: list[str]) -> int:
    """Met à jour AMCx selon son mode d'installation."""
    kind, path = install_kind()
    print(f"AMCx {get_version()} — installation détectée : {kind}  ({path})\n")

    if kind == "uv":
        rc = _run([shutil.which("uv") or "uv", "tool", "upgrade", "amcx"])
        if rc != 0:
            print("\n  Si la mise à jour n'a rien trouvé, forcer la réinstallation :")
            print("    uv tool install --force git+https://github.com/epilliat/amcx")
        return rc

    if kind == "pipx":
        return _run([shutil.which("pipx") or "pipx", "upgrade", "amcx"])

    if kind == "git":
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(path),
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            print("  ✘ Le dépôt contient des modifications locales :")
            print("    " + dirty.replace("\n", "\n    "))
            print("\n  Mise à jour annulée pour ne rien écraser.")
            return 1
        if _run(["git", "pull", "--ff-only"], cwd=path) != 0:
            return 1
        return _run([sys.executable, "-m", "pip", "install", "-e", ".",
                     "--upgrade-strategy", "only-if-needed"], cwd=path)

    print("  Installation non reconnue (venv classique).")
    print("  Mettre à jour manuellement :")
    print(f"    {sys.executable} -m pip install --upgrade "
          "git+https://github.com/epilliat/amcx")
    return 1


# --------------------------------------------------------------------------
# Autres sous-commandes
# --------------------------------------------------------------------------

def cmd_doctor(_argv: list[str]) -> int:
    import doctor
    return doctor.main()


def cmd_where(_argv: list[str]) -> int:
    """Où vivent le code, les projets et la configuration."""
    import config
    import project_state
    kind, path = install_kind()
    print(f"AMCx {get_version()}")
    print(f"  code (installation) : {_HERE}")
    print(f"  mode d'installation : {kind}  ({path})")
    print(f"  configuration       : {project_state.STATE_DIR}")
    print(f"  dossier des projets : {project_state.DEFAULT_PROJECTS_ROOT}")
    print(f"  projet actif        : {config.project_root()}")
    return 0


def cmd_run(argv: list[str]) -> int:
    """Démarre le serveur Flask. `argv` est passé tel quel à server.main()."""
    import server
    sys.argv = [sys.argv[0]] + argv
    server.main()
    return 0


COMMANDS = {
    "run":    cmd_run,
    "doctor": cmd_doctor,
    "update": cmd_update,
    "where":  cmd_where,
}


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] in ("--version", "-V"):
        print(f"amcx {get_version()}")
        return 0

    if argv and argv[0] in ("--help", "-h", "help"):
        print(__doc__.strip())
        return 0

    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:]) or 0

    # Pas de sous-commande → serveur, avec les options éventuelles.
    return cmd_run(argv) or 0


if __name__ == "__main__":
    sys.exit(main())
