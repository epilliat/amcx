#!/usr/bin/env bash
# Met à jour AMCx : récupère la dernière version et réinstalle les dépendances.
# Ne touche à AUCUN projet : tes sujets, copies et notes vivent hors du dépôt
# (~/Documents/AMCx/), ils ne sont jamais écrasés par une mise à jour.
set -e

cd "$(dirname "$0")"

if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ Le dépôt contient des modifications locales :"
  git status --short
  echo ""
  echo "  La mise à jour est annulée pour ne rien écraser."
  echo "  Pour les abandonner : git checkout -- . && ./update.sh"
  exit 1
fi

echo "→ Récupération de la dernière version…"
git pull --ff-only

echo "→ Mise à jour des dépendances…"
.venv/bin/pip install -e . --upgrade-strategy only-if-needed

echo ""
echo "→ Diagnostic…"
.venv/bin/python auto_grading/doctor.py || true

echo ""
echo "✓ À jour. Relancer le serveur : ./run.sh"
