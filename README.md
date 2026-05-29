# AMCx — éditeur QCM + correction automatique

**AMCx = AMC eXtended.** Crée des sujets QCM dans une interface web, imprime,
distribue, scanne, puis laisse la correction se faire toute seule (OpenCV + ML).
Pas besoin du logiciel `auto-multiple-choice` — seul `pdflatex` est utilisé.

## Installation

### Prérequis

- **Python 3.10+** ([python.org/downloads](https://www.python.org/downloads/))
- **pdflatex** (pour générer le PDF du sujet) :
  - Ubuntu / Debian : `sudo apt install texlive-latex-extra texlive-lang-french`
  - macOS : `brew install --cask mactex-no-gui`
  - Windows : [MiKTeX](https://miktex.org/download)

### Installation en 3 commandes

```bash
git clone https://github.com/epilliat/amcx.git
cd amcx
./install.sh        # Linux/macOS
# Windows : double-clic sur install.bat
```

Lance ensuite :

```bash
./run.sh            # Linux/macOS
# Windows : double-clic sur run.bat
```

Ouvre [http://localhost:5050/](http://localhost:5050/). Au premier lancement,
l'écran d'accueil te propose de **créer un nouveau projet** (template *Examen
minimal* ou import d'un `.tex` AMC existant). Les projets sont rangés par
défaut dans `~/Documents/AMCx/<nom>/`.

## Cycle d'utilisation

1. **Créer** un projet (template ou import AMC).
2. **Éditer le sujet** dans l'onglet *Sujet* (questions, barème, randomisation
   multi-copies, en-tête, feuille de réponses) ; aperçu LaTeX live.
3. **Compiler** le PDF (bouton ⚙ de l'onglet Sujet).
4. **Imprimer, distribuer, scanner**. Déposer les PDF scannés dans le dossier
   du projet.
5. **Corriger** : « ⚙ Traiter les scans » (pipeline OpenCV + GBM), relire les
   cas ambigus dans le dashboard, valider les identités.
6. **Exporter** le CSV final ou sauvegarder un compte rendu visuel.

## Multi-projets

Un seul projet actif à la fois. Switch via le **menu déroulant** à côté du
brand *AMCx* en haut à gauche. Pointeur d'état dans
`~/.config/amcx/active_project`. Liste des projets récents dans
`~/.config/amcx/recent.json`.

Pour lancer sur un projet spécifique (utile en dev) :

```bash
AMCX_PROJECT_DIR=/chemin/vers/projet \
  ./run.sh
```

## Édition assistée par IA (optionnelle)

L'onglet *Sujet* propose un bouton 🤖 sur chaque question pour la modifier via
Claude. Deux modes d'auth :

- **Clé API Anthropic** : Réglages → IA → coller la clé `sk-ant-…`.
- **Claude Code subscription** : si le binaire `claude` est dans le PATH,
  AMCx l'utilise comme fallback (consomme du quota d'abonnement, pas de l'argent).

L'IA est aussi utilisée par le bouton 🪄 d'auto-détection des identités
(onglet *Identités*).

## Documentation détaillée

Architecture, pipeline complet, formats des fichiers intermédiaires, pièges
techniques : voir [CLAUDE.md](CLAUDE.md).

## Licence

MIT.
