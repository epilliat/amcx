#!/usr/bin/env bash
# Installe AMCx dans un venv local (.venv/) et indique comment lancer.
set -e

cd "$(dirname "$0")"

# 1) Python 3.10+
if ! command -v python3 >/dev/null; then
  echo "✘ Python 3 introuvable. Installer Python 3.10 ou plus récent :"
  echo "   Ubuntu/Debian : sudo apt install python3 python3-venv"
  echo "   macOS         : brew install python@3.11"
  echo "   Windows       : https://www.python.org/downloads/"
  exit 1
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "✘ Python ${PY_MAJOR}.${PY_MINOR} détecté. AMCx exige Python 3.10+."
  exit 1
fi

# 2) pdflatex (avertissement, pas bloquant — on peut éditer le sujet sans compiler)
if ! command -v pdflatex >/dev/null; then
  cat <<'EOF'
⚠ pdflatex introuvable.
   L'UI fonctionnera (édition, dashboard, correction des copies déjà scannées)
   mais le bouton « Compiler » de l'onglet Sujet sera en erreur.
   Pour l'installer :
     Ubuntu/Debian : sudo apt install texlive-latex-extra texlive-lang-french
     macOS         : brew install --cask basictex   (~100 Mo ; MacTeX = 5 Go)
     Windows       : https://miktex.org/download

   Le style AMC (automultiplechoice.sty) est fourni avec AMCx — rien d'autre
   à installer. Les paquets LaTeX manquants (tikz, hyperref…) sont des paquets
   standards que MiKTeX installe automatiquement à la première compilation.

EOF
fi

# 3) venv + install editable
echo "→ Création du venv (.venv/)…"
python3 -m venv .venv

echo "→ Installation des dépendances…"
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e .

echo ""
echo "→ Diagnostic de l'installation…"
echo ""
.venv/bin/python auto_grading/doctor.py || true

echo ""
echo "✓ Installé. Pour lancer le serveur :"
echo "    ./run.sh"
echo ""
echo "  Puis ouvrir http://localhost:5050/ dans le navigateur."
echo "  En cas de souci : page /diagnostic, ou .venv/bin/python auto_grading/doctor.py"
