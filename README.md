# AMCx — éditeur QCM + correction automatique

**AMCx = AMC eXtended.** Crée des sujets QCM dans une interface web, imprime,
distribue, scanne, puis laisse la correction se faire toute seule (OpenCV + ML).
Pas besoin du logiciel `auto-multiple-choice` — seul `pdflatex` est utilisé.

---

## Installation

### Prérequis

| | Pourquoi | Comment |
|---|---|---|
| **Python 3.10+** | tout le pipeline | [python.org/downloads](https://www.python.org/downloads/) — **Windows : cocher « Add python.exe to PATH »** pendant l'installation |
| **pdflatex** | générer le PDF du sujet | Ubuntu/Debian : `sudo apt install texlive-latex-extra texlive-lang-french` · macOS : `brew install --cask basictex` (~100 Mo ; MacTeX pèse 5 Go) · Windows : [MiKTeX](https://miktex.org/download) |

Le style AMC (`automultiplechoice.sty`) est **fourni avec AMCx** : il n'est pas
sur CTAN, donc ni MiKTeX ni MacTeX ne sauraient l'installer. Les autres paquets
LaTeX manquants sont téléchargés automatiquement par MiKTeX à la première
compilation.

> **Sans pdflatex, AMCx fonctionne quand même** pour scanner et corriger un
> projet **déjà compilé** que quelqu'un t'a transmis. Seule la *création* d'un
> sujet exige TeX.

### Installation

**Avec git** (Linux, macOS, Windows) :

```bash
git clone https://github.com/epilliat/amcx.git
cd amcx
./install.sh          # Linux / macOS
install.bat           # Windows : double-clic dans l'explorateur
```

**Sans git** (Windows, le plus simple) : sur
[github.com/epilliat/amcx](https://github.com/epilliat/amcx), bouton vert
**Code → Download ZIP**, décompresser, puis double-cliquer sur `install.bat`.
⚠ Dans ce cas `update.bat` ne fonctionnera pas — il faudra retélécharger le ZIP
pour mettre à jour.

Le script crée `.venv/`, installe les dépendances et termine par un
**diagnostic**. Lancer ensuite :

```bash
./run.sh              # Linux / macOS
run.bat               # Windows : double-clic
```

puis ouvrir <http://localhost:5050/>.

### En cas de problème

```bash
.venv/bin/python auto_grading/doctor.py         # Linux / macOS
.venv\Scripts\python auto_grading\doctor.py     # Windows
```

Ou la page **`/diagnostic`** dans l'interface, qui a un bouton « 📋 Copier le
rapport ». Elle contrôle Python, les dépendances, `pdflatex`, le style AMC, la
compatibilité du modèle de correction, les chemins et la cohérence
sujet ↔ calage. **En cas de souci, envoyer ce rapport** plutôt que « ça ne
marche pas ».

### Mettre à jour

```bash
./update.sh           # Linux / macOS
update.bat            # Windows
```

Le script récupère la dernière version, réinstalle les dépendances et relance
le diagnostic. Il **refuse de tourner** si tu as modifié des fichiers du dépôt,
pour ne rien écraser.

**Tes données ne sont jamais touchées par une mise à jour** : sujets, copies et
notes vivent dans `~/Documents/AMCx/`, en dehors du dépôt.

---

## Cycle d'utilisation

### 1. Créer un projet

Au premier lancement, l'écran d'accueil propose **➕ Créer un nouveau projet** :
template *Examen minimal*, ou import d'un `.tex` AMC existant. Le projet est
créé dans `~/Documents/AMCx/<nom>/` :

```
~/Documents/AMCx/<nom>/
├── projet/                 ← déposer ici les PDF des copies scannées
└── auto_grading/
    ├── config.json
    ├── sujet/              ← exam.tex, subject.json, DOC-sujet.pdf, exam.xy
    ├── pages/              ← images extraites des scans (généré)
    └── raw_responses/      ← ⭐ LES NOTES ET CORRECTIONS (à sauvegarder !)
```

### 2. Écrire le sujet

Onglet **Sujet** : questions à choix unique ou multiple, questions ouvertes,
barème par réponse, en-tête, feuille de réponses, randomisation multi-copies.
Aperçu LaTeX + maths en direct. Les questions peuvent venir d'une
[banque partagée](#banque-de-questions-optionnelle).

### 3. Compiler et imprimer

Bouton **⚙ Compiler** de l'onglet Sujet → produit le PDF *et* le calage
(positions des cases). Imprimer ce PDF, faire passer l'examen.

> ⚠ **Ne pas recompiler après avoir imprimé.** Le calage doit provenir de la
> même compilation que les copies distribuées, sinon la lecture des cases sera
> décalée.

### 4. Charger la liste des étudiants

Dashboard → **Liste étudiants** → charger un `.xlsx` contenant au minimum les
colonnes `id_etudiant`, `nom`, `prenom_etat_civil` (noms configurables dans les
Réglages). Sans cette liste, les copies sont corrigées mais **restent anonymes**
et l'export n'aura pas les noms.

### 5. Scanner et corriger

Scanner les copies en PDF, puis **au choix** :

- déposer les fichiers dans `~/Documents/AMCx/<nom>/**projet/**` ;
- ou, plus simple, dashboard → **« + Ajouter un PDF de copies »**.

Puis **⚙ Traiter les scans** : extraction des pages, lecture des cases
(OpenCV + classifieur), préparation de la relecture. Ensuite :

- **Review rapide** — passer en revue les cases douteuses signalées ;
- **Identités** — relier les copies aux étudiants (glisser-déposer, ou 🪄
  auto-détection du nom manuscrit si une clé API Claude est configurée) ;
- **Dashboard** — notes, histogrammes, seuils, moyenne pondérée ;
- **Questions** — taux de réussite par question.

### 6. Exporter

**⤓ Export CSV** dans la barre du haut, ou « Sauvegarder le compte rendu »
(dossier `compte_rendu/` : notes + graphiques).

### Sauvegarder son travail

Le dossier à sauvegarder est **`~/Documents/AMCx/<nom>/`** en entier. Le
sous-dossier **`auto_grading/raw_responses/`** est la source de vérité : il
contient toutes les réponses lues *et* toutes tes corrections manuelles. Le
reste (`pages/`) est régénérable depuis les PDF.

---

## Multi-projets

Un seul projet actif à la fois. Switch via le **menu déroulant** à côté du brand
*AMCx* en haut à gauche. Pointeur d'état dans `~/.config/amcx/active_project`,
récents dans `~/.config/amcx/recent.json`.

Pour lancer sur un projet précis (utile en dev) :

```bash
AMCX_PROJECT_DIR=/chemin/vers/projet/auto_grading ./run.sh
```

> AMCx est prévu pour une **installation par poste**. Le projet actif est global
> et il n'y a pas d'authentification : une instance partagée entre plusieurs
> personnes servirait le même examen à tout le monde.

---

## Édition assistée par IA (optionnelle)

Bouton 🤖 sur chaque question de l'onglet *Sujet* pour la modifier via Claude
(reformuler, ajouter des distracteurs, générer des questions voisines). Deux
modes d'authentification :

- **Clé API Anthropic** : Réglages → IA → coller la clé `sk-ant-…` ;
- **Abonnement Claude Code** : si le binaire `claude` est dans le PATH, AMCx
  l'utilise en repli (consomme du quota d'abonnement, pas de l'argent).

La même clé alimente le 🪄 d'auto-détection des identités (onglet *Identités*)
et la lecture des réponses libres manuscrites.

---

## Banque de questions (optionnelle)

Onglet **Banque** : réutiliser une question d'un examen à l'autre sans
copier-coller de LaTeX, avec les statistiques de réussite accumulées.

Par défaut la banque est **locale** (`~/Documents/AMCx-banque/`). Le menu
déroulant en haut de l'onglet permet d'en gérer plusieurs et d'en **ajouter**
une en ligne (bouton « + Ajouter » → type *online* → URL Supabase + clé anon),
pour partager entre collègues :

- créer un projet Supabase (gratuit) — pas-à-pas dans
  [supabase/README.md](supabase/README.md) ;
- se connecter depuis l'onglet Banque (code à 6 chiffres reçu par email) ;
- les questions sauvées sont en `draft` (privées) jusqu'à publication ;
- ⚠ **passer la banque en invite-only**, sinon un étudiant peut créer un compte
  et lire les bonnes réponses — voir supabase/README.md § 4.0.

Migrer une banque locale existante vers le en ligne :

```bash
python auto_grading/bank_migrate.py --also-patch-projects
```

---

## Documentation détaillée

Architecture, pipeline complet, formats des fichiers intermédiaires, pièges
techniques : [CLAUDE.md](CLAUDE.md).

## Licence

Code AMCx : **MIT**.

Le dépôt inclut `auto_grading/tex/automultiplechoice.sty` — © 2008-2025 Alexis
Bienvenüe, **GNU GPL v2 ou ultérieure**, redistribué à l'identique avec son
en-tête de licence. Ce fichier provient du projet
[Auto Multiple Choice](https://www.auto-multiple-choice.net/) ; il est fourni
ici parce qu'il n'est pas distribué par CTAN et qu'il est indispensable à la
compilation des sujets. Voir [auto_grading/tex/README.md](auto_grading/tex/README.md).
