#!/usr/bin/env sh
# Installation d'AMCx en une commande — Linux et macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/epilliat/amcx/main/bootstrap.sh | sh
#
# Ne demande AUCUN prérequis : si Python manque, uv l'installe. Rien n'est posé
# en dehors du dossier personnel, aucun droit administrateur n'est requis.
# Pour installer une branche précise : AMCX_REF=ma-branche ... | sh
set -eu

REPO="https://github.com/epilliat/amcx"
REF="${AMCX_REF:-main}"

echo ""
echo "AMCx — installation"
echo "==================="

# 1) uv : gestionnaire d'environnements Python (installe Python au besoin).
if command -v uv >/dev/null 2>&1; then
  echo "→ uv déjà présent ($(uv --version))"
else
  echo "→ Installation de uv (gestionnaire Python, dans ton dossier personnel)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv s'installe dans ~/.local/bin : le rendre utilisable dans CE script.
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH"
  done
  export PATH
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "✘ uv introuvable après installation. Ouvre un nouveau terminal et relance."
  exit 1
fi

# 2) AMCx, avec sa propre installation Python isolée.
echo "→ Installation d'AMCx depuis $REPO ($REF)…"
uv tool install --force "git+$REPO@$REF"

# 3) S'assurer que la commande sera trouvée dans les prochains terminaux.
uv tool update-shell >/dev/null 2>&1 || true

# 4) pdflatex : nécessaire seulement pour CRÉER un sujet.
if ! command -v pdflatex >/dev/null 2>&1; then
  echo ""
  echo "⚠ pdflatex n'est pas installé."
  echo "  AMCx fonctionne sans (correction de copies d'un projet déjà compilé),"
  echo "  mais la création d'un sujet en a besoin :"
  case "$(uname -s)" in
    Darwin) echo "    brew install --cask basictex     (~100 Mo)" ;;
    *)      echo "    sudo apt install texlive-latex-extra texlive-lang-french" ;;
  esac
  echo "  Le style AMC lui-même est fourni avec AMCx : rien d'autre à installer."
fi

echo ""
echo "→ Diagnostic…"
echo ""
"$(command -v amcx || echo "$HOME/.local/bin/amcx")" doctor || true

cat <<'MSG'

===================================================================
✓ AMCx est installé.

   amcx              démarre le serveur, puis ouvrir http://localhost:5050/
   amcx --version    version installée
   amcx doctor       diagnostic (à envoyer en cas de problème)
   amcx update       mise à jour

Si « amcx » n'est pas reconnu, ferme et rouvre ton terminal.
===================================================================
MSG
