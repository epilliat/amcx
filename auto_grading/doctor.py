"""Diagnostic d'installation AMCx.

Sert au support à distance : quand un collègue dit « ça ne marche pas », lui
demander la sortie de cette commande évite trois allers-retours par mail.

    python -m auto_grading.doctor        # ou : python auto_grading/doctor.py

Chaque contrôle renvoie (statut, libellé, détail) avec statut ∈ {ok, warn, fail}.
`GET /api/doctor` renvoie la même chose en JSON, et `/diagnostic` l'affiche.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

OK, WARN, FAIL = "ok", "warn", "fail"


def _pdflatex() -> tuple[str, str, str]:
    exe = shutil.which("pdflatex")
    if not exe:
        return (FAIL, "pdflatex", "introuvable — installer MiKTeX (Windows), "
                "BasicTeX (macOS) ou texlive-latex-extra (Linux). "
                "Sans lui : aucun sujet compilable.")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=20).stdout.splitlines()
        ver = out[0].strip() if out else "version inconnue"
    except (OSError, subprocess.SubprocessError) as e:
        return (WARN, "pdflatex", f"{exe} — version illisible ({e})")
    return (OK, "pdflatex", f"{ver}  [{exe}]")


def _amc_sty() -> tuple[str, str, str]:
    """Le style AMC vendorisé — le point de panne n°1 hors Debian."""
    try:
        from sujet_store import AMC_STY
    except Exception:                                   # noqa: BLE001
        AMC_STY = _HERE / "tex" / "automultiplechoice.sty"
    if not AMC_STY.exists():
        return (FAIL, "automultiplechoice.sty",
                f"absent de {AMC_STY} — le style AMC n'est PAS sur CTAN, ni "
                "MiKTeX ni MacTeX ne peuvent l'installer. Réinstaller AMCx.")
    import re
    ver = "version inconnue"
    try:
        head = AMC_STY.read_text(encoding="utf-8", errors="replace")[:8000]
        m = re.search(r"\\def\\AMC@VERSION\{([^}]*)\}", head)
        if m:
            ver = m.group(1)
    except OSError:
        pass
    return (OK, "automultiplechoice.sty", f"{ver}  [vendorisé]")


def _sklearn_vs_model() -> tuple[str, str, str]:
    """Un pickle sklearn n'est pas garanti compatible entre versions mineures."""
    try:
        import sklearn
    except ImportError:
        return (FAIL, "scikit-learn", "non installé — `pip install -e .`")
    model = _HERE / "models" / "cell_clf_full.pkl"
    if not model.exists():
        return (FAIL, "modèle de correction",
                f"{model} absent — la détection des cases cochées ne peut pas "
                "fonctionner.")
    try:
        import warnings
        import joblib
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            joblib.load(model)
        bad = [w for w in caught if "version" in str(w.message).lower()]
        if bad:
            return (WARN, "scikit-learn",
                    f"{sklearn.__version__} — le modèle a été entraîné avec une "
                    f"autre version : {bad[0].message}")
    except Exception as e:                              # noqa: BLE001
        return (FAIL, "scikit-learn",
                f"{sklearn.__version__} — chargement du modèle impossible : {e}")
    return (OK, "scikit-learn", f"{sklearn.__version__} — modèle chargé")


def _python() -> tuple[str, str, str]:
    v = sys.version_info
    status = OK if (v.major, v.minor) >= (3, 10) else FAIL
    return (status, "Python",
            f"{platform.python_version()}  [{sys.executable}]"
            + ("" if status == OK else "  — 3.10+ requis"))


def _deps() -> tuple[str, str, str]:
    missing = []
    for mod, label in (("cv2", "opencv-python-headless"), ("fitz", "PyMuPDF"),
                       ("numpy", "numpy"), ("flask", "flask"),
                       ("openpyxl", "openpyxl"), ("PIL", "pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        return (FAIL, "dépendances Python",
                "manquantes : " + ", ".join(missing) + " — `pip install -e .`")
    return (OK, "dépendances Python", "toutes présentes")


def _project() -> tuple[str, str, str]:
    try:
        import config
        import project_state
    except Exception as e:                              # noqa: BLE001
        return (FAIL, "projet actif", f"config illisible : {e}")
    root = config.project_root()
    if not (root / "sujet").exists():
        return (WARN, "projet actif",
                f"{root} — pas de sujet/ : créer un projet depuis l'accueil.")
    bits = []
    for name, path in (("exam.tex", root / "sujet" / "exam.tex"),
                       ("subject.json", root / "sujet" / "subject.json"),
                       ("exam.xy (calage)", root / "sujet" / "exam.xy")):
        bits.append(f"{name}: {'oui' if path.exists() else 'non'}")
    status = OK if (root / "sujet" / "exam.xy").exists() else WARN
    return (status, "projet actif",
            f"{root}\n    " + " · ".join(bits)
            + ("" if status == OK else "\n    (pas de calage : compiler le sujet)"))


def _layout_consistency() -> tuple[str, str, str]:
    try:
        from sujet_store import check_layout_consistency
        issues = check_layout_consistency(verbose=False)
    except Exception as e:                              # noqa: BLE001
        return (WARN, "cohérence sujet/calage", f"non vérifiable : {e}")
    if issues:
        return (WARN, "cohérence sujet/calage",
                "\n    ".join(issues) + "\n    → recompiler le sujet")
    return (OK, "cohérence sujet/calage", "sujet et calage concordent")


def _paths() -> tuple[str, str, str]:
    try:
        import project_state
        state = project_state.STATE_DIR
        projects = project_state.DEFAULT_PROJECTS_ROOT
    except Exception as e:                              # noqa: BLE001
        return (WARN, "chemins", str(e))
    return (OK, "chemins",
            f"état: {state}\n    projets: {projects}"
            f"\n    AMCX_PROJECT_DIR: {os.environ.get('AMCX_PROJECT_DIR') or '(non défini)'}")


def _system() -> tuple[str, str, str]:
    return (OK, "système",
            f"{platform.system()} {platform.release()} ({platform.machine()})")


CHECKS = (_system, _python, _deps, _pdflatex, _amc_sty, _sklearn_vs_model,
          _paths, _project, _layout_consistency)


def run_checks() -> list[dict]:
    """Exécute tous les contrôles. Ne lève jamais."""
    out = []
    for fn in CHECKS:
        try:
            status, label, detail = fn()
        except Exception as e:                          # noqa: BLE001
            status, label, detail = FAIL, fn.__name__, f"contrôle en échec : {e}"
        out.append({"status": status, "label": label, "detail": detail})
    return out


def main() -> int:
    icon = {OK: "✔", WARN: "!", FAIL: "✘"}
    results = run_checks()
    print("AMCx — diagnostic d'installation\n" + "=" * 34)
    for r in results:
        print(f"  {icon[r['status']]} {r['label']:24s} {r['detail']}")
    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_warn = sum(1 for r in results if r["status"] == WARN)
    print("=" * 34)
    if n_fail:
        print(f"{n_fail} problème(s) bloquant(s), {n_warn} avertissement(s).")
    elif n_warn:
        print(f"Installation utilisable — {n_warn} avertissement(s).")
    else:
        print("Installation complète et cohérente.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
