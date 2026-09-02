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
  # Rendre uv utilisable DANS ce script, sans attendre un nouveau terminal.
  # L'installateur respecte UV_INSTALL_DIR ; sinon il pose uv dans
  # ~/.local/bin (ou ~/.cargo/bin sur d'anciennes versions).
  for d in "${UV_INSTALL_DIR:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -n "$d" ] && [ -x "$d/uv" ]; then
      PATH="$d:$PATH"
      break
    fi
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

# 3) Rendre `amcx` utilisable : maintenant (dans ce script) et plus tard
#    (nouveaux terminaux). uv pose l'exécutable dans UV_TOOL_BIN_DIR si défini,
#    sinon dans ~/.local/bin.
uv tool update-shell >/dev/null 2>&1 || true
AMCX_BIN=""
for d in "${UV_TOOL_BIN_DIR:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
  if [ -n "$d" ] && [ -x "$d/amcx" ]; then
    AMCX_BIN="$d/amcx"
    PATH="$d:$PATH"
    export PATH
    break
  fi
done
[ -z "$AMCX_BIN" ] && AMCX_BIN="$(command -v amcx 2>/dev/null || true)"

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
if [ -n "$AMCX_BIN" ]; then
  echo "→ Diagnostic…"
  echo ""
  "$AMCX_BIN" doctor || true
else
  echo "⚠ La commande amcx n'a pas été trouvée après installation."
  echo "  Ouvre un nouveau terminal puis lance : amcx doctor"
fi

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
