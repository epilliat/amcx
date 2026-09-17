# CLAUDE.md — AMCx (éditeur QCM + correction auto)

Notes pour un agent qui reprend le projet. Lis ce fichier en entier avant d'éditer quoi que ce soit.

**AMCx = AMC eXtended** : éditeur de sujet QCM (interface web) + correction automatique
des copies scannées (OpenCV + ML), sans dépendance au binaire `auto-multiple-choice`.
Seule la compilation `pdflatex` est utilisée.

## ⚠ Deux mots, deux niveaux — et le code porte les noms historiques

| interface | sur le disque | dans le code |
|---|---|---|
| une **évaluation** — un examen : son sujet, ses copies, ses notes | un dossier portant `sujet/exam.tex` | `project` — `project_state.py`, `config.project_root()`, `~/.config/amcx/active_project`, `/api/projects*`, `AMCX_PROJECT_DIR` |
| un **projet** — ce qui rassemble les évaluations d'une promotion et agrège leurs notes | un dossier portant `cohorte.json` | `cohort` — `cohort.py`, `~/.config/amcx/active_cohort`, `/api/cohorte*`, `AMCX_COHORT_DIR` |

C'est **volontaire et assumé** : renommer le code coûterait des centaines de
points d'appel, tous les tests et les `config.json` déjà écrits, pour un gain
nul à l'exécution — même raisonnement que `qcm_seuil`, dont le libellé est
devenu « normalisation » sans que la clé bouge. Quand ce fichier dit « projet »
dans une phrase qui parle de code (`project_state`, « projet actif »,
`new_project.py`), il désigne une **évaluation**.

⚠ **La racine du dossier de travail EST le projet actif** : même pointeur
(`active_cohort`). L'onglet Fichiers la parcourt, l'onglet Projet l'agrège.

## Contexte général

Pipeline **réutilisable** de correction automatique de QCM au **format AMC**
(`automultiplechoice` LaTeX), **sans dépendre du logiciel AMC** : seule la
compilation `pdflatex` est utilisée. Il sert à : créer un sujet → l'imprimer →
recevoir les copies scannées → les corriger via l'UI Flask.

Ce répertoire (`projet_modele/`) est une **copie généralisée** du pipeline d'origine
(qui restait câblé en dur sur un seul examen). Il est actuellement **configuré pour
re-corriger l'examen de test EXAM_2026** (QCM de 31 questions, 174 copies) afin de
valider la généralisation — voir `auto_grading/config.json` (`amc_dir`).

**Le dossier de l'examen est configurable** (`config.amc_dir`, défaut `../projet`) :
il contient les PDF des copies scannées et, pour un examen déjà préparé par AMC,
un sous-dossier `data/` avec les SQLite. Pour le test, `amc_dir` pointe sur
`../../EXAM_2026/` (lecture seule).

| Élément du dossier d'examen | Contenu |
|---|---|
| `<amc_dir>/*.pdf` | PDF des copies scannées (auto-découverts ; hors PDF compilés du sujet) |
| `<amc_dir>/data/layout.sqlite` | *(optionnel)* calage AMC — sinon dérivé du `.xy`, voir piège #1 |
| `<amc_dir>/data/capture.sqlite` | *(optionnel)* analyse AMC des scans (contrôle croisé) |
| `<amc_dir>/data/scoring.sqlite` | *(optionnel)* — non requis : barème lu dans `exam.tex` |
| liste étudiants (xlsx/csv) | `config.student_xlsx` + colonnes par **index** (`xlsx_*_idx`, `xlsx_data_start`) |

Le sujet vit dans `auto_grading/sujet/subject.json` (**source de vérité unique** —
voir *Store du sujet* plus bas ; `exam.tex` en est un **produit**, régénéré à la
compilation). Le
code vit dans [auto_grading/](auto_grading/). `pyproject.toml` est à la racine.

## Installation (Linux, macOS, Windows)

AMCx est du Python pur (wheels uniquement) + `pdflatex`. Aucun paquet système
au-delà de TeX, et **le binaire `auto-multiple-choice` n'est jamais requis**.

### Les 2 prérequis

| | Rôle | Installation |
|---|---|---|
| **Python 3.10+** | tout le pipeline | [python.org](https://www.python.org/downloads/) (Windows : cocher « Add python.exe to PATH ») |
| **pdflatex** | compiler le sujet (PDF + calage `.xy`) | Debian/Ubuntu : `sudo apt install texlive-latex-extra texlive-lang-french` · macOS : `brew install --cask basictex` (~100 Mo, préférer à MacTeX qui pèse 5 Go) · Windows : [MiKTeX](https://miktex.org/download) |

### Installation utilisateur : une ligne, zéro prérequis

```sh
curl -LsSf https://raw.githubusercontent.com/epilliat/amcx/main/bootstrap.sh | sh   # Linux/macOS
irm https://raw.githubusercontent.com/epilliat/amcx/main/bootstrap.ps1 | iex        # Windows
```

[bootstrap.sh](bootstrap.sh) / [bootstrap.ps1](bootstrap.ps1) installent `uv`
s'il manque (dossier personnel, sans droits admin), qui installe **Python si
besoin** — c'est ce qui rend l'installation possible sur un Windows nu — puis
`uv tool install git+…` pose AMCx dans un environnement isolé et met la commande
`amcx` sur le PATH.

### La commande `amcx`

`[project.scripts] amcx = "auto_grading.cli:main"` → [cli.py](auto_grading/cli.py) :

| | |
|---|---|
| `amcx` | démarre le serveur (tout argument non reconnu est passé à `server.main()`, donc `amcx --port 5051`) |
| `amcx --version` | version, source unique dans [_version.py](auto_grading/_version.py) (lue aussi par hatch, `dynamic = ["version"]`) |
| `amcx doctor` | délègue à [doctor.py](auto_grading/doctor.py) |
| `amcx update` | détecte le mode d'installation et lance la bonne commande |
| `amcx where` | chemins du code, des projets, de la config |
| `amcx results` | notes de l'examen (`--project P`, `--json`) — cf. *exam_results.py* |
| `amcx cohort` | notes agrégées d'un projet (`--dir D`, `--json`) — cf. *Onglet Projet* |

**Détection du mode d'installation** (`cli.install_kind()`) : par
**fichier-marqueur** à la racine de l'environnement — `uv-receipt.toml` (uv),
`pipx_metadata.json` (pipx), `.git` du dépôt (clone) — et non par
reconnaissance du chemin, qui casse dès que l'utilisateur configure
`UV_TOOL_DIR` ou `PIPX_HOME`. Repli sur le chemin si les marqueurs changent.

⚠ **Le relanceur doit rester compatible commande console.**
`project_state._spawn_relauncher()` re-exécute la commande d'origine au
changement de projet : si `sys.argv[0]` n'est pas un `.py` (cas d'un point
d'entrée console, et **`.exe` sous Windows**), il faut ré-exécuter `argv[0]`
directement et non le passer à `python`. Le détachement utilise
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` sous Windows,
`start_new_session` ailleurs (POSIX seulement).

### Installation pour développer

```bash
git clone https://github.com/epilliat/amcx.git
cd amcx
./install.sh          # Linux / macOS — Windows : install.bat
```

Le script crée `.venv/`, installe les dépendances, puis **lance le diagnostic**.
Lancer ensuite `./run.sh` (ou `run.bat`) et ouvrir <http://localhost:5050/>.
`./update.sh` fait `git pull` + dépendances.

### ⚠ `automultiplechoice.sty` est vendorisé — ne pas le retirer

Le style LaTeX d'AMC **n'est pas sur CTAN** (`ctan.org/pkg/automultiplechoice`
→ 404) : il est livré avec le logiciel AMC, donc empaqueté pour Debian/Ubuntu
seulement. **Ni MiKTeX ni MacTeX ne peuvent l'installer.** Sans lui, `pdflatex`
échoue sur `File 'automultiplechoice.sty' not found` → pas de PDF, pas de
calage, donc aucun sujet créable hors Debian.

Il est donc versionné dans [auto_grading/tex/](auto_grading/tex/) (v1.7.0,
GPL — en-tête de licence à conserver) et `compile_pdf()` le copie dans son
dossier temporaire de compilation. `pdflatex` cherche le répertoire courant en
premier : **cette copie prime sur une éventuelle installation AMC du système**,
ce qui est voulu — deux versions du style peuvent produire des positions de
cases différentes, donc un `.xy` différent, donc des copies imprimées qui ne
correspondent plus au calage. Panne silencieuse, découverte en corrigeant.

Toutes les *autres* dépendances du style (`tikz`, `hyperref`, `fancybox`,
`csvsimple`, `environ`, `storebox`, `rotating`, `xkeyval`…) sont des paquets
CTAN standards, que MiKTeX télécharge automatiquement à la première
compilation. Détails et procédure de mise à jour : [auto_grading/tex/README.md](auto_grading/tex/README.md).

### Diagnostic — le réflexe support

```bash
amcx doctor                                  # ou : page /diagnostic dans l'UI
.venv/bin/python auto_grading/doctor.py      # depuis un clone non installé
```

[doctor.py](auto_grading/doctor.py) contrôle : OS, version de Python,
dépendances, `pdflatex`, présence et version du style AMC vendorisé,
**compatibilité scikit-learn ↔ modèle picklé**, chemins résolus, projet actif,
et cohérence sujet ↔ calage. Chaque contrôle est `ok` / `warn` / `fail`, le
code de sortie vaut 1 s'il y a un `fail`.

Quand un collègue signale un problème, demander la sortie de cette commande
(ou le bouton « 📋 Copier le rapport » de `/diagnostic`) plutôt que d'engager
un échange de mails. Routes : `GET /diagnostic` (page), `GET /api/doctor` (JSON).

### Pièges connus par plateforme

- **scikit-learn est épinglé** (`>=1.8,<1.9` dans `pyproject.toml`) : les
  modèles `models/*.pkl` ont été picklés avec la 1.8 et sklearn ne garantit pas
  la compatibilité entre versions mineures. Pour changer de version, ré-entraîner
  (`build_dataset.py` puis `train_classifier.py`) et déplacer la borne.
  `doctor.py` détecte le décalage.
- **Windows — changement de projet non testé sur une vraie machine.** Le code
  gère désormais les deux cas (drapeaux `DETACHED_PROCESS`, ré-exécution de
  `argv[0]` quand ce n'est pas un `.py`), mais rien n'a pu être vérifié sous
  Windows depuis l'environnement de développement Linux.
- **Windows — chemins.** L'état vit dans `~/.config/amcx` (fonctionne, mais
  l'idiome Windows serait `%APPDATA%`) et les projets dans `~/Documents/AMCx`
  (attention à une redirection OneDrive du dossier Documents).
- **Partager un projet sans TeX** : un collègue qui reçoit un dossier de projet
  déjà compilé (`exam.tex` + `exam.xy` + `DOC-sujet.pdf`) peut scanner et
  corriger **sans pdflatex**. Seule la *création* de sujet exige TeX.
- **Pas de mode multi-utilisateur.** Le projet actif est global
  (`~/.config/amcx/active_project`) et en changer redémarre le process : une
  instance partagée servirait le même examen à tout le monde, et il n'y a
  aucune authentification. AMCx s'installe sur le poste de chacun.

## Démarrer un nouveau projet

**Le plus simple : par l'UI** (recommandé pour un nouvel utilisateur).
1. Lancer le serveur (`python auto_grading/front/server.py --port 5050`).
2. Si aucun projet n'est actif → page d'**accueil** (`onboarding.html`) :
   « Ouvrir un projet existant » ou « Créer un nouveau projet ».
3. Le bouton **➕ Créer un nouveau projet** ouvre une modale avec 2 options :
   - **Examen minimal** : projet vierge canonique (header + 1 section + 1 QCM
     single + 1 QCM mult + 1 ouverte).
   - **Importer un fichier AMC** : upload d'un `.tex` existant → copié dans
     `sujet/exam.tex` → migration auto vers le mode canonique (best-effort).
     Gère les sujets **à groupes** (une version par `\exemplaire`, cf.
     *Versions du sujet*). Une migration qui ne comprend pas le sujet est
     **refusée** : le projet est créé, le sujet reste en lecture seule et
     l'écran affiche pourquoi — plutôt qu'un sujet vide au barème nul.
4. Le nom du projet → dossier créé sous `~/Documents/AMCx/<nom>/`.
5. Le serveur **redémarre** pour basculer sur le nouveau projet.

**Alternative CLI** :

```bash
python auto_grading/new_project.py <chemin_du_nouveau_projet>
# ou pour importer un fichier AMC existant :
python auto_grading/new_project.py <chemin> --from-amc <exam.tex>
```

Ensuite, dans le nouveau projet (ou depuis l'UI sur le projet actif) :
1. UI → onglet **Sujet** : éditer l'examen, puis le **Compiler** (produit le PDF
   *et* le calage `sujet/exam.xy` — positions des cases) ;
2. imprimer le PDF, faire passer l'examen ;
3. scanner les copies en PDF, les déposer dans `projet/` (= `amc_dir`) ;
4. `extract_pages.py` → `cv_grade.py --all` → `front/seed_raw_responses.py`, puis
   corriger dans l'UI.

⚠️ Le pipeline suppose **un seul sujet imprimé en N exemplaires** (feuille de
réponses séparée). Ne pas changer `\AMCrandomseed` entre la compilation et
l'impression : copies et calage doivent provenir de la même compilation.

## Multi-projets

Un seul projet **actif** à la fois, désigné par un pointeur global :
`~/.config/amcx/active_project` (fichier texte → chemin absolu) et l'historique
des récents dans `~/.config/amcx/recent.json`. La variable d'env
`AMCX_PROJECT_DIR` (utile pour tests/dev) prend le pas sur le pointeur.

Architecture :
- **Le code** (Python, Flask, `front/templates`, `front/static`, modèles ML
  `models/`) vit **uniquement** dans l'installation (le `auto_grading/` du repo),
  poussée sur GitHub. Une invocation `python auto_grading/front/server.py` lance
  toujours le code installé — peu importe le projet actif. Résolu au runtime via
  `__file__` / `_INSTALL_DIR` (cf. `cv_grade.MODELS_DIR`, Flask
  `template_folder`/`static_folder`). `new_project.py` ne copie **aucun** code :
  un projet vierge = `config.json` + `sujet/exam.tex` seulement.
- **Les données** (sujet, raw_responses, pages, config, imports, compte_rendu)
  vivent dans le projet actif → résolues via `config.project_root()`. Un projet
  ne contient donc **que des données** ; les modèles ML sont partagés depuis
  l'installation.
- **Switch de projet** = `project_state.restart_server_with_project(path)` :
  écrit le pointeur, spawn un watcher détaché qui attend la libération du port,
  puis `os._exit(0)` du process courant → le watcher exec un nouveau serveur
  qui repart sur le nouveau projet (≈ 500-800 ms côté browser).

**Topbar** : le menu déroulant à côté du brand **AMCx** nomme l'évaluation
active et **déroule les évaluations du dossier de travail**
(`workspace.evaluations`, injecté par `server._inject_project_context`).

⚠ **Ce n'est plus une liste de « récents »**, et c'est le point : un historique
décrit le passé d'une personne, alors que ce qu'on veut savoir en ouvrant ce
menu est ce que contient le dossier qu'on a sous les yeux dans l'onglet
Fichiers. Les deux écrans montraient deux mondes, qui se contredisaient dès
qu'un dossier était renommé ou déplacé. `recent.json`, `recent_projects()` et
`/api/projects/forget` existent toujours — plus aucune page ne les lit.

⚠ **Le scan porte sur DEUX niveaux** : un dossier de travail est soit plat (une
évaluation par sous-dossier), soit groupé en projets. N'en regarder qu'un
viderait le menu dans le second cas. On ne descend pas dans une évaluation
(son `auto_grading/` n'en est pas une seconde), et le dossier intermédiaire est
affiché à droite du nom — sans lui, deux évaluations homonymes de deux
promotions s'affichent pareil.

⚠ **Le scan est silencieux sur échec** : un dossier de travail illisible ne doit
pas faire échouer le rendu de *toutes* les pages, le menu étant dans
`base.html`. Fixé par `test_un_dossier_de_travail_illisible_ne_casse_pas_le_rendu`.

⚠ **Il n'y a plus qu'UNE voie de création**, la modale de `base.html` (« Nouvelle
évaluation »), ouverte par `window.AMCxNewEvaluation(parentAbs)` : l'écran
d'accueil et le clic droit de l'onglet Fichiers l'appellent tous les deux. Un
`prompt()` dans l'arbre aurait perdu l'import d'un `.tex` AMC, qui n'existe que
là, et deux formulaires auraient fini par accepter deux jeux de noms
différents. La modale « Ouvrir un projet » a disparu : ouvrir, c'est choisir
dans ce menu ou dans l'arbre.

Routes API :
- `GET /api/projects` → `{active, active_name, recent, default_root}`
- `POST /api/projects/open` → `{path}` → restart sur la nouvelle évaluation
- `POST /api/projects/create` → `{name, template, parent?, file?}` → crée puis restart
- `GET /api/projects/browse?path=` → `{path, display, parent, at_root, dirs}` —
  sous-dossiers, pour le sélecteur de dossier de la modale et de `/fichiers`
- `POST /api/projects/mkdir` → `{parent, name}` → crée un sous-dossier
  (bouton **＋ Nouveau dossier** du sélecteur) et y entre
- `POST /api/projects/forget`, `GET /api/projects/discover` — **plus aucun
  appelant côté front** depuis le retrait des récents

⚠ **Créer ou ouvrir un projet se termine par un suicide du serveur**, donc
l'échec du transport ne dit rien du résultat. `_restart_after_response()`
accroche le redémarrage à `response.call_on_close` (le corps a été remis au
serveur WSGI) plutôt qu'à un `sleep(0.2)` lancé avant de répondre — sans ça la
connexion était coupée avant l'arrivée du corps sur une création un peu longue,
et le navigateur affichait « Erreur réseau » alors que le projet existait. Le
front ne conclut plus à l'échec sans vérifier : sur erreur réseau il attend le
retour du serveur et compare le projet actif au nom demandé.

**Où est créé un projet** : champ « Dossier parent » de la modale, par défaut
`~/Documents/AMCx`, avec un bouton **📁 Parcourir**.

⚠ **Le sélecteur est borné au dossier personnel** (`project_state.browse_root`),
le champ texte non. Le serveur n'a aucune authentification et `--host` permet
de l'exposer : une route qui énumère n'importe quel dossier de la machine
serait une primitive de reconnaissance offerte à qui l'atteint. Le champ libre,
lui, ne révèle rien — il faut déjà connaître le chemin qu'on y tape. Les
tentatives de sortie (`/etc`, `~/../..`) répondent **403**.

⚠ **Lecture et écriture partagent le même contrôle de borne**
(`project_state.check_under_browse_root`, appelé par `list_subdirs` *et* par
`make_subdir`) : deux contrôles séparés finiraient par diverger, et c'est celui
de l'écriture qui coûterait cher. Le nom d'un dossier créé passe par la même
règle qu'un nom de projet — il peut en devenir un — donc `..` et `a/b` sont
refusés avant tout accès disque.

⚠ **Le nom de projet est validé par liste noire**, pas blanche
(`project_state.project_name_error`) : l'ancienne règle n'acceptait que
`[A-Za-z0-9_-. ]`, donc « Régression » était refusé avec un message annonçant
« lettres », qui ne disait pas pourquoi. On n'interdit que ce qui casse un
chemin — séparateurs, caractères interdits sous Windows, `.`/`..`, point ou
espace final (Windows les retire en silence, le dossier ne porterait pas le nom
affiché) et les noms de périphérique réservés (`CON`, `NUL.txt`…). Le JS ne
duplique que les cas courants, la règle qui fait foi est côté serveur.
- `GET /api/templates` → liste des templates dans `auto_grading/templates/`

## Statut (examen de test EXAM_2026)

- **174 scans → 173 copies** (1 page de pub CamScanner) ; **136 traitées par AMC**, **38 en échec AMC** (mires non détectées) → CV seul.
- **Levier 2 livré** (`masked_detect.py` + 5 features masquées + GBM ré-entraîné sur 173 copies + flagging multi-estimateurs). Voir `auto_grading/FLAGGING_PLAN.md` pour la spec et `proto_mask_benchmark.py` pour le banc d'essai.
- **Précision** (modèle de prod, 23 features) :
  - CV honnête par copie (GroupKFold 5-fold sur 173 copies / 26 469 cases) : **99.93 % ± 0.05**.
  - `cv_benchmark` vs AMC : 99.89 % — 10 erreurs résiduelles dont la plupart sont **AMC qui se trompe** (27 cellules AMC≠UI sur EXAM_2026, dont 26 « AMC=vide / utilisateur=cochée » sur des marques pâles).
  - Out-of-fold (simulation futur examen) : **18 erreurs / 26 469**, dont **16 flaggées (89 %)**, 2 silencieuses irréductibles (encre tracée *sur* la lettre imprimée → invisible à la mesure).
  - Reproduction des 67 corrections de la relecture utilisateur : **67/67 lus correctement** par le modèle (in-sample, attendu).

## Pipeline

```
PDFs → pages/            (extract_pages.py — PyMuPDF, 300 dpi)
     → raw_responses_cv/ (cv_grade.py — OpenCV + classifieur GBM)
     → raw_responses/    (front/seed_raw_responses.py — merge CV + AMC + diff)
     → UI Flask          (front/server.py, port 5050)
     → students.csv      (batch_run.py --cache-only)  ou  /export.csv (UI)
```

**Section « 📁 Fichiers du projet » en haut de l'onglet Évaluation** : 2 cartes
côte-à-côte (PDFs scannés à gauche + xlsx étudiants à droite) avec :
- noms de fichiers, nb pages, taille, date
- stats agrégées (📥 extraites · ⚙ corrigées · ✓ validées · 👥 étudiants)
- bouton **« + Ajouter un PDF de copies »** → upload via `POST
  /api/upload-scan-pdf` dans `amc_dir/`
- bouton **« ⚙ Traiter les scans »** → lance un pipeline async (extract →
  grade → seed) via `POST /api/process-scans` qui renvoie un `task_id` ;
  l'UI poll `GET /api/process-scans/<task_id>` toutes les 1.5 s pour la
  barre de progression et le log (extraction par PDF, puis grading
  page-par-page, puis seed via subprocess `seed_raw_responses.py
  --preserve-manual`).
- bouton **« ↻ Changer la liste »** → réutilise la modale xlsx existante.

Backend helpers : `_project_files_info()` (récap pour le render),
`_run_pipeline(task_id)` (thread daemon), `_PIPE_TASKS` (dict en mémoire
des tâches, non persisté).

⚠ Régex `_ARTIFACT_RE` dans `extract_pages.py` filtre les artefacts de
compilation (`exam.pdf`, `DOC-*`, `*-corrige`, `corrige_*`, `*_solution*`,
etc.) pour ne pas les traiter comme des copies scannées. Pour forcer une
liste explicite : `config.scan_pdfs = ["batch1.pdf", ...]`.

## ⚠️ Pièges critiques (lire impérativement)

### 1. AMC randomise l'ordre des options — calage via `layout_store`
Le 1er `\bonne` dans `exam.tex` n'est **pas** la case A sur la feuille. La géométrie
des cases (position + lettre affichée) vient de [layout_store.py](auto_grading/layout_store.py),
qui résout la source par précédence : `<amc_dir>/data/layout.sqlite`, sinon un `.xy`
dans `amc_dir`, sinon `sujet/exam.xy`.

**Le `.xy` (« calage ») est produit par `pdflatex`** : `automultiplechoice.sty`
l'écrit en mode *calibration* (`compile_pdf()` ajoute un `exam-config.tex` avec
`\def\SujetExterne{1}`). `layout_store.parse_xy()` est un portage fidèle de l'outil
AMC `meptex` — vérifié reproduire `layout.sqlite` à ~1e-12 px. → **plus aucune
dépendance au logiciel AMC**.

⚠ **Ne jamais coder en dur les numéros de question.** AMC numérote *toutes* les
questions (QCM, ouvertes, colonnes du code étudiant) : les colonnes ID sont
Q32-35 sur EXAM_2026 mais Q3-6 ou Q33-36 ailleurs. La correspondance
« numéro AMC ↔ bloc du sujet » est donnée par
`sujet_store.amc_question_map(copy)`, bâtie sur les tags du `.xy`
(`question_names`) avec repli positionnel ; `server.id_columns()` en dérive les
colonnes du code étudiant. `check_layout_consistency()` tourne au démarrage du
serveur et signale tout décalage sujet ↔ calage.

### 2. Q8 vaut 2 pts → total 32
Q8 a 6 bonnes réponses (`\bareme{b=1/3,m=-1/3}` dans l'`exam.tex` d'EXAM_2026 — dossier d'examen externe, hors dépôt). Total max = **32**.

**Le barème est piloté par `auto_grading/sujet/subject.json`** (source de vérité unique, éditée via l'onglet *Sujet*). [score.py](auto_grading/score.py) lit `b`/`m`/`value` via [sujet_store.py](auto_grading/sujet_store.py)`.get_bareme()` (qui lit `subject.json`, cache `mtime`). Il n'y a **aucun repli** : une question absente du sujet vaut 0 (`answer_key.py`, figé sur EXAM_2026, a été supprimé — il produisait des notes fausses et silencieuses dans les autres projets, cf. [archive/](auto_grading/archive/)). Modifier le barème dans l'UI recalcule toutes les notes (le total max n'est donc plus figé à 32). Voir la section *Onglet Sujet*.

**Pas de plancher** : depuis le passage en points négatifs, [score.py](auto_grading/score.py) ne plafonne plus une question mult à 0 (`mult = Σ b/m`, peut être négatif) — donc le total d'une copie peut aussi être négatif. Le score est recalculé à la volée depuis `answers` à chaque affichage ; changer `score.py` ne touche jamais `raw_responses/`.

### 3. Source de vérité = `raw_responses/<batch>/page_<NNN>.json`
L'UI lit/écrit là. **Ne jamais écraser les `answers` de ces fichiers** — c'est la relecture finale de l'utilisateur. `cv_grade.py --all` n'écrit QUE dans `raw_responses_cv/` (scratch). Le re-seed (`seed_raw_responses.py --preserve-manual`) préserve les copies portant un flag de `seed_raw_responses.USER_FLAGS` (`manually_edited`, `validated`, `id_corrige`, `open_answer_edited`) ou un `_student_override`/`_cv_student_id` : `answers`, `student_name`, `student_id`, `_student_override`, `_cv_student_id`, `open_answers` et les flags utilisateur. Les autres copies sont rafraîchies depuis le CV. Toutes les écritures de `raw_responses/` passent par `config.write_json_atomic` (tmp + `os.replace`). **En pratique : ne pas re-grader cet examen.**

### 4. Structure d'un JSON
```jsonc
{
  "student_name": "DUPONT Jean",          // rempli si identité corrigée, sinon ""
  "student_id": "3021",                   // 4 derniers chiffres lus (éditable, peut contenir "?")
  "answers": {"1": ["A","C"], ...},        // état COURANT (CV → édité par l'utilisateur)
  "notes": "method=cv_full; mires=ok; ml=on(overrides=N); ambigu(K): Q5_C Q12_B ...; frame_fail=M",
  "_cv_answers": {...},                    // lecture CV originale, immuable
  "_amc_answers": {...},                   // ground truth AMC (absent si AMC failed)
  "_amc_copy": 10,                         // ID AMC 1..136 (absent si AMC failed)
  "_amc_validated_cells": 153,             // nb cases AMC manual∈{0,1} (0 = AMC auto seul)
  "_cv_amc_diff": [{"q":22,"char":"D","cv":false,"amc":true}, ...],
  "_ambiguous_cells": [                    // levier 2 : cases douteuses (flagging multi-estim.)
     {"q":22,"char":"F","decision":false,"ratio":0.574,"masked":0.236,"proba":0.04,
      "reasons":["disagree"]}, ...
  ],
  "_cv_student_id": "30?1",                // ID lu par CV, immuable (créé au 1er edit de chiffre)
  "_student_override": "13021",            // ID canonique 5 chiffres posé manuellement (review finale)
  "_reviewed_cells": ["22_F", "5_C"],      // cases signalées DÉJÀ traitées (cf. review_state)
  "_reviewed_questions": [12],             // signalement de structure traité, par question
  "_reviewed_id": true,                    // identité confirmée alors que le numéro est illisible
  "_source": "cv",
  "_flags": ["cv_differs_amc(2)", "manually_edited", "validated"]
}
```
**Flags** : `cv_differs_amc(N)`, `amc_unvalidated`, `ambiguous`, `id_incomplet`, `no_mires`, `manually_edited`, `validated`, `id_corrige` (identité assignée manuellement).

### 5. Décision = GBM ; flagging = convergence d'estimateurs indépendants
[cv_grade.py](auto_grading/cv_grade.py) `grade_image` :
- `fill_ratio` par case (`box_fill_ratio`, shrink 0.18).
- **Détection masquée** ([masked_detect.py](auto_grading/masked_detect.py)) : mesure de noirceur **hors de l'encre imprimée** (cadre + lettre A/B/C…) — référence = rendu du PDF du sujet, calage par cadre détecté, masque large, mesure relative au papier (p85). Élimine le biais par-lettre.
- Le **classifieur GBM tourne sur TOUTES les cases** (23 features : 18 historiques + 5 masquées : `masked_ratio_e3/e5/e7`, `frame_detected`, `align_residual`) → décision finale.
- **Flagging multi-estimateurs (« levier 2 »)** — une case est `douteuse` ssi au moins un :
  - **E1** masked_ratio_e5 > 0.12 (seuil ABSOLU, indépendant de la calibration GBM) ≠ E2 (shrink vs seuil adaptatif) ≠ E3 (GBM) ;
  - **E4** `predict_proba` ∈ [0.30, 0.70] (GBM peu sûr).
- Sortie → `_ambiguous_cells` (liste de dicts `{q, char, decision, ratio, masked, proba, reasons}`) écrite directement dans le JSON (cv_grade et seed_raw_responses la propagent ; l'UI la signale par un `?` orange).

#### ⚠ La référence masquée décrit UNE COPIE — pas « le sujet »

Deux défauts corrigés ensemble, tous deux **silencieux**, tous deux mesurés sur
un lot réel (39 pages, sujet à 2 versions, `shuffle_answers` actif) :

- **`ref_frames` était indexé par `(question, answer)`.** `answer` est l'ordre
  de déclaration LaTeX : il est **permuté d'une copie à l'autre**, alors que la
  case `A` de la question 1 est toujours au même pixel de la feuille. Dès que la
  copie scannée n'était pas la copie 1, la table rendait donc le cadre d'une
  **autre case** (jusqu'à 300 px plus loin) : le masque d'encre imprimée tombait
  à côté, et la mesure masquée devenait du bruit. La clé est désormais
  **`(question, char)`**, stable d'une copie à l'autre.
- **`render_reference` rendait la page `page` du PDF**, alors que le calage
  numérote les pages *par copie* (piège de `Layout.pdf_page`) : la feuille de la
  **première version** servait de référence à toutes les copies, y compris à
  celles d'une version dont les cases ne sont pas aux mêmes ordonnées.

Mesure avant/après sur ce lot : **23 cases pourtant noircies à plus de 50 %
étaient lues « non cochées »** (dont les 6 que l'utilisateur avait corrigées à la
main sur une copie) → **0** ; **27 % des cases vides** dépassaient le seuil
d'encre E1, donc signalées « douteuses » pour rien → **1 %** ; signalements de la
file : **175 → 5**. Le p99 du `masked_ratio_e5` des cases vides passe de 0,251 à
0,055, pour un minimum de 0,468 chez les cases noircies : le seuil E1 à 0,12
redevient un seuil, et n'a **pas** eu besoin d'être touché.

⚠ **Le cache de `get_reference` est indexé par la GÉOMÉTRIE de la feuille**
(`masked_detect.sheet_signature`), pas par le numéro de copie : deux copies
d'une même version ont des feuilles identiques au pixel près et doivent
partager le rendu — sinon on re-rend une page de PDF 300 dpi par copie
corrigée. Deux versions ont deux références. Coût mesuré : +2 % par page.

⚠ **`grade_image` charge la référence APRÈS avoir identifié la copie.** Chargée
avant (elle sert aussi d'amorce au recalage sans mires), elle décrit la copie 1.

⚠ **Sans mesure masquée, le GBM ne décide plus** (`cv_grade.decide_cell`). Il a
été entraîné avec ces 5 features toujours présentes ; sur une ligne où elles
manquent, sa probabilité s'effondre vers une constante (~0,42 mesuré) — donc
« non cochée », quelle que soit la noirceur de la case. La main revient au seuil
adaptatif, qui ne dépend que de la mesure brute, et la case n'est signalée
(`no_masked`) que si le verdict en change : signaler toutes les autres
remplirait la file. Sans ce garde-fou, une panne de la mesure masquée fait
disparaître des réponses sans rien afficher.

⚠ **E6 (structurel : question `single` avec ≠ 1 case cochée) n'est PLUS écrit
par `cv_grade`** — il est recalculé à l'affichage par
[review_state.py](auto_grading/review_state.py). C'est une fonction pure des
réponses courantes, et le stocker avait deux défauts : le signal restait affiché
après correction (5 cases d'EXAM_2026 le portaient encore alors que leur
question était réparée), et il signalait les 5 ou 6 cases d'une question
simplement laissée blanche — **496 des 885 cases signalées, soit 56 %**. Le
banc de non-régression le confirme : réponses et identités **inchangées**,
features **identiques au bit près** sur 26 622 cases, seul `_ambiguous_cells`
passe de 801 à 257 entrées.
- Repli sans classifieur : `ticked = ratio > seuil adaptatif`.
- Modèle chargé depuis `models/cell_clf_full.pkl`.

### 6. PDF → JPEG via PyMuPDF, 300 dpi
[extract_pages.py](auto_grading/extract_pages.py) utilise **PyMuPDF (`fitz`)** + Pillow — pas de poppler. Défaut **`--dpi 300`** : résolution canonique du pipeline. Ne pas réextraire à 200. Les PDF de copies sont **auto-découverts** dans `amc_dir` (`config.scan_pdfs` pour une liste explicite ; option `--pdfs`). Chaque `<nom>.pdf` → `pages/<nom>/`.

### 8. La page de la feuille de réponses est **dérivée**, pas figée
`layout_store` déduit `answer_sheet_page` (la page portant des cases « réponse ») —
pour EXAM_2026 c'est la page 12, pour le gabarit vierge la page 2. De même le
nombre de questions QCM et de colonnes du code étudiant sont dérivés du calage
(cases à lettres = QCM ; cases à chiffres = colonnes ID). Rien n'est figé à 31/35.

### 7. Recalage : mires d'abord, cadres imprimés en repli

`cv_grade.detect_mires(gray, layout=lay)` cherche les 4 disques **dans des
fenêtres autour de leur position canonique** (donnée par le calage), teste la
forme par trois critères et **valide le quadrilatère** avant de rendre quoi que
ce soit. Le filtre historique (aire + circularité ≥ 0,65) acceptait tout carré
noir : une case cochée, un bit du code imprimé, deux bits contigus — 133 à 201
candidats par page, départagés par la seule distance au coin, alors que le
premier bit du code est à 736 px du coin haut-gauche pour un seuil de 744. Une
mire absente donnait une homographie fausse sous un `method=cv_full` rassurant.
Les seuils sont **mesurés** sur 692 vraies mires contre 599 autres candidats
des scans d'EXAM_2026 ; chacun garde 100 % des vraies. **Toujours passer
`layout=`** — sinon la recherche retombe sur la page entière.

**Sans mires, on ne redimensionne plus : on recale sur les ~193 cadres
imprimés** (`align_by_frames`). Amorce par corrélation avec le rendu du PDF du
sujet, puis quelques homographies robustes sur les cadres retrouvés. Mesuré sur
15 vraies copies dont les 4 coins sont rognés : **100 % des questions justes
(465/465) et 15 identités sur 15**, contre **33 %** avec l'ancien
redimensionnement — une page sans mires versait donc du bruit dans
`raw_responses/` sans que rien ne le signale. Le résidu obtenu (0,8 à 1,6 px)
est meilleur que celui du recalage par les mires (3 à 8 px).

⚠ **Le critère d'acceptation est indispensable** : une page qui n'est pas une
feuille de réponses ne retrouve presque aucun cadre (14 sur 193, corrélation
0,04 sur la pub CamScanner d'EXAM_2026). Elle est refusée et le pipeline
retombe sur `cv_no_mires`, plutôt que de rendre une lecture plausible et
fausse. `method` vaut donc `cv_full`, `cv_frames` ou `cv_no_mires`.

### 7ter. Réponses sur plusieurs feuilles — ⚠ implémenté mais peu éprouvé

Un sujet à beaucoup de questions déborde : AMC imprime alors **une feuille de
réponses par groupe de questions**, chacune avec son propre code (copie, page,
checksum) en haut. Le pipeline lit une feuille à la fois puis les recolle.

⚠ **Ce chemin n'a été validé que sur des scans fabriqués** (rendus du PDF avec
des cases noircies par programme), jamais sur de vraies copies scannées. Le cas
à une feuille, lui, est éprouvé sur les 173 scans d'EXAM_2026. Attendre des
surprises sur : marques pâles d'une feuille à l'autre, feuille agrafée de
travers, scan qui saute une feuille. **Vérifier quelques copies à la main avant
de corriger un lot.** `doctor` le rappelle dès qu'il voit plusieurs feuilles.

Ce qui a été mesuré, sur un sujet de 80 questions à 2 feuilles : les deux
feuilles sont lues (34 + 47 questions, l'union couvre le sujet), 3 copies
recollées sans erreur, et le recollage résiste à un scan dans le désordre
quand les exemplaires sont numérotés.

- **Le calage distingue les deux notions** : `Layout.answer_sheet_pages` (toutes
  les feuilles) et `answer_sheet_page` (la principale, celle qui porte le plus
  de cases). `sheet_boxes()` **sans argument rend TOUTES les feuilles** — c'est
  ce que veut une correspondance globale (question ↔ lettre, barème, liste des
  QCM) ; `sheet_boxes(page=N)` rend celles d'une feuille, ce que veut tout ce
  qui travaille sur UNE image (lecture, référence masquée, ronds de l'UI).
  ⚠ Un sujet à une feuille rend la même chose dans les deux cas : une confusion
  entre les deux ne se voit donc **pas** sur EXAM_2026.
- **Quelle feuille est sous les yeux** : `cv_grade.pick_sheet_page()`. Le code
  imprimé donne le numéro de page, validé par checksum — source sûre. À défaut,
  on compte les cadres retrouvés sur un échantillon de chaque feuille : seule la
  bonne s'ajuste. Écrit dans le JSON (`_sheet_page`, `_sheet_source`).
- **La référence masquée est par feuille** (`masked_detect.get_reference(lay,
  page)`, cache de 4 entrées) : mélanger les feuilles ferait chercher les cases
  de l'une aux positions de l'autre.
- **Le recollage** vit dans `seed_raw_responses.group_sheets()` /
  `merge_sheets()`. La copie fusionnée est écrite à l'emplacement de sa
  **première feuille**, avec `_sheets` qui liste les images sources — tout
  l'aval (score, liste, export) reste donc inchangé.

⚠ **Deux façons de relier les feuilles d'un même étudiant, très inégales.**
Avec des **exemplaires numérotés** (`\exemplaire{N}`, bandeau *Randomisation*),
le code imprimé donne le numéro de copie : le recollage est exact et insensible
à l'ordre du scan. Avec **un seul exemplaire imprimé en N**, toutes les copies
portent le numéro 1 et seul l'**ordre du scan** relie les feuilles — une feuille
manquante ou intervertie décale tout le reste du lot. C'est pour cette raison
qu'AMC impose des exemplaires numérotés sur les examens multi-pages. Le mode
dégradé est testé et fixé dans `tests/test_multi_sheet.py`, pas corrigé : on ne
peut pas deviner à qui appartient une feuille anonyme.

⚠ Un n° de copie lu sur la **grille manuelle** (`_copy_id_source = "grid"`) n'a
pas de checksum : il ne sert **pas** à regrouper, on retombe sur l'ordre.

**UI** : la vue copie affiche un sélecteur de feuilles (`?sheet=N`), ne pose les
ronds que sur les cases de la feuille affichée, et `zoom_img` résout la case
vers l'image qui la porte réellement (`server.sheet_of_question`).

### 7bis. Banc de non-régression — à lancer avant/après toute optimisation

[cv_regress.py](auto_grading/cv_regress.py) fige le résultat de `grade_image`
**et la matrice des 23 features telle que le GBM la reçoit**, puis compare deux
instantanés bit à bit :

```bash
python auto_grading/cv_regress.py snapshot avant     # code de référence
# … modifications …
python auto_grading/cv_regress.py snapshot apres
python auto_grading/cv_regress.py compare avant apres
```

« features : IDENTIQUES au bit près » est la seule preuve qu'une optimisation
ne touche pas le modèle. Les temps affichés ne valent que sur machine calme :
pour comparer, alterner les deux versions (un `git worktree` sur l'ancien
commit) plutôt que de comparer deux exécutions éloignées.

## Le classifieur ML (GBM)

Détecte si une case est cochée à partir de **23 features** :
- **18 historiques** (multi-shrink fill ratio, centroïde, composantes connexes, edge density, light-gray Tipp-Ex, contexte par question…) ;
- **5 masquées** (cf. [masked_detect.py](auto_grading/masked_detect.py) — `MASKED_FEATURE_COLS`) : `masked_ratio_e3/e5/e7` à 3 érosions de l'intérieur, `frame_detected` (0/1), `align_residual` (MSE des 4 coins après similarité réf→scan).

- [build_dataset.py](auto_grading/build_dataset.py) — assemble `results/labeled_cells.parquet` : features + labels depuis **relecture UI** en **priorité 1** — toutes les cases si la copie est `validated` (= relue en entier), sinon **uniquement** ses `_reviewed_cells` (cf. *Relecture*) —, AMC `manual∈{0,1}` en **repli** (AMC parfois erroné sur les marques pâles ; cf. 27 conflits sur EXAM_2026). La référence masquée (rendu du PDF sujet) est mise en cache au mtime via `masked_detect.get_reference`.
- [train_classifier.py](auto_grading/train_classifier.py) — `HistGradientBoostingClassifier` (sklearn), split **par copie** ; écrit `models/cell_clf_full.pkl` (prod) + `models/cell_clf.pkl` (test) + `results/clf_report.txt`.
- `extract_features()` / `FEATURE_COLS` vivent dans [cv_grade.py](auto_grading/cv_grade.py) ; signature `(warped, box, q_ratios_s18, copy_baseline, offset, ref, ref_corners, masked_feats)` — les 3 derniers servent à brancher la détection masquée ; `masked_feats` permet de réutiliser un calcul (évite le double-calcul dans `grade_image`).
- [cv_benchmark.py](auto_grading/cv_benchmark.py) — accuracy vs ground truth AMC ; `--no-ml` = seuil seul. Sur EXAM_2026 le « 99.89 % » est limité par les 27 erreurs d'AMC lui-même — la mesure honnête est la **CV par copie** dans `clf_report.txt`.

## Configuration runtime

[auto_grading/config.py](auto_grading/config.py) + `config.json` (créé au 1er save ;
`save_config` ne persiste que les clés de `DEFAULTS`, les clés obsolètes sont purgées). Clés :
- **`amc_dir`** (dossier de l'examen : PDF des copies, `data/` AMC éventuel), `scan_pdfs` (liste explicite de PDF, sinon auto-découverte), `answer_sheet_page` (0 = dérivée du calage) ;
- `export_template_xlsx` (modèle xlsx scolarité pour `export_scolarite.py`, "" = aucun) ;
- `student_xlsx` (liste étudiants, .xlsx ou .csv) et ses colonnes **par index** :
  `xlsx_id_idx`, `xlsx_nom_idx`, `xlsx_prenom_idx`, **`xlsx_mail_idx`**
  (-1 = aucune), `xlsx_data_start` (index de la 1re ligne de données) et
  **`xlsx_sheet`** (onglet du classeur, `""` = onglet actif — voir le piège
  plus bas). Les anciennes clés `xlsx_*_col` (intitulés) ne servent plus qu'à
  relire une config antérieure ;
- `grade_files` (fichiers de notes importés, voir [grade_imports.py](auto_grading/grade_imports.py)) — chaque entrée `{path, sheet, join_mode:"id"|"name", join_col:<idx>, data_start:<idx>, grade_cols:[{idx, label, seuil, max, agg_weight}], name_overrides:{<nom brut>:<id|null>}}` (colonnes par **index**, jointure par id ou nom fuzzy ; `sheet` = onglet du classeur, `""` = onglet actif) ;
- ⚠ **Les cinq clés suivantes ne sont plus lues au niveau d'un examen** (cf.
  *Onglet Évaluation*) : elles sont conservées telles quelles et serviront au
  niveau qui rassemble plusieurs examens. Les effacer casserait les config déjà
  écrites, et les appliquer en silence changerait la note :
  - `hist_granularity` (largeur d'une barre d'histogramme, en points) ;
  - `qcm_seuil` (**normalisation** du QCM ; `null` = auto), `qcm_max`, `qcm_agg_weight` ;
  - `final_threshold` (plafond dur de la note agrégée) ;
  - `pass_mark` (seuil de réussite).
- `question_floor` / `question_ceiling` / `total_floor` / `show_score_range` :
  règles de **barème**, appliquées par `score.py` à chaque calcul. Réglées dans
  l'onglet **Sujet** (bandeau *Réglages globaux* → *Barème*).

Importé par `student_list.py` (roster), `grade_imports.py` (notes importées) et `front/server.py`. Modifiable via l'UI (onglet Évaluation : « Liste étudiants » ; onglet Sujet : plancher/plafond du barème).

### ⚠ La normalisation par défaut est le barème du sujet, pas une constante

Le diviseur du rescaling s'appelle **normalisation** dans l'interface. La clé de
stockage garde son nom historique (`qcm_seuil`, et `seuil` dans
`grade_files[*].grade_cols[*]`) : la renommer casserait les config.json déjà
écrites — mais plus aucun libellé ne dit « seuil » pour ce paramètre, qui se
confondait avec le *seuil de réussite* et le *plafond* de la note finale.

`config.DEFAULTS["qcm_seuil"] = None` = **auto**, résolu au barème maximal du
sujet (`sujet_store.subject_total_max()`, une copie par **version** — deux
versions inégales prennent la plus haute, car un diviseur unique ne peut pas
sous-noter une version entière). Avant, il était figé par le gabarit : 32 dans
`DEFAULTS` (câblé sur EXAM_2026) et **10** dans `new_project.CONFIG_TEMPLATE`.
Sur un sujet qui vaut 5 points, toute la promo était donc divisée par 10 — un
QCM parfait affichait 10/20 — **sans que rien ne le signale**.

⚠ `config._migrate_qcm_seuil()` ramène ces deux valeurs à « auto » à la lecture
(in-memory, comme `_migrate_banks`). Elle ne peut pas faire de dégât : quand le
placeholder coïncide avec le barème réel, « auto » rend exactement le même
nombre — le seul cas où elle change quelque chose est celui où il ne le
décrivait pas.

⚠ **Ces clés ne sont plus lues au niveau d'un examen** (cf. *Onglet Évaluation*
ci-dessous) : elles restent en config, et serviront au niveau qui compare
plusieurs examens.

## Onglet Évaluation (`/`) — UN examen, aucun réglage

L'ex-« Dashboard ». Il porte le nom de ce qu'il décrit : **une** évaluation,
celle dont le sujet est dans l'onglet d'à côté.

⚠ **La note d'un examen est son score BRUT sur le barème du sujet.** Il n'y a
aucun curseur, aucune normalisation, aucun plafond, aucune pondération.
Seuiller (« 30 points suffisent pour tout avoir ») et ramener sur une autre
échelle (« sur 20 ») ne servent qu'à *comparer ou agréger* cet examen avec
autre chose — c'est le travail du niveau au-dessus, pas de cette page. Les deux
histogrammes, le nuage de points, la formule et l'import de fichiers de notes
sont partis pour la même raison : ils décrivent un **projet**, pas une évaluation.

Ce que la page garde : la carte « Fichiers du projet », la liste des copies
(score brut), la distribution de la note brute sur une ligne, le bouton
« compte rendu », et le **score moyen par question** — la seule mesure qui
parle de cet examen-là.

- `server.exam_columns()` / `exam_threshold()` : l'unique colonne de note, sur
  le barème. ⚠ On garde la forme « liste de colonnes » que consomme
  `compute_aggregate` — un examen en est le cas **N = 1**, où le rescaling se
  réduit à l'identité. **Une seule implémentation de la note**, servie à la
  page, à `/export.csv`, au `compte_rendu/notes.csv` et aux courriels ; deux
  auraient fini par annoncer deux notes.
- ⚠ **Le score moyen par question vient de `server.question_stats()`**, la même
  fonction que l'onglet Questions (`/api/questions/stats` n'en est que
  l'emballage JSON). L'évaluation n'en affiche que `mean_raw`, en points.
- ⚠ **`legacy_grade_settings()` dit ce qui a changé, et seulement si ça a
  changé.** Un projet antérieur porte `qcm_max = 20` : sa note passe de
  `brut × 20 ∕ barème` au brut, et ce nombre part à la scolarité. Le critère
  est donc **la note, pas la présence d'une clé** : l'ancienne formule
  `min(brut × max ∕ normalisation, plafond)` est identique à la nouvelle tant
  que `max = normalisation` et que le plafond ne mord pas sur le barème.
  Mesuré sur les projets réels : « 5 sur 5, plafond 20 » sur un sujet qui vaut
  5 ne signale **rien** (c'est le cas courant), « ramené sur 20 à partir de 33 »
  en signale deux. Un bandeau qui crie pour rien est un bandeau qu'on apprend à
  ignorer.

⚠ **Plancher/plafond par question et plancher global ont déménagé dans l'onglet
Sujet** (bandeau *Réglages globaux* → *Barème*, avec « afficher la fourchette
sur le sujet »). Ils changent le **score** — `score.py` les applique à chaque
calcul —, donc les retirer du tableau de bord sans les remettre ailleurs aurait
laissé un projet avec un plancher actif et aucune commande pour le voir. Leur
place est auprès du barème, pas auprès d'une page qui ne règle plus rien. Ils
s'écrivent toujours dans le `config.json` du projet (`POST /api/config`, pas
`/api/sujet/config`) : `score.py` les relit sur le mtime du fichier.

### [grades_view.py](auto_grading/grades_view.py) — les notes, sans projet actif

Colonnes, rescaling, agrégation, histogrammes, nuage de points, bornes de
curseurs : **logique pure**, zéro I/O, zéro état global. Tout ce qui dépend du
sujet (barème, points d'une question) entre par **paramètre** — c'est ce qui
permettra au même code de servir un projet entier sans une seconde
implémentation. `server.py` n'en importe plus que `SERIES_COLORS`,
`series_stats` et `compute_aggregate` ; le reste attend le niveau au-dessus.

Le rescaling qu'il porte : `note* = note × max ∕ normalisation`, note finale
`min( Σ(agg_weightᵢ·noteᵢ*) ∕ Σ agg_weightᵢ , final_threshold )`.

⚠ **Les routes `/api/grade-file*` et `build_grade_files_info()` sont
conservées** alors que plus aucune page ne les appelle : elles sont l'API des
notes importées, que le niveau au-dessus consommera telle quelle.

### [exam_results.py](auto_grading/exam_results.py) — la table des résultats, une seule fois

Une ligne par étudiant, absents compris, triée par nom. **Pure** : les copies,
les absents et la note entrent par paramètre, `note_of(copy)` étant fourni par
l'appelant (`server.exam_columns()` + `compute_aggregate`) — ce module ne
décide pas de la note, une seconde définition ici finirait par contredire la
première.

⚠ `/export.csv` et `compte_rendu/notes.csv` la bâtissaient **chacun de leur
côté** : deux tris, deux façons de marquer un absent, deux jeux de colonnes,
pour deux fichiers censés dire la même chose. `server.exam_rows()` est
désormais l'unique construction. Vérifié sur un projet réel : le `notes.csv`
régénéré est **identique au caractère près** à celui qu'écrivait l'ancien code.

⚠ **`ABSENT_MARK` n'est plus déclaré qu'ici.** Il l'était trois fois — serveur,
[export_scolarite.py](auto_grading/export_scolarite.py),
[mail_results.py](auto_grading/mail_results.py) — chaque copie renvoyant aux
deux autres en commentaire.

⚠ Les intitulés `note_sur_32` / `QCM_brut_sur_32` gardent leurs noms : 32 était
le barème d'EXAM_2026, pas une constante, mais des scripts de la scolarité et
l'onglet Courriels (`mail_score_col`) les lisent.

#### Lire un projet sans serveur — `amcx results`

```sh
amcx results                                   # projet actif, lisible
amcx results --json                            # pour un autre programme
amcx results --project ~/Documents/AMCx/QCM1 --json
```

C'est le point d'entrée qu'un niveau supérieur (plusieurs examens d'un même
dossier) appellera **en sous-processus**, un par projet.

⚠ **Un process = un projet.** `config`, `sujet_store` et `server` figent leurs
chemins **à l'import** : lire deux projets dans le même process donnerait le
sujet de l'un et les copies de l'autre, en silence. `--project` re-exécute donc
le script avec `AMCX_PROJECT_DIR` posé. **Mesuré** : import de `server` 0,3 s
(Flask n'est pas démarré), lecture de 38 copies 0,09 s, **3 projets lus en
parallèle en 0,56 s** — c'est ce qui rend inutile le refactor « passer
`project_root` partout ».

⚠ L'argv de la ré-exécution est **reconstruit**, pas recopié de `sys.argv` :
appelé par `amcx results`, celui-ci porte le mot « results » que le script ne
connaît pas.

⚠ **Un dossier qui n'est pas un projet le dit** (`ResultsError`, code de sortie
2). Avant le contrôle, un mauvais chemin rendait « 0 copie, barème 0 » avec un
code 0, et un chemin inexistant retombait sur le dossier d'installation : un
examen vide se serait glissé dans le relevé d'un projet sans que rien ne le
signale. `resolve_project()` accepte le dossier du projet **ou** son
sous-dossier `auto_grading/` — c'est ce dernier que rend
`new_project.create_project()` et que pointe `~/.config/amcx/active_project`,
alors qu'on nomme « projet » le dossier parent. La règle est déterministe et le
chemin réellement lu est rendu dans la sortie (`path`).

L'import de notes, les réglages et la sauvegarde du compte rendu ne touchent jamais `raw_responses/`.


## Onglet Fichiers (`/fichiers`) — le dossier de travail

On y arrive par le **brand « AMCx »** de la barre du haut, pas par un onglet :
les onglets décrivent l'évaluation active, le dossier de travail est le niveau
qui la contient.

Une arborescence à la VS Code sur le **dossier de travail** : des
**évaluations** (un sous-dossier par examen), éventuellement groupées en
**projets**, et ce qui les accompagne (listes d'étudiants, scans en attente,
comptes rendus). Moteur : [workspace.py](auto_grading/workspace.py) — voir le
glossaire en tête de ce fichier pour le couple `project`/`cohort`.

⚠ **C'est le MÊME dossier que le projet de `/cohorte`**, et le même pointeur
(`~/.config/amcx/active_cohort`, env `AMCX_COHORT_DIR`). Deux racines — « mon
dossier de travail » ici, « mon projet » là — auraient fini par désigner deux
endroits, et « mes examens » aurait voulu dire deux choses. Le `cohorte.json`
n'apparaît que le jour où l'on compose réellement un projet : **définir la
racine n'écrit rien**, ce qui permet de la poser sur un dossier existant sans
le transformer.

### Créer un projet, créer une évaluation

Les deux se font au clic droit dans l'arbre (ou par les boutons de la barre) :

| | ce que ça pose | comment on l'ouvre |
|---|---|---|
| **🎓 Nouvelle évaluation ici** | `sujet/exam.tex` (modale partagée, import `.tex` compris) | `/api/projects/open` → **le serveur redémarre** |
| **📚 Nouveau projet ici** | un dossier + son `cohorte.json` (`workspace.new_cohort`) | `/api/workspace/root` → **l'arbre se ré-enracine**, pas de redémarrage |
| **📚 En faire un projet** / **… n'est plus un projet** | pose ou retire le `cohorte.json` d'un dossier qui existe déjà | idem |

⚠ **Créer un projet ne bascule PAS dessus.** L'ouvrir re-enracine l'arbre :
le faire d'office planterait l'utilisateur dans un dossier vide, alors qu'il
vient le plus souvent de créer un rangement où **déplacer** des évaluations
existantes. Le message de création dit comment l'ouvrir ensuite.

**Un dossier existant se change en projet, et inversement** — même entrée de
menu, dans les deux sens (`workspace.make_cohort` / `unmake_cohort`, route
`POST /api/workspace/cohorte/set {path, is_project}`). C'est le geste courant :
on range d'abord, on déclare ensuite, et c'est ce qui permet de reprendre un
dossier d'examens déjà sur le disque sans rien déplacer. La **racine** se
convertit aussi (clic droit sur le vide du panneau).

⚠ **Une évaluation ne peut pas devenir un projet.** Un dossier qui porte
`sujet/exam.tex` est un examen ; lui donner en plus un `cohorte.json` le ferait
apparaître sous les deux pastilles, et « Ouvrir » n'aurait plus de sens unique
— bascule d'examen d'un côté, ré-enracinement de l'arbre de l'autre. L'entrée
de menu n'apparaît donc pas sur une évaluation, et la route refuse en 400.

⚠ **Retirer ne supprime rien : le `cohorte.json` part à la CORBEILLE**, comme
tout le reste de cet onglet. Il porte la composition du projet et les réglages
de note (plafond, seuil, poids) : les détruire sur un clic de trop se paierait
en réglages à refaire, alors que restaurer le fichier remet tout d'un coup. Et
**aucune évaluation n'est touchée** — les dossiers restent où ils sont, ce qui
est le sens de « ce dossier n'est plus un projet ». La confirmation nomme le
nombre d'évaluations que le projet comptait (`info()` rend `n_exams`) : c'est
ça qu'on perd de vue, pas les dossiers.

⚠ **Un dossier peut n'être ni l'un ni l'autre**, et la racine se comporte en
projet **même sans `cohorte.json`** (`cohort.load` traite un fichier absent
comme un projet vide). `is_cohort` n'est donc pas l'inverse de `is_project` :
c'est la présence du fichier, qui n'est écrit qu'au premier réglage.

⚠ **`📚` et non `🗂`** : « 🗂 » n'a pas de glyphe couleur dans la police du
système (mesuré : rendu par un repli monochrome, 35 px contre 40 pour les
autres), il apparaissait comme un carré terne à côté des pastilles voisines.

⚠ **Un rendu du panneau de détail porte un jeton** (`detailSeq`). Il vide le
panneau *avant* d'aller chercher le détail : deux sélections rapprochées
(flèches maintenues) laissent deux requêtes en vol, la seconde vide, puis la
**première** ajoute son contenu par-dessus. Constaté — deux fiches empilées,
dont une périmée. L'aperçu, qui arrive après un second aller-retour, porte le
même jeton.

### ⚠ La route la plus dangereuse du projet

Le serveur n'a **aucune authentification** et `--host` permet de l'exposer. Ces
routes déplacent, renomment et suppriment des fichiers. Les garde-fous, dans
l'ordre où ils mordent :

- **L'API ne parle qu'en chemins RELATIFS à la racine.** Un client ne peut même
  pas *exprimer* un chemin extérieur — c'est la barrière la moins contournable,
  parce qu'elle ne repose sur aucune comparaison.
- **`resolve()` vérifie le chemin RÉSOLU**, liens symboliques suivis : sans ça,
  un lien déposé dans le dossier de travail ouvrirait le reste du disque avec
  les droits de l'utilisateur. Fixé par `test_un_lien_symbolique_qui_sort_est_refuse`.
- **La racine reste bornée au dossier personnel**
  (`project_state.check_under_browse_root`), comme le sélecteur de projet.
- **Un nom passe par `project_state.project_name_error`** (liste noire : `..`,
  séparateurs, caractères interdits sous Windows, noms de périphérique, point
  ou espace final). Son paramètre `what` ne change que le libellé — « Donne un
  nom au fichier » plutôt qu'« au projet » : **une seule règle**, un dossier de
  rangement pouvant devenir un projet.
- **Supprimer, c'est mettre à la corbeille** (`.amcx-corbeille/` dans la
  racine), jamais `rmtree`. Un dossier de projet porte des scans, des
  corrections relues à la main et des notes. `empty_trash()` est **la seule
  fonction du projet qui détruit vraiment**, et la confirmation annonce le
  nombre de fichiers et les octets.
- **Rien n'écrase rien** : ni un déplacement, ni un renommage, ni un dépôt
  (`scan.pdf` déposé deux fois donne `scan-2.pdf`), ni une restauration dont
  la place d'origine a été reprise.
- **On ne déplace pas un dossier dans lui-même** ni dans l'un de ses
  descendants : `shutil.move` y construirait une arborescence dont le parent
  est son propre enfant, sans message utilisable.

⚠ **Liste blanche stricte pour l'affichage EN LIGNE** (`workspace.INLINE_TYPES`
= pdf, png, jpeg, servis avec `nosniff` et une CSP). Servir un fichier de
l'utilisateur en `inline` le place sur l'origine du serveur : un `.html` ou un
`.svg` déposé dans le dossier de travail deviendrait du script exécuté avec les
droits de l'interface. **Tout le reste passe par `/download`, en
`as_attachment=True`**, qui n'exécute rien.

⚠ **Le `auto_grading/` d'un projet n'est pas un projet de plus**
(`workspace.is_project`). Sans cette règle, l'arbre affichait deux pastilles
« projet actif » imbriquées et il fallait deviner laquelle ouvrir.

### Ce que la page fait

- Arbre **paresseux** (un niveau par requête), dépli persisté par racine dans
  `localStorage`, guides d'indentation, icône par type, pastilles `évaluation`
  / **`évaluation active`** et `projet`. Sans la première, on croit corriger
  l'examen qu'on a sous les yeux ici alors que les autres onglets en montrent
  un autre ; les deux teintes séparent les deux niveaux, que le regard
  confondrait — on ouvrirait l'un pour l'autre. La racine porte
  `projet actif` : c'est elle que l'onglet Projet agrège.
- ⚠ **Toutes les actions sont au clic droit dans l'arbre** (ou par le `⋯` au
  survol d'une ligne) : ouvrir, nouveau sous-dossier, nouvelle évaluation ici,
  nouveau projet ici, dépôt de fichiers, renommer, déplacer, supprimer. Le
  **panneau de droite décrit, il ne commande pas** — une rangée de boutons y
  agissait sur l'élément sélectionné, donc à l'autre bout de l'écran de ce
  qu'on vise. Seul « Ouvrir » y reste (`openCard`) : ce n'est pas une opération
  de fichier, c'est ce que fait l'application.
- Le clic droit sur le **vide du panneau** vise la racine (le menu la nomme en
  tête) : sans ce cas, on ne pourrait plus rien créer à la racine dès qu'un
  dossier est sélectionné.
- Glisser-déposer pour déplacer ; déposer des fichiers depuis le bureau pour
  les ajouter (viser une ligne précise reste possible, le panneau entier
  accepte le dépôt).
- Clavier : ↑ ↓ pour naviguer, → ← pour déplier/replier, `F2` renommer,
  `Suppr` mettre à la corbeille.
- **Aperçu** dans le panneau de détail : texte (tronqué à 200 ko), PDF et
  images en ligne. Un panneau vide n'aide personne, et l'usage courant est de
  vérifier un `notes.csv` ou une page scannée sans quitter l'onglet.
- « Ouvrir cette évaluation » bascule l'application (le serveur redémarre) ;
  « Ouvrir ce projet » ne fait que re-enraciner l'arbre (`/api/workspace/root`)
  — un projet lit ses évaluations par sous-processus, rien n'est figé dans le
  process courant. La page ne conclut pas à l'échec sur une erreur réseau :
  la création d'une évaluation se termine par un suicide du serveur, elle sonde
  jusqu'au retour.
- Le sélecteur de dossier réutilise le composant `pm-browser-*` et
  `/api/projects/browse`, la seule route qui énumère le disque (403 hors du
  dossier personnel). Un second sélecteur aurait divergé du premier.

| Route | Rôle |
|---|---|
| `GET /fichiers` | la page (ou l'invite de choix de racine) |
| `GET /api/workspace` | `{root, display, name, n_trash, active, cohort}` |
| `POST /api/workspace/root` | `{path}` — n'écrit rien dans le dossier |
| `GET /api/workspace/list?path=&hidden=` | entrées d'un dossier |
| `GET /api/workspace/info?path=` | détail, enrichi d'un `project_root` (évaluation) ou d'un `cohort_root` (projet) |
| `POST /api/workspace/cohorte` | `{parent, name}` → crée un **projet** ; ne bascule pas dessus |
| `POST /api/workspace/cohorte/set` | `{path, is_project}` → change un dossier existant en projet, ou l'inverse (`cohorte.json` → corbeille) |
| `POST /api/workspace/mkdir` · `rename` · `move` | remaniement |
| `POST /api/workspace/delete` | `{path}` ou `{paths}` → **corbeille** ; un échec sur l'un n'arrête pas les autres et est **rendu** |
| `GET /api/workspace/trash` · `POST .../restore` · `.../empty` | corbeille |
| `POST /api/workspace/upload` | multipart `dest` + `files` |
| `GET /api/workspace/preview?path=` | texte tronqué / type d'affichage |
| `GET /api/workspace/view?path=` | **inline, liste blanche** (pdf/png/jpeg) |
| `GET /api/workspace/download?path=` | toujours `as_attachment` |

## Onglet Projet (`/cohorte`) — plusieurs évaluations d'un même dossier

Un **projet** est un dossier qui contient un `cohorte.json` et, à côté, les
**évaluations** qu'il rassemble :

```
L3-2026/
  cohorte.json
  QCM1/          ← une évaluation
  rattrapage/    ← une autre
  compte_rendu/  ← notes.csv + mail_log.csv du PROJET
```

C'est le niveau où « seuiller à 30 » et « ramener sur 20 » ont un sens : une
évaluation seule se lit sur son propre barème (cf. *Onglet Évaluation*),
comparer ou agréger demande une échelle commune. Moteur :
[cohort.py](auto_grading/cohort.py), page `/cohorte`, ligne de commande
`amcx cohort --dir D [--json]`. Le module et ses routes gardent le nom
historique `cohort`/`cohorte` (cf. le glossaire en tête).

⚠ **Changer de projet ne redémarre PAS le serveur**, contrairement à changer
d'évaluation : un projet lit ses évaluations par sous-processus, il ne fige
aucun chemin dans le process courant. Le pointeur vit dans
`~/.config/amcx/active_cohort` (env `AMCX_COHORT_DIR` prioritaire), et **c'est
le même que la racine de l'onglet Fichiers** : ouvrir un projet, c'est y
enraciner l'arbre.

### La note d'une colonne — le plafond s'applique AVANT la moyenne

`note* = min(brut ∕ normalisation, 1) × échelle`, dans `grades_view.rescale`.
La normalisation est **le score qui vaut tout** : seuiller à 30 un examen qui en
vaut 31 donne 20/20 à qui obtient 30, et au-delà on ne gagne plus rien. Par
défaut, normalisation = échelle = **barème de l'examen**, donc `note* = brut`.

⚠ **Ce plafond est par colonne, pas seulement sur la note finale.** C'est une
promesse faite aux étudiants (« 30 points suffisent »), pas un crédit
transférable sur une autre note. Mesuré sur un QCM de 31 points seuillé à 30 et
ramené sur 20, plus un projet à 14/20 de poids égal : **17,0** avec le plafond
par colonne, **17,33** sans.

⚠ **Pas de plancher ici** : une note brute peut être négative (`mult = Σ b/m`),
et l'écraser à 0 fausserait les moyennes. Le plancher à 0 est une décision
d'affichage, prise au moment d'annoncer la note (`mail_results`, `--no-floor`).

⚠ **Une colonne absente ne compte ni au numérateur ni au dénominateur**
(`grades_view.weighted_final`) : un étudiant qui n'a passé qu'un examen sur deux
obtient la note de celui qu'il a passé. C'est la règle du niveau examen depuis
toujours ; ici elle devient visible, donc elle est **affichée** (« ABS », colonne
`absent_de`) plutôt que subie.

⚠ **`scale_warnings()` dit ce qui rendrait la moyenne trompeuse** : deux
colonnes ramenées sur des échelles différentes (un QCM sur 33 et un projet sur
20, à poids égal) donnent un « /26,5 » que personne n'a demandé. Le niveau
examen l'évitait en n'ayant qu'une colonne.

### ⚠ Le même étudiant ne porte pas le même identifiant d'un examen à l'autre

Le défaut le plus coûteux trouvé ici, et il ne se voit que sur des données
réelles. Mesuré sur deux vrais examens : `3017` dans l'un, `13017` dans l'autre
— **la même personne**, une liste portant le numéro complet et l'autre ses
quatre derniers chiffres. **36 étudiants sur 39** apparaissaient en double, avec
la moitié de leurs notes chacun et deux notes finales fausses.

`cohort.identity_map()` rapproche donc un identifiant d'un autre dont il est le
**suffixe** — la même règle que `StudentMatcher.by_id` pour rattacher une copie
à sa liste —, à deux conditions vérifiées toutes les deux :

- le rapprochement est **unique** : deux identifiants longs finissant par le
  même suffixe ⇒ on ne rapproche rien (fondre deux étudiants est pire que d'en
  afficher un en double) ;
- les **noms concordent**, ou l'un des deux est inconnu (un examen sans liste
  rend « ? » et ne doit pas bloquer un rapprochement que le numéro établit).

Ce qui est refusé est **listé** dans `warnings`, jamais tu : c'est la seule
façon de voir qu'une ligne en double vient d'un numéro ambigu. L'identifiant
canonique retenu est le plus long, les autres restent visibles (`also_id`).

⚠ Une copie **non reliée** (aucun identifiant) ne peut être recollée à rien d'un
examen à l'autre : elle est comptée à part, jamais fondue dans une ligne au
hasard.

### ⚠ Rien n'entre dans la moyenne sans qu'on l'ait demandé

Un projet trouvé dans le dossier mais absent de `cohorte.json` est un
**candidat**, pas un membre (`cohort.candidates()`) : sans cette règle, un
dossier d'essai deviendrait une note. La page les propose, un clic les ajoute.

⚠ `load()` **lève** sur un `cohorte.json` corrompu au lieu de repartir des
défauts : repartir de zéro effacerait la composition du projet et les
réglages de note à la première écriture.

### Exports et courriels — les deux niveaux, un seul moteur

- `GET /cohorte/export.csv` : une ligne par étudiant, `<colonne>_brut` et
  `<colonne>` pour chacune, `note_finale`, `absent_de`.
  ⚠ **Une cellule vide et un `ABS` ne disent pas la même chose** : `ABS` = cet
  étudiant était *attendu* à cet examen et n'a pas composé ; vide = cet examen
  ne le concernait pas. Les confondre ferait passer une promotion entière pour
  absente à l'examen de l'autre demi-journée.
- `POST /api/cohorte/report` écrit `<projet>/compte_rendu/notes.csv` — **le
  même fichier**, posé là où les courriels le cherchent, avec les intitulés
  qu'attend `mail_results.load_recipients` (`id_canonique`, `nom_prenom`,
  `courriel`, `note_finale`). Pas de second format à maintenir.
- L'envoi passe par la ligne de commande, et la page **affiche la commande**
  plutôt que de la deviner :

```sh
python auto_grading/mail_results.py \
  --notes "<projet>/compte_rendu/notes.csv" \
  --log   "<projet>/compte_rendu/mail_log.csv" \
  --out-of 20 --send
```

⚠ **`--log` est indispensable** (option ajoutée pour ça) : le journal du projet
actif ferait **sauter les étudiants déjà servis pour l'examen** — même adresse,
autre note. Le journal suit le fichier de notes, il ne le devine pas.

⚠ Le prénom vient du roster du **projet actif** (`load_recipients`) : pour un
étudiant que ce roster ne connaît pas, le message dit son nom complet. Jamais
« Dear , ».

### Routes

| Route | Rôle |
|---|---|
| `GET /cohorte` | la page (ou l'invite d'ouverture si aucun projet actif) |
| `POST /api/cohorte/open` | `{path, create}` — ⚠ `create` est explicite : poser un `cohorte.json` dans un dossier au hasard n'est pas anodin. Borné au dossier personnel (`check_under_browse_root`) |
| `POST /api/cohorte/config` | plafond, seuil de réussite, granularité + `columns:[{path, seuil, max, agg_weight}]` |
| `POST /api/cohorte/exams` | `{path, label}` ajoute · `{path, remove:true}` retire (**aucun fichier supprimé**) |
| `POST /api/cohorte/report` | écrit `compte_rendu/notes.csv` → `{path, n_rows, command}` |
| `GET /cohorte/export.csv` | le même tableau, en téléchargement |

⚠ **`null` = « auto »** dans `/api/cohorte/config`, et c'est une valeur : le
front renvoie `null` (classe `is-off`) et non le nombre affiché, qui figerait
l'échelle au barème du jour. Même contrat que la normalisation du tableau de
bord d'origine.

## Architecture fichiers

```
pyproject.toml                 ← deps (wheels pures, zéro poppler) + extra [api]
auto_grading/
├── config.py / config.json    ← config runtime partagée (amc_dir, etc.)
├── cli.py                     ← commande `amcx` (run / doctor / update / where)
├── _version.py                ← version, source unique (lue par hatch)
├── doctor.py                  ← diagnostic d'installation (CLI + /api/doctor)
├── tex/                       ← automultiplechoice.sty vendorisé (hors CTAN !)
├── layout_store.py            ← géométrie des cases : parseur .xy (port de meptex)
│                                 + lecteur layout.sqlite ; get_layout() (précédence)
├── new_project.py             ← crée un projet vierge DONNÉES SEULES (config + sujet gabarit, aucun code copié)
├── archive/                   ← code hors service (answer_key, voie Claude-vision,
│                                 ancien workflow to_review/) — voir son README
├── sujet_store.py             ← parse/édite sujet/exam.tex : parse_tex, get_bareme,
│                                 max_score, total_max, save_questions, compile_pdf
├── sujet/                     ← subject.json (SOURCE DE VÉRITÉ) + exam.tex (généré)
│                                 + DOC-sujet.pdf + exam.xy (calage)
├── review_state.py            ← ce qui reste à relire : signalements, état traité, risque (pur)
├── workspace.py               ← dossier de travail : arborescence, corbeille, bornes,
│                                 création d'évaluations et de projets
├── grades_view.py             ← colonnes de note, rescaling, agrégation, histogrammes (PUR)
├── exam_results.py            ← table des résultats (1 ligne/étudiant) + `amcx results` (PUR)
├── cohort.py                  ← PROJET (ensemble d'évaluations) : membres (sous-processus), agrégation
├── score.py                   ← applique le barème (single=value/0 ; mult=Σ b/m, peut être négatif)
├── student_list.py            ← import de la liste (xlsx/csv, colonnes détectées par contenu)
│                                 + StudentMatcher : match par le numéro lu (largeur quelconque) puis nom
├── grade_imports.py           ← import csv/xlsx de notes externes : auto-détection de structure,
│                                 jointure par id OU par nom (fuzzy), résolution manuelle des ambigus
├── mail_results.py / .txt     ← envoi des notes par courriel (onglet Courriels + CLI)
├── extract_pages.py           ← PDF → JPEG 300 dpi (PyMuPDF)
├── cv_grade.py                ← pipeline OpenCV + GBM : detect_mires, warp, box_fill_ratio,
│                                 adaptive_threshold, extract_features, load_cell_classifier,
│                                 load_name_field, grade_image
├── build_dataset.py           ← dataset labellisé pour le classifieur
├── train_classifier.py        ← entraîne le GBM
├── cv_benchmark.py            ← accuracy CV vs ground truth
├── batch_run.py               ← orchestrateur → students.csv (import grader paresseux)
├── models/                    ← cell_clf_full.pkl (prod), cell_clf.pkl
├── front/
│   ├── server.py              ← UI Flask (toutes les routes)
│   ├── seed_raw_responses.py  ← merge CV+AMC → raw_responses/ (--preserve-manual)
│   ├── templates/             ← base.html + evaluation/zoom/flagged/student/identites/sujet/banque
│   │                            + partials _zoom_grid / _id_grid / _student_card / zoom_fragment
│   └── static/                ← style.css + vendor/ (KaTeX + marked.js, vendorisés hors-ligne)
├── pages/                     ← 173 JPEG (ignorés git ; 1 pub CamScanner écartée)
├── raw_responses_cv/          ← sortie CV brute
├── raw_responses/             ← SOURCE DE VÉRITÉ
└── results/                   ← students.csv, labeled_cells.parquet, clf_report.txt
```

## Commandes utiles

```bash
# Installer (zéro dépendance système)
uv pip install -e .                # ou .venv/bin/pip install -e .
.venv/bin/pip install -e ".[api]"  # + voie Claude-vision optionnelle

# Diagnostic (à demander en premier quand « ça ne marche pas »)
.venv/bin/python auto_grading/doctor.py

# UI (port 5050) — Jinja n'auto-reload PAS (debug off) : redémarrer après édition de template
.venv/bin/python auto_grading/front/server.py --port 5050
pkill -f "front/server.py"

# Re-extraire les PDF (300 dpi)
.venv/bin/python auto_grading/extract_pages.py

# Re-grader (n'écrit que dans raw_responses_cv/) puis re-seed (préserve les modifs user)
.venv/bin/python auto_grading/cv_grade.py --all
.venv/bin/python auto_grading/front/seed_raw_responses.py --preserve-manual

# Non-régression du pipeline de détection (cf. piège 7bis)
.venv/bin/python auto_grading/cv_regress.py snapshot <nom>
.venv/bin/python auto_grading/cv_regress.py compare <ref> <nouveau>

# Classifieur : (ré)entraîner
.venv/bin/python auto_grading/build_dataset.py
.venv/bin/python auto_grading/train_classifier.py --cv

# Benchmark + CSV
.venv/bin/python auto_grading/cv_benchmark.py
.venv/bin/python auto_grading/batch_run.py --cache-only

# Sujet : récap du sujet parsé depuis sujet/exam.tex (lecture seule)
.venv/bin/python auto_grading/sujet_store.py
```

## UI — routes

**Ordre des onglets** (dans `base.html`) : Banque | **Sujet** | **Évaluation** | **Questions** | **Projet** | Review rapide | Identités | **Courriels** | Export CSV.

⚠ **Le brand « AMCx » de la topbar EST le lien vers `/fichiers`**, et il n'y a
pas d'onglet Fichiers. Les onglets décrivent tous l'**évaluation active** ; le
dossier de travail est le niveau au-dessus, celui qui les contient. En faire un
onglet de plus le rangeait à côté de « Sujet » et « Évaluation », comme s'il
parlait du même examen. L'onglet **Projet** est l'exception assumée : il décrit
ce qui contient l'évaluation, et c'est là qu'on agrège les notes.

| Route | Rôle |
|---|---|
| `/sujet` | **Onglet Sujet** : modèle canonique (text/qcm/open) + outline + bandeau global |
| `/` | **Évaluation** : un examen — copies, note brute, score moyen par question. Aucun réglage |
| `/cohorte` | **Projet** : plusieurs évaluations d'un dossier — colonnes, histogrammes, nuage, formule |
| `/fichiers` | **Fichiers** (lien du brand « AMCx ») : arborescence du dossier de travail, corbeille, création d'évaluations et de projets |
| `/questions` | **Onglet Questions** : ranking par taux de réussite + aperçu PDF + histo par question |
| `/api/questions/stats` | GET : `[{q, tag, type, statement, max_score, n_eval, n_perfect, mean, scores, bank_id}]` pour chaque QCM du sujet |
| `/flagged` | **Review rapide** : signalements groupés par question, triés par risque ; `?status=open\|done\|all&sort=risk\|scan` |
| `/student/<b>/<p>` | Vue copie : image canonique + ronds magenta + zoom embedded |
| `/student/<b>/<p>/zoom` | Onglets *Réponses* (2 zones) / *Identité* (crop nom + grille ID) |
| `/identites` | Review finale : copies non reliées ↔ noms, drag&drop |
| `/mail` | **Onglet Courriels** : gabarit, expéditeur, secret SMTP, envoi des notes |
| `/sujet/pdf` | PDF du sujet (`sujet/DOC-sujet.pdf`), inline |
| `/sujet/publication/<kind>.pdf` | Sujet vierge / corrigé à publier (`kind` ∈ `sujet`, `corrige`) |
| `/api/sujet/publication` | POST `{kind}` → recompile ce document, renvoie `{ok, log, n_pages, url}` |
| `/diagnostic` | Diagnostic d'installation (à envoyer au support) |
| `/api/doctor` | GET : mêmes contrôles en JSON `{ok, checks:[{status,label,detail}]}` |
| `/sujet/region/<q>.png` | crop PNG de la région d'une question (aperçu) |
| `/sujet/page/<n>.png` | page entière du PDF rendue en PNG (aperçu empilé) |
| `/sujet/regions.json` | GET : `{pages:[{n,w,h}], total_pages, regions:[{q,page,x0,y0,x1,y1}]}` — **ratios** [0,1], pour poser les cadres de question sur l'aperçu |
| **API Sujet — édition** | |
| `/api/sujet` | GET : `{config, header, answer_sheet, blocks, mode, available_copies, total_max, max}` |
| `/api/sujet/save` | POST batch `{questions:[…]}` (compat legacy, redirige vers blocks/update) |
| `/api/sujet/compile` | POST : `pdflatex exam.tex` → PDF + `.xy` |
| `/api/sujet/config` | POST patch (num_copies, random_seed, shuffle_*) — OK en legacy |
| `/api/sujet/header` | POST patch (canonique seul, refus legacy = 409) |
| `/api/sujet/answer-sheet` | POST patch (canonique seul) |
| `/api/sujet/regenerate-seed` | POST → nouveau seed aléatoire |
| `/api/sujet/versions/update` | POST `{vid, name?, num_copies?, header?}` → plages de copies recalculées (cf. *Versions du sujet*) |
| `/api/sujet/blocks/add` | POST `{kind, after_bid?, data?}` → `{bid}` |
| `/api/sujet/blocks/delete` | POST `{bid}` |
| `/api/sujet/blocks/move` | POST `{bid, after_bid|null}` |
| `/api/sujet/blocks/update` | POST `{bid, data}` (OK legacy pour qcm) |
| `/api/sujet/blocks/duplicate` | POST `{bid}` → `{bid}` |
| `/api/sujet/migrate-to-canonical` | POST → ajoute marqueurs `%%QCM-…` + backup |
| **API correction (inchangées)** | |
| `/api/toggle` | toggle case réponse + flag `manually_edited` + marque la case traitée |
| `/api/review-cell` | POST `{batch,page,q,char,reviewed?}` — « j'ai regardé, c'est bon » |
| `/api/review-question` | POST `{batch,page,q,reviewed?}` — signalement de structure traité |
| `/api/review-copy` | POST `{batch,page}` — tous les signalements de la copie traités |
| `/api/review-identity` | POST `{batch,page,reviewed?}` — identité confirmée malgré un numéro illisible |
| `/api/set-id-digit` | fixe un chiffre du numéro étudiant |
| `/api/assign-student` | assigne/retire un étudiant |
| `/api/mark_validated` | flag `validated` = **copie relue en entier** ; `{value:false}` pour l'enlever |
| `/api/config` | GET/POST config du projet (barème : plancher/plafond, fourchette) |
| `/api/upload-xlsx` | POST fichier (.xlsx/.csv) → analyse : colonnes, aperçu, proposition |
| `/api/student-list/preview` | POST mapping → ce qu'il chargerait, sans rien écrire |
| `/api/student-list` | POST mapping → contrôle, sauvegarde de l'ancienne liste, enregistrement |
| `/api/student-list/analyze` | POST `{sheet}` → ré-analyse le fichier en attente sur cet onglet |
| `/api/student-list/cancel` | POST → abandonne le fichier en attente |
| `/api/upload-grade-file`, `/api/grade-file`, `/api/grade-file/remove`, `/api/grade-file/resolve` | notes externes |
| `/api/save-report` | écrit `compte_rendu/` : notes.csv + SVG |
| `/api/student-card/<b>/<p>` | fragment HTML fiche étudiant |
| `/export.csv` | CSV récap (dont le `courriel`, si la liste en porte un) |
| `/img/...`, `/img_canon/...`, `/zoom_img/...`, `/name_img/<b>/<p>.jpg` | images (cache disque sous `static/zoom_cache/<hash-projet>/`, invalidé au mtime de la page source) |

## Édition du sujet — pertes de saisie évitées

L'onglet *Sujet* recharge la page après plusieurs actions (ajout, duplication,
import de banque, édition IA, migration). Chacune passe par `reloadPage()`,
précédée de `ensureSavedBeforeReload()` pour les actions déclenchées à la main :
proposition d'enregistrer, ou abandon. Un `beforeunload` couvre tout le reste
(fermeture d'onglet, navigation). Un changement de type single↔mult ne recharge
plus au milieu de la boucle de sauvegarde (`_reloadAfterSave`, appliqué à la
fin) — sinon les blocs suivants étaient abandonnés.

`AMCxBlockEditor` (front/static/block_editor.js) stocke son contexte **par bloc**
(`WeakMap`) : un callback partagé faisait que le dernier `initBlock` écrasait
ceux des blocs précédents. Sans effet tant qu'une page n'édite qu'un bloc
(banque), bloquant pour la migration de `/sujet` vers cet éditeur.
`onTypeChange(blk)` est attendu (`await`) : l'appelant enregistre avant de
re-rendre.

## Sécurité (serveur local, sans authentification)

Le serveur écoute par défaut sur `127.0.0.1` mais `--host` permet de l'exposer,
et aucune route n'est authentifiée. Garde-fous en place — **à ne pas retirer** :

- **Noms de batch validés au plus près du disque** : `server.safe_batch()` est
  appelé dans `load_copy_json` / `save_copy_json` / les routes d'images, pas
  dans chaque route — un nouvel appelant ne peut pas l'oublier. Sans ça,
  `batch="../../.."` lit et écrit hors du projet (`save_copy_json` crée les
  dossiers manquants). Idem `q`/`char` de `/zoom_img`, validés avant de
  construire le chemin de cache.
- **Anti-CSRF** : `_same_origin_only()` (`before_request`) refuse toute requête
  non-GET dont l'`Origin` ne correspond pas à l'hôte servi. Les 11
  `get_json(force=True)` acceptent du `text/plain`, donc sans ça une page web
  tierce peut déclencher n'importe quelle mutation sur `localhost:5050`.
  Une requête sans `Origin` (curl, tests) reste acceptée.
- **Secrets jamais renvoyés au navigateur** : `public_config()` masque
  `anthropic_api_key` et retire les jetons Supabase des banques.
  `/api/ai/auth-status` et `/api/banks` exposent déjà ce dont le front a besoin.
- **Contenu de banque = entrée non fiable** : une question `public` vient d'un
  autre utilisateur. `AMCxRender.sanitizeHtml()` filtre par liste blanche la
  sortie de marked (avant réinsertion du HTML KaTeX, qui est généré localement).
  Les messages d'erreur vont en `textContent`, jamais en `innerHTML`.
- **`bank_id` validé** avant tout glob (`bank.is_valid_bank_id`) : `"*"`
  matchait la première question venue.
- `ValueError` → **400** via `@app.errorhandler`, pas un 500 opaque.

- **Le sélecteur de dossier ne sort pas du dossier personnel** :
  `GET /api/projects/browse` refuse en **403** tout chemin hors de
  `project_state.browse_root()`. C'est la seule route qui énumère le disque.

- **`[hidden]` doit gagner contre les classes** (`style.css`, en tête) : la
  règle du navigateur est de spécificité 0, donc `.pm-modal-field { display:
  flex }` la battait et un élément masqué en JS restait affiché — le champ
  « Fichier AMC » s'affichait pour le template fourni. Trois règles ponctuelles
  rattrapaient déjà le coup au cas par cas ; `[hidden] { display: none
  !important }` vaut pour tout le reste.

La route `/api/save` (écriture d'un JSON arbitraire à un chemin fourni par le
client, sans aucun appelant côté front) a été **supprimée**.

## Relecture — ne rien louper, et savoir où on en est

Module : [review_state.py](auto_grading/review_state.py) (logique pure, zéro
I/O), consommé par `/flagged`, la vue copie, le tableau de bord et
`build_dataset`. **Une seule implémentation de « ce qui reste »** — sinon une
page dit « terminé » pendant qu'une autre dit « 107 à revoir ».

### Le défaut d'origine : rien ne distinguait « relu » de « pas encore relu »

`_ambiguous_cells` était figé à l'heure de la correction, jamais marqué. Une
case regardée puis jugée correcte était indistinguable d'une case jamais
ouverte, et **confirmer la lecture du CV était impossible** : seule une
correction faisait sortir une case de la file. D'où trois clés d'état, écrites
par l'UI et préservées au re-seed :

| clé | posée par | sens |
|---|---|---|
| `_reviewed_cells` | `/api/review-cell`, `/api/toggle`, `/api/review-copy` | cases signalées traitées |
| `_reviewed_questions` | `/api/review-question` | signalement de structure traité |
| `_reviewed_id` | `/api/review-identity` | identité confirmée malgré un numéro illisible |

⚠ **Basculer une case vaut décision** : `/api/toggle` pose la marque, sans clic
supplémentaire. Elle est posée *explicitement* et pas déduite de `answers ≠
_cv_answers` — un aller-retour ramènerait à l'état CV et ferait réapparaître une
case qu'on vient pourtant d'examiner.

⚠ **Une copie `validated` n'a rien d'ouvert, par définition** : ce drapeau ne se
pose que depuis la vue copie ou le zoom, les seules qui montrent toutes les
cases. Sans cette règle, une copie relue de bout en bout garderait ses halos et
la relecture ne convergerait jamais.

### `validated` ≠ « signalements traités » — et pourquoi ça compte

`build_dataset` prend une copie `validated` comme **vérité terrain sur toutes
ses cases**, en priorité 1 devant AMC. Or « Marquer validé » était aussi le
bouton de la review rapide, qui ne montre que les cases signalées. Résultat
mesuré : les **26 469 étiquettes** du jeu d'entraînement venaient *toutes* de
`ui_validated`, alors qu'au plus **~3 %** des cases avaient été mises sous les
yeux de quelqu'un — le modèle réapprenait sa propre sortie sur le reste, et la
validation croisée mesurait cette reproduction.

Depuis : la review rapide pose des marques par case (`✓ Tout traiter`), jamais
`validated` ; seules les vues qui montrent tout posent `validated`. Côté
`build_dataset`, une copie non `validated` n'étiquette que ses
`_reviewed_cells` (source `ui_reviewed`), AMC reprend la main ailleurs. Les
données déjà validées d'EXAM_2026 gardent leur sens ancien — c'est irrattrapable
a posteriori, il faut le savoir avant de citer le chiffre d'exactitude.

### `/flagged` — une file de doutes, du plus ambigu au moins ambigu

- **Un bloc = une question, toutes ses cases sur une ligne**, et rien d'autre.
  Le liseré magenta dit la décision courante, le `?` orange le doute de
  l'algorithme (cf. *Code couleur*). Un clic sur une case change la décision.
- ⚠ **Aucune notion de « traité ».** Retirée sur retour d'usage : elle
  superposait un second état (traité / pas traité) à celui qui compte ici
  (douteux / pas douteux), et « ✓ Tout traiter » vidait la file **sans rien
  décider**. Corriger une case reste enregistré comme décision humaine
  (`/api/toggle` → `_reviewed_cells`), ce dont `build_dataset` a besoin pour
  ses étiquettes `ui_reviewed` — le seul usage de ces marques qui subsiste.
  **Conséquence assumée : la file ne se vide pas toute seule** ; un doute
  qu'on choisit de laisser tel quel y reste. Les routes `/api/review-cell`,
  `review-question`, `review-copy`, `review-identity` existent encore mais
  aucune page ne les appelle.
- ⚠ **Une copie dont seule l'identité pose question n'est PAS dans l'onglet
  Réponses** : elle n'y a rien à montrer, et c'est ce qui remplissait la liste
  de copies sans une seule case à regarder (38 sur 38, projet sans liste
  étudiants chargée). Elle est dans l'onglet Identité, qui existe pour ça. Le
  bandeau d'identité reste affiché sur les copies qui figurent dans les deux.
- **Tri par ambiguïté décroissante** (`sort=amb`, défaut ; `scan` pour l'ordre
  de scan) : le **maximum** de `1 − |2p − 1|` sur les cases signalées, pas la
  somme — cinq doutes tièdes ne doivent pas passer devant un vrai doute. Un
  signalement de structure vaut 0,5 (ni sûr, ni douteux). `review_state` rend
  les questions déjà triées ; la page ne trie rien.
- ⚠ **Deux comptes distincts** : `n_cell_flags` (réponses seules, affiché) et
  `n_flagged` (identité comprise). Les confondre annonçait « 42 signalements »
  pour 4 questions et 38 identités.

**Historique** : la page a été groupée par question (2025), puis découpée en
deux colonnes cochées / non cochées, puis ramenée à une seule colonne — la
colonne de droite était le plus souvent vide, et la question à se poser se lit
déjà sur la case.

⚠ Deux degrés d'alerte sur l'identité (`server.id_state`) : `unresolved`
(aucun étudiant ne correspond) et `weak` (grille illisible, rattachement par le
seul nom manuscrit reconnu de façon approchée — 9 copies sur EXAM_2026). Un
rattachement posé à la main (`override`, `id_corrige`) n'est **pas** douteux :
quelqu'un l'a décidé. Les compter aurait rempli la file de cas déjà tranchés.

⚠ La clé du dict de copie s'appelle **`questions`**, pas `items` : dans un
template Jinja, `s.items` résout la *méthode* du dict avant la clé et la boucle
casse sur `'builtin_function_or_method' object is not iterable`.

⚠ **`server.get_layout(copy)` prend une copie.** Sans elle, l'index
`(question, lettre) → case` était celui de la **copie 1** : sur un sujet à
versions, toutes les vignettes des copies de la seconde version répondaient
**404** (`/zoom_img/...`), donc s'affichaient vides dans la review rapide comme
dans le zoom. Même correction pour `get_warped(..., copy)` (offsets par
question) et `sheet_of_question`.

⚠ **`copy_review()["n_open"]` ne compte QUE les réponses ; la file y ajoute
l'identité douteuse.** Les routes rendaient le premier nombre à une page qui
affichait le second : « 9 à traiter » tombait à 7 au premier clic, l'identité
disparaissant du compte en même temps que la case traitée. `server.
copy_open_count(d)` est l'unique implémentation, servie à la page comme aux
routes (`toggle`, `review-cell`, `review-question`, `review-copy`,
`review-identity` — cette dernière rendait un décompte que le front devinait
en ±1).

### Code couleur — une couleur, un sens, dans toutes les vues de correction

| | veut dire | où |
|---|---|---|
| **magenta** | la **décision courante** — « cette case compte comme cochée », ce chiffre est celui retenu — qu'elle vienne du modèle ou d'un clic | rond plein de la vue copie, liseré des cases de `/flagged` et du zoom, teinte de la zone *Positifs* |
| **orange** | un **doute de l'algorithme** : `?` (case signalée), `⚠` (CV ≠ AMC), halo pointillé sur l'image | sous la case, jamais en liseré |
| **gris** | **déjà traité** — un état de la relecture, pas de la case | `✓`, vignette pâlie |

⚠ **Le doute ne se dit jamais par un liseré.** Dans une colonne de `/flagged`,
toutes les cases sont douteuses : un liseré posé là-dessus ne distingue rien —
c'est le défaut signalé en usage réel. Et le vert disait à la fois « cochée »
(zone *Positifs*, bordure du zoom) et « relue » (`seen-mark`), pendant que le
magenta disait à la fois « cochée » (vue copie) et « douteuse » (zoom) : un
liseré ne se lisait qu'en se rappelant sur quelle page on était.

Sur l'image de la copie, le rond magenta **plein** dit « lu comme coché ». Une
case *signalée mais non cochée* — **78 %** des signalements — n'aurait donc
aucune marque : d'où le **halo pointillé** (`.cell-halo`), **orange**, qui
disparaît dès que la case est traitée.

⚠ Le `viewBox` de l'overlay est la page canonique (~2480 px) ramenée à ~600 px
à l'écran : une épaisseur de trait en unités utilisateur y devient sous-pixel et
le halo disparaît. `vector-effect: non-scaling-stroke` la fixe en pixels écran ;
le tireté, lui, reste en unités utilisateur (d'où `stroke-dasharray: 26 18`).

### Compteurs

⚠ **Le tableau de bord n'en affiche plus aucun** (cf. *Configuration runtime*) :
la progression de la relecture se lit dans l'onglet Review rapide et sur la
carte « Fichiers du projet ». `_is_to_review()` a donc été supprimé — il ne
servait plus que ces cartes.

Son histoire vaut d'être gardée : il comptait les drapeaux posés par la
correction automatique et ignorait la relecture, si bien que le tableau de bord
affichait « 107 à revoir » sur EXAM_2026 alors que **106 de ces copies étaient
déjà validées**, et que le nombre ne décroissait jamais. Il portait aussi une
comparaison morte — `f.startswith("cv_differs_amc")` testait la chaîne
littérale de la boucle, jamais les drapeaux de la copie. Un compteur qui ne
bouge pas ne mesure pas ce qu'il annonce.

⚠ **Un dé-clic est possible partout** : `POST /api/mark_validated {value:false}`,
`review-cell {reviewed:false}`, `review-identity {reviewed:false}`. Avant, le
drapeau `validated` ne s'ajoutait que et le bouton se désactivait : un clic de
trop se réparait en éditant le JSON à la main.

⚠ **Au re-seed** (`--preserve-manual`), les marques de relecture survivent —
mais **seulement pour les cases dont la lecture CV n'a pas bougé**
(`seed_raw_responses.carry_review_marks`). Une nouvelle correction qui lit
autrement doit revenir dans la file, sinon la marque « vu » masquerait la
nouveauté. Même règle pour `_reviewed_id`, invalidée si `student_id` change.

## Liste étudiants — import et rattachement

### ⚠ Le rattachement suit la largeur de la grille, pas une constante

`AnswerSheetConfig.id_grid_digits` va de 1 à 9, et `cv_grade` lit un chiffre par
colonne du calage. `StudentMatcher.by_id` essaie donc, dans l'ordre :
l'identifiant **complet**, puis le **suffixe de la longueur lue**, puis les deux
à nouveau sans les zéros de tête.

Avant, il exigeait **exactement 4 chiffres** : une grille à 5 chiffres — le
choix naturel quand les numéros en font 5 — ne rattachait plus **aucune** copie,
alors que le numéro complet était lu correctement et que `by_full_id` l'aurait
résolu. EXAM_2026 fonctionnait parce que sa grille fait 4, pas parce que le code
était juste.

⚠ **Un suffixe partagé par deux étudiants ne désigne personne** (`None`, pas le
premier arrivé) : attribuer une copie au hasard entre deux étudiants est pire
que ne pas l'attribuer, et la copie non résolue remonte dans `/identites`.
`matcher.collisions(width)` les liste et `matcher.warnings(width)` les formule —
avec les homonymes, qui n'étaient jusque-là imprimés que sur `stdout`. Le
tableau de bord et `doctor` les affichent.

### ⚠ Rien n'est deviné en silence

`load_students` lève `RosterError` si une colonne configurée n'existe plus dans
le fichier. L'ancien repli positionnel sur les colonnes 0/1/2 produisait des
étudiants dont l'identifiant valait « DUPONT », **sans un mot**, et l'interface
annonçait « ✓ 2 étudiants ». `StudentMatcher`, lui, ne lève jamais (toutes les
pages en construisent un) : il porte le message dans `matcher.error`.

### ⚠ Un classeur peut porter plusieurs promotions — l'onglet se demande

`openpyxl` ouvre `wb.active`, c'est-à-dire **l'onglet sélectionné au dernier
enregistrement du fichier** : ni le premier, ni rien qui se voie. Mesuré sur un
classeur de scolarité réel à 5 onglets (`resultat - …`, `Feuil1`, `Feuil2`,
`Reg-EN`, `Reg-FR`) : AMCx chargeait les **165 étudiants du groupe FR** alors
que l'examen était celui du groupe EN (39 étudiants), sans un mot. Les deux
groupes ayant des numéros disjoints, **0 des 37 copies scannées** se rattachait
à quelqu'un — l'utilisateur voit « aucune identité » et n'a aucune raison de
soupçonner l'onglet.

- `grade_imports.list_sheets(path)` énumère les onglets (`[]` pour un csv) et
  `read_table(path, sheet=None)` en lit un. ⚠ **Un onglet demandé mais absent
  lève**, il ne retombe PAS sur `wb.active` : après un renommage, lire
  silencieusement un autre onglet rendrait une tout autre promo — le défaut
  même qu'on corrige.
- L'onglet fait partie du mapping, comme un index de colonne : `xlsx_sheet` en
  config (`""` = onglet actif), `cols["sheet"]` pour `students_from_file`,
  `sheet` dans une entrée `grade_files[*]`. `load_students()` le relit à chaque
  démarrage — sans lui, la relecture repartirait sur l'onglet actif.
- **La modale demande l'onglet AVANT de proposer les colonnes** : rien n'est
  présélectionné quand il y en a plusieurs, et le formulaire reste fermé tant
  qu'aucun n'est choisi. Un `<select>` avec une valeur par défaut aurait laissé
  passer le choix sans le faire.
- ⚠ **`student_list.sheet_summaries()` compte les étudiants de chaque onglet**,
  et c'est ce qui rend le choix possible : une liste de noms bruts ne dit pas
  lequel porte la promo. Mesuré : `Reg-EN — 39 étudiants · 40 lignes` contre
  `Feuil1 — 208 étudiants`. `n_students = None` = onglet non compris (annoncer
  « 0 étudiant » serait une affirmation non vérifiée) ; l'onglet reste
  choisissable, les colonnes se règlent à la main. Coût : un classeur relu une
  fois par onglet — 0,4 s pour 5 onglets dont un de 816 lignes —, plafonné à
  `max_sheets=20`.
- **Un fichier à un seul onglet (ou un csv) n'affiche rien** : pas de choix à
  faire. Même règle que le sélecteur d'exemplaire de `/sujet`.
- ⚠ **Et il n'est pas épinglé non plus** (`xlsx_sheet` reste `""`) : sans
  ambiguïté à lever, retenir le nom de l'onglet rendrait fatal un simple
  renommage. On ne contraint que là où l'ambiguïté existe.
- Le tableau de bord et `doctor` affichent l'onglet retenu. `doctor` **avertit**
  quand un classeur a plusieurs onglets et qu'aucun n'est choisi (config
  antérieure au correctif) : c'est le seul cas qui reste silencieux.
- Routes : `POST /api/student-list/analyze {sheet}` et
  `POST /api/grade-file/analyze {path, sheet}` ré-analysent le fichier déjà
  déposé sur un autre onglet, sans re-téléverser et sans rien enregistrer.
- **Les fichiers de notes ont la même invite** (même composant `.rl-sheet`) :
  `read_table` leur servait aussi l'onglet actif.

### Import : détection par contenu, aperçu, contrôle avant écriture

`analyze_roster()` lit xlsx **et csv** (`grade_imports.read_table`) et propose
les colonnes **d'après leur contenu** — une colonne d'identifiants est faite de
nombres de largeur constante ; les lignes de données commencent au premier bloc
de trois lignes consécutives où elle est remplie ; entre deux colonnes
alphabétiques, celle qui est le plus en MAJUSCULES est le nom de famille.

⚠ **La détection ne peut pas passer par `grade_imports.analyze_table`** : celui-ci
reconnaît les identifiants en les cherchant dans le roster — circulaire quand
c'est justement le roster qu'on charge.

⚠ **Une colonne de courriels n'est pas une colonne de noms** (`_profile`) :
`jean.dupont@ensai.fr` ne contient aucun chiffre, passe donc le test
« alphabétique » et concourait comme colonne de nom. Sur un export où le
courriel s'intercale entre le numéro et le nom, c'est LUI qui était proposé
comme nom de famille — plausible dans l'aperçu, et faux.

**Ce que la détection encaisse**, fixé par `tests/test_roster_formats.py` :
csv (virgule, `;`, tabulation, BOM), xlsx, xlsm ; colonnes dans n'importe quel
ordre ; colonnes parasites (courriel, voie, libellé) ; plusieurs lignes de titre
au-dessus de l'en-tête ; colonnes vides à gauche ; **aucun en-tête** ; pas de
colonne prénom ; lignes vides intercalées ; accents, traits d'union et
apostrophes ; numéros de **n'importe quelle largeur**, zéros de tête conservés.

**Ce qu'elle ne devine pas**, et qui se règle à la main dans la modale (le
message dit lequel) : un identifiant **non numérique** (`E3021`) — proposé
nulle part, mais parfaitement chargeable une fois la colonne choisie, et
`roster_report` prévient alors que ces identifiants ne sont pas des nombres ;
une liste d'**un seul étudiant** (la 1re ligne de données se cherche sur un bloc
de trois lignes consécutives).

### Le courriel : une donnée de SORTIE, jamais de rattachement

`Student.email` (facultatif, `""` par défaut) est lu dans la colonne
`xlsx_mail_idx` et ressort dans les deux CSV — `/export.csv` et le `notes.csv`
du compte rendu — sous l'intitulé `courriel`. Il sert à renvoyer les notes sans
re-croiser la liste à la main.

⚠ **Il ne participe à aucun rattachement** : rien ne l'écrit sur une feuille de
réponses, `StudentMatcher` ne l'indexe pas. L'ajouter au matching ne ferait que
créer des correspondances invisibles sur la copie.

- **Détection** : la colonne qui porte des « @ » (`_profile` → `n_email`), à
  partir de **2 cellules** — une seule (un contact en pied de tableau) ne fait
  pas une colonne de courriels. Aucun repli : sans colonne à « @ », rien n'est
  proposé et les étudiants portent un courriel vide. Une liste sans courriel
  reste une liste valide.
- La même mesure sert à **écarter cette colonne des candidats « nom »**
  (cf. plus haut) : les deux usages viennent du même comptage.
- ⚠ **La colonne est toujours présente dans le CSV**, vide si la liste n'en
  porte pas — un en-tête stable vaut mieux qu'un en-tête qui change selon le
  projet pour les scripts en aval.
- ⚠ `_pad_leading_zeros` reconstruit les étudiants pour compléter les zéros de
  tête : il passe par `dataclasses.replace`, pas par un `Student(id=…, nom=…,
  prenom=…)` écrit à la main, qui aurait effacé le courriel **en silence**.
  Même piège pour tout futur champ.

La modale montre les premières lignes du fichier, grise celles situées avant la
1re ligne de données et teinte les trois colonnes retenues : c'est ce qui rend
visible qu'un titre d'export n'est pas lu comme un étudiant. Le compte affiché
vient de `/api/student-list/preview`, donc c'est un nombre **d'étudiants
construits**, pas de lignes lues — un import raté ne peut plus afficher « ✓ 3
étudiants » dont l'un était la ligne d'en-tête. `roster_report` signale en plus
les identifiants non numériques, les doublons, les largeurs hétérogènes, la même
colonne choisie deux fois, et une grille plus large que les numéros.

⚠ Intitulés et exemples viennent d'un fichier fourni par l'utilisateur : ils
passent par `textContent`, jamais par `innerHTML`.

⚠ **La liste remplacée est conservée** en `student_list.prev<ext>`, y compris
quand la nouvelle a une autre extension — un csv qui remplaçait un xlsx
effaçait le xlsx sans copie. Le fichier en attente vit dans `imports/` et
l'abandon de la modale le supprime.

⚠ **`_sniff_delimiter` regarde plusieurs lignes**, pas seulement la première :
un export commence souvent par un titre sans séparateur, et le fichier entier
était alors lu comme **une seule colonne**.

## Identités — match étudiant & doublons

- `student_list.StudentMatcher` : `by_id` (numéro complet **ou** suffixe de la
  largeur lue), fuzzy `by_name`, `by_full_id` (pour les overrides).
- `server.resolve_student(d, matcher)` : honore `_student_override` en priorité.
  ⚠ **Si l'override ne désigne plus personne — la liste a changé —, on s'arrête
  là** (`method="override_lost"`). L'ancienne version retombait sans un mot sur
  la lecture de la grille : une copie explicitement attribuée à ABIDELLI
  s'affichait au nom d'ADJEBA MBA, toujours estampillée « assignée à la main »,
  sans aucun drapeau. Une décision humaine ne doit pas être remplacée en
  silence par une lecture machine ; la copie redevient à traiter.
- **`/identites` distingue les quatre provenances** : à résoudre · reconnus par
  le **nom manuscrit seul** (à confirmer) · assignés à la main · rattachés par
  le **numéro lu**. ⚠ Les deux derniers étaient réunis sous « Auto-détectés par
  la grille ID » — faux pour un rapprochement de nom à 70 % de similarité, et
  c'est justement celui qu'il faut regarder (11 copies sur EXAM_2026).
- Le pool de droite a une **recherche** (nom ou numéro) : 174 chips ne se
  parcourent pas à l'œil.
- **Doublons** : si ≥2 copies résolvent vers le même étudiant (ex. ID mal lu), `/identites` met **toutes** ces copies à gauche (badge « doublon ») et libère les noms candidats dans le pool de droite.
- Corriger une identité : soit glisser un nom dans `/identites`, soit cliquer les bons chiffres dans l'onglet *Identité* du zoom (`/api/set-id-digit`).

## Onglet Sujet (`/sujet`) — édition LaTeX d'exam.tex + recompilation

L'onglet *Sujet* est **placé en première position** dans la barre de nav. Il fonctionne
en deux modes auto-détectés selon le contenu d'`exam.tex` :

### Modes : `canonical` ↔ `legacy` (+ `empty`)

- **`canonical`** : le tex contient des **marqueurs commentés** `%%QCM-…` autour
  de chaque morceau structurel (préambule, header, blocs ordonnés, feuille de
  réponses). Chaque bloc a un `bid` stable (`uuid4().hex[:8]`) → CRUD complet
  débloqué (ajout, suppression, drag&drop, renommage).
- **`legacy`** : tex « ordinaire » (cas d'EXAM_2026) sans marqueurs.
  **Lecture seule complète** : badge `🔒 lecture seule` dans le bandeau ; tous
  les inputs/textareas/selects/boutons sont désactivés sauf **Compiler**,
  **Migration** et la navigation (outline, sélecteur copie).
- **`empty`** : `exam.tex` absent (créer un projet via `new_project.py`).

Détection : `sujet_store.is_canonical(tex)` ⇔ présence de `%%QCM-BLOCKS-START`.
Le mode est **persisté dans `subject.json`** : un sujet legacy le reste tant
qu'il n'a pas été migré explicitement.

### Store du sujet : `sujet/subject.json`

**La source de vérité est `sujet/subject.json`**, pas `exam.tex`.

- « Sauvegarder » (toutes les routes `/api/sujet/*`) écrit **uniquement** le
  store, jamais le `.tex`.
- « Compiler » (`compile_pdf`) est le **seul** endroit qui écrit `exam.tex` :
  il le régénère depuis le store (backup `exam.tex.bak` si le contenu change),
  puis produit `DOC-sujet.pdf` et le calage `exam.xy`.
- **Bootstrap** : si `subject.json` est absent mais `exam.tex` présent, le tex
  est parsé une fois et le store écrit ; le `.tex` n'est pas touché.
- ⚠ **Un sujet legacy bootstrappé reste `legacy`** et n'est **pas** régénéré à
  la compilation — son préambule et sa feuille de réponses sont conservés
  verbatim (`cfg.preamble_tex` / `cfg.answer_sheet_tex`, découpés par
  `_split_legacy_tex`, partagé avec `migrate_to_canonical`). C'est ce qui
  garantit un calage `.xy` **byte-identique** : régénérer depuis le gabarit
  changerait la position des cases et désalignerait toutes les copies déjà
  scannées. Vérifié sur EXAM_2026 : `bb78eb3d97b26e34` avant migration, après
  migration, et après bootstrap.
- **Store corrompu** : il est mis de côté en `subject.json.corrupt-<horodatage>`,
  le sujet repart d'`exam.tex`, et un avertissement remonte dans
  `GET /api/sujet` → `warnings` (plus de repli silencieux, qui perdait sans un
  mot toutes les éditions non compilées).

### Modèle de blocs canonique

3 kinds de blocs ordonnés :
- **`text`** : `{tex, readonly?, level?, title?}` — texte libre + sections `\section{X}`.
- **`question_qcm`** : `{tag, qtype: single|mult, env, statement, answers:[{text,
  correct, bareme}], value}` — édition complète y compris ajout/suppression de
  réponses, toggle bonne/mauvaise, types `single`↔`mult`.
- **`question_open`** : `{tag, statement, lines, points, grading_cases:[{label,
  value}]}` — `\AMCOpen` natif avec cases de notation cochables par correcteur.

### ⚠ `question_freeform` est DÉSACTIVÉ à la création

`sujet_store.DISABLED_KINDS` interdit d'en créer : ces blocs **ne s'impriment
pas dans le PDF compilé**, sans la moindre erreur LaTeX. Diagnostic par
élimination (sur un sujet à 3 exemplaires) :

| test | résultat |
|---|---|
| `\element{open}{TEXTE-TEMOIN}` | s'imprime pages 2, 4, 6 → le groupe marche |
| `\AMCOpen` **dans** `\element{open}{…}` | rien |
| … enveloppé dans `\begin{question}` | rien |
| … **hors** du groupe `open` | **s'imprime** |

→ un `\AMCOpen` ne survit pas au stockage dans le registre de tokens du groupe,
et `render_block` enveloppe tous les kinds ouverts dans `\element{open}{…}`.
**`question_open` est donc probablement atteint de la même façon** — non
vérifié de bout en bout.

Le filtre ne porte QUE sur la création (`add_block`) : les blocs existants
restent lisibles, éditables et supprimables, sinon un projet qui en contient
deviendrait inéditable. Ils affichent un bandeau « ne s'imprime pas » et la
pastille `LIBRE — DÉSACTIVÉ`. Le bouton d'ajout est retiré de la toolbar, la
route `/api/sujet/blocks/add` répond 400 avec le motif, et `question_freeform`
sort de `IMPORTABLE` (import d'un `.tex`) — ces blocs sont alors comptés dans
`skipped`.

Pour réactiver : corriger d'abord le rendu (ne plus passer par
`\element{open}` ou trouver l'équivalent qui survit), puis retirer le kind de
`DISABLED_KINDS`.

Marqueurs `%%QCM-PREAMBLE` / `%%QCM-PREAMBLE-END`, `%%QCM-HEADER` / `%%QCM-HEADER-END`,
`%%QCM-BLOCKS-START` / `%%QCM-BLOCKS-END`, `%%QCM-BLOCK bid=… kind=…` / `%%QCM-END bid=…`,
`%%QCM-ANSWER-SHEET` / `%%QCM-ANSWER-SHEET-END`.

### Identification d'une page : le code imprimé en haut (copie / page / checksum)

**L'étudiant n'a rien à recopier.** AMC imprime en haut de *chaque* page un code
en cases noircies — `\AMC@binaryCode` dans le style vendorisé, trois codes
successifs : `id=1` numéro de copie, `id=2` numéro de page, `id=3` checksum. Le
`+1/1/60+` en monospace juste au-dessus n'en est que le doublon lisible à l'œil.

Chaque bit est une **vraie case AMC** taguée `chiffre:<kind>,<rang>`, donc le
calage en donne les positions exactes. `layout_store` les expose désormais
(`Layout.code_boxes`, `CodeBox`), avec la liste des triplets valides
(`Layout.page_ids`, toutes copies confondues) et le checksum par page.
`cv_grade.decode_page_code(warped, layout)` les lit.

⚠ **Rang 1 = bit de poids fort** (vérifié sur un sujet compilé : copie 1 →
`000000000001`, checksum 60 → `111100`), pas l'inverse.

Trois différences avec les cases que l'étudiant coche, qui dictent la méthode :
- le contenu est **imprimé** : les mesures sont bimodales, pas de marque pâle ni
  de gomme — le GBM ne sert à rien ici ;
- les cases **se touchent** (1,7 px d'écart pour 37,8 px de côté), donc
  `refine_box_offset` est inutilisable : deux bits noirs voisins fusionnent en
  un seul contour, rejeté par son filtre de taille ;
- le code est **redondant** et le calage connaît les triplets valides — c'est un
  critère de vérité que les cases réponses n'ont pas.

D'où la stratégie, par coûts croissants : lecture directe → balayage de
décalages → suivi de la déformation case par case. **La validation par triplet
connu sert aussi de critère d'alignement** : inutile de deviner le recalage
géométriquement, et une lecture douteuse est *refusée* au lieu d'attribuer
silencieusement la mauvaise copie. Plusieurs triplets valides trouvés à des
décalages différents ⇒ ambigu ⇒ refus.

⚠ **N'utilise pas `box_fill_ratio` pour ces cases** : il binarise à 128 en dur,
et un scan pâle dont l'encre plafonne à 130 rend alors tout le code « blanc »
(constaté en test : le triplet devenait `(0,0,0)`). `_code_cell_darkness` mesure
l'intensité brute et le seuil est calculé **relativement aux 24 bits de la
page**, ce qui se cale tout seul sur l'exposition du scan.

⚠ **La dérive résiduelle est asymétrique.** Mesuré sur les 173 scans réels
d'EXAM_2026 : après recalage sur les mires, certaines copies mal engagées dans
le chargeur glissent de **70 à 85 px vers le bas** (2-3 mm à 300 dpi), jamais
vers le haut. Le balayage va donc jusqu'à `CODE_DRIFT_DOWN = 120` px vers le
bas mais seulement 20 px vers le haut — élargir symétriquement quadruplerait le
coût pour rien. Avec une fenêtre symétrique de ±16 px, 19 copies sur 173
échouaient.

**Résultats mesurés** (173 scans réels, « pas oufs ») : **173 lus, 0 refusé,
0 faux**. Coût : **0,2 ms** quand le code est lu du premier coup (le cas
courant), 64 ms quand le balayage complet tourne.

Le JSON de `raw_responses/` porte `_copy_id_source` (`printed` / `grid` /
`default`) et `_page_no`, et les `notes` affichent `copy_id=N(printed); page=P`.

**La grille manuelle « N° copie » n'est donc plus imprimée**
(`sujet_store._copy_grid_digits` retourne toujours 0). `cv_grade.detect_copy_id`
reste comme **repli** pour les sujets déjà compilés qui en contiennent une, et
pour les calages sans cases `chiffre:*` (`layout.sqlite` d'AMC, ou un style plus
ancien). ⚠ Recompiler un sujet multi-copies **déjà imprimé** fera disparaître sa
grille et changera son calage : ne pas recompiler après impression.

### Multi-copies (`\exemplaire{N}`)

- `grade_image` identifie la copie par le code imprimé (ci-dessus), sinon par la
  grille manuelle si le sujet en a une.
- `grade_image` détecte `_copy_id` puis recharge `layout_store.get_layout(copy=N)` :
  les positions des cases sont identiques mais le mapping char↔réponse est permuté.
  Le `_copy_id` est écrit dans le JSON `raw_responses/`.
- `layout_store.parse_xy_all_copies(path) -> dict[int, Layout]` et
  `parse_sqlite_all_copies(path)` chargent toutes les copies (plus de hardcode
  `WHERE student=1`). `get_layout(copy=None)` défaut copie #1 (rétrocompat).
- `sujet_store._tex_chars(copy=1)`, `effective_spec(q, copy=1)`,
  `get_bareme(copy=1)`, `max_score(q, copy=1)`, `total_max(copy=1)` ont tous
  un défaut `copy=1` → 100% rétrocompat. Cache `_charmap_by_copy`.
- `score.py` : `score_question(q, sel, copy=1)`, `score_copy(answers, copy=1)`.
- `server.py` : helper `copy_id_of(d) = int(d.get("_copy_id", 1))` injecté à
  TOUS les call sites avec un JSON copie (`list_all_copies`, `build_student_card`,
  `student()` route, `build_zoom_questions`, `api_toggle`).

### Versions du sujet (groupes AMC) — matin / après-midi

Donner des questions **toutes différentes** à deux populations (deux
demi-journées, deux salles) se fait en AMC avec des **groupes** : chaque
question est déclarée dans un `\element{groupe}{…}` au **niveau document**, et
chaque `\exemplaire` ne restitue que son groupe.

```latex
\element{matin}{ \begin{question}{q1} … \end{question} }
\element{aprem}{ \begin{question}{q2} … \end{question} }
\exemplaire{40}{ en-tête matin … \restituegroupe{matin} … feuille de réponses }
\exemplaire{35}{ en-tête aprem … \restituegroupe{aprem} … feuille de réponses }
```

⚠ **C'est la seule construction AMC qui garantit la disjonction.** Le tirage
dans un pool commun (`\setdefaultgroupmode{withoutreplacement}` +
`\restituegroupe[5]{pool}`) est plus simple et protège aussi du voisin de
table, mais les tirages sont **indépendants d'une copie à l'autre** : mesuré
sur 4 copies tirant 5 questions sur 10, les copies 1 et 3 en partageaient 3.
Si la contrainte est « aucune question commune entre les deux sessions », il
faut deux groupes.

**AMC numérote les copies en continu** d'un `\exemplaire` au suivant (vérifié :
deux `\exemplaire{2}` donnent les copies 1-2 puis 3-4, codes imprimés
`+1/1/…`, `+2/1/…`, `+3/1/…`, `+4/1/…`). Conséquence pratique : le numéro
imprimé en haut de chaque feuille dit de quelle version elle vient, donc **les
deux demi-journées se scannent dans le même lot** — `decode_page_code` →
`get_layout(copy=N)` → le bon jeu de questions. Rien à trier à la main.

**Modèle** — `SubjectConfig.versions: list[SubjectVersion]`, chacune
`{vid, name, group, num_copies, header}` ; `Block.group` dit à quelle version
appartient un bloc. **`versions` vide = sujet à une version**, et tout le
chemin rendu/parsing reste alors identique — c'est ce qui protège les projets
déjà compilés et scannés (vérifié : recompiler un projet migré de 44 blocs
rend un calage `.xy` identique au bit près).

- ⚠ **Le groupe vit sur le `Block`, pas dans `data`** : `data` est ce qui part
  dans la banque de questions, et « matin » n'a aucun sens dans un autre projet.
- ⚠ **Un bloc sans groupe est mis dans `COMMON_GROUP` (`commun`)**, restitué par
  *toutes* les versions. Déclaré au niveau document hors de tout `\element`, il
  ne serait imprimé **nulle part**, sans la moindre erreur LaTeX.
- ⚠ **Le groupe n'est pas renommable** (`update_version` refuse) : c'est
  l'identité AMC de la version, le renommer désaffilierait toutes ses questions
  d'un coup. Le libellé `name` est là pour ça.
- ⚠ **Le nom d'une version est écrit en dernier sur le marqueur
  `%%QCM-VERSION`** et tout ce qui le suit lui appartient : `_parse_attrs`
  découpe sur les blancs et tronquerait « Session du matin » à « Session ».
- ⚠ **Pas de `\newpage` quand la feuille de réponses est conservée verbatim** :
  le découpage garde déjà le saut d'origine s'il y en avait un. En ajouter un
  insérait une page blanche sur un sujet dont la feuille commence par
  `\AMCdebutFormulaire` (qui fait la coupure lui-même) — mesuré : 6 pages au
  lieu de 4.
- `_slug_group()` : AMC construit un nom de macro depuis le nom de groupe, tout
  ce qui n'est pas une lettre ASCII casse la compilation sans message utilisable.

**Détection à l'import** — `_split_grouped_tex()` exige **les deux** moitiés :
des `\element{G}{…}` au niveau document *et* au moins un `\exemplaire` qui
restitue un de ces groupes. Un sujet AMCx avec `shuffle_questions` a bien des
`\element`, mais à l'intérieur de son `\exemplaire` et sur un groupe réservé
(`RESERVED_GROUPS` = `questions`, `open`, `bareme`) : il ne doit surtout pas
être lu comme un sujet à versions.

#### ⚠ Numéro AMC ≠ indice d'ordre du document

Le piège central, et la source de toutes les erreurs de notation trouvées ici.
`answers` dans `raw_responses/` est indexé par **numéro AMC** (celui du
calage) ; `parse_tex()` indexe ses QCM par **ordre du document**. Les deux
coïncident sur un sujet simple — un groupe, code étudiant après les questions —
et c'est exactement ce qui masquait la confusion. Avec deux versions, la copie 1
porte les questions AMC **1-5** et la copie 2 les **10-14**, alors que l'ordre
du document les numérote 1 à 10.

| fonction | attend | traduction |
|---|---|---|
| `effective_spec(q, copy)`, `get_bareme(copy)`, `max_score(q, copy)` | **indice document** | — |
| `score.score_question/score_copy`, `answers`, `layout` | **numéro AMC** | `amc_question_map(copy)["qcm"]` |
| `server.spec_of(q, copy)` / `max_of(q, copy)` | **numéro AMC** | wrappers, à utiliser côté serveur |

`sujet_store.tex_to_amc(copy)` fait la traduction inverse (indice → AMC), pour
les boucles qui partent des blocs du sujet (stats de banque, onglet Questions).

Ce qui a été corrigé au passage, et qui était déjà faux avant les groupes sur
tout sujet où les deux numérotations divergent (code étudiant en tête, par ex.) :
- `score.question_set(copy)` rend les numéros **AMC de cette copie** ; itérer
  l'ordre du document faisait chercher `answers[1]` sur une copie qui n'a que
  des clés 10-14 → **toutes les questions à zéro, sans un mot** ;
- `total_max(copy)` ne somme que les questions **de cette copie** — sommer tout
  le sujet donnait un barème sur 10 à des copies qui valent 5 ;
- `effective_spec`/`get_bareme` interrogeaient `_tex_chars` (indexé AMC) avec un
  indice document → `charmap = None`, options et bonnes réponses **vides**, donc
  une question à choix unique payée à toute copie qui n'y répond pas (mesuré :
  copie vide notée 2/5) ;
- `server.question_numbers(copy)` prend la copie ; sans elle, une feuille de
  l'après-midi n'affichait aucune de ses questions et en inventait cinq vides ;
- `amc_question_map` ne signale plus « QCM sans correspondance » pour une
  question dont le tag est connu du calage mais absent de **cette** copie : elle
  appartient à l'autre version. Le repli positionnel l'aurait collée sur un
  numéro de l'autre version.

`check_layout_consistency()` contrôle la première copie de **chaque** version ;
`doctor` liste les versions, leur plage de numéros et leur barème.

#### Import d'un sujet AMC : ce qui a été réparé

Testé sur un vrai sujet à deux groupes (ENSAI, 10 questions, 2 demi-journées) :

- les 10 questions vivant hors du `\exemplaire`, l'import n'en voyait **aucune**
  et écrivait un `subject.json` « canonique » à **0 bloc** — éditeur vide,
  `total_max` **0**, donc **toutes les copies notées 0**, en silence ;
- la **seconde version disparaissait** du store : `render_subject` ne rendait
  qu'un `\exemplaire`, donc la moitié après-midi du sujet. Latent tant que
  `blocks` est vide (`compile_pdf` compile alors le `.tex` tel quel), déclenché
  par le premier bloc ajouté ;
- ce qui suit `\end{reponses}` dans une question était **jeté** — typiquement le
  `\end{multicols}` dont l'ouverture est en fin d'énoncé : `\begin{multicols}`
  jamais fermé, sujet qui ne compile plus. Conservé désormais dans
  `data.epilogue`, la paire étant volontairement scindée entre `statement` et
  `epilogue` (la modéliser demanderait de comprendre l'imbrication LaTeX) ;
- un `\bareme{b=1,m=0}` collé après `\begin{question}{tag}` restait dans
  l'énoncé alors qu'il est déjà lu dans `value` et réémis → **deux `\bareme`**
  dans la même question dès qu'on changeait la valeur.

**Garde-fou** : `_migration_lost_questions()` refuse d'enregistrer une migration
qui ne sort **aucun** bloc QCM d'un `.tex` qui contient des `\begin{question}`.
Le projet est conservé, le sujet reste **legacy** (lecture seule, `.tex` compilé
tel quel, correction normale) et `POST /api/projects/create` renvoie un
`warning` que le front affiche en bandeau après le redémarrage (déposé dans
`sessionStorage`, sinon il serait perdu par le rechargement).

Vérification de bout en bout, sur ce sujet : import → recompilation →
**calage `.xy` identique au bit près** sur les deux copies, 4 pages, mêmes codes
imprimés ; puis scans fabriqués des deux versions → `_copy_id` lu (`printed`),
questions `[1..5]` et `[10..14]`, **5/5 sur chacune**.

**Routes** :

| Route | Rôle |
|---|---|
| `POST /api/sujet/versions/update` | `{vid, name?, num_copies?, header?}` |
| `POST /api/sujet/versions/add` | `{name?, group?, num_copies?, vid?, index?, header?}` |
| `POST /api/sujet/versions/delete` | `{vid, mode?}` → `{undo}` |
| `POST /api/sujet/versions/restore` | `{undo}` tel que rendu par delete |
| `POST /api/sujet/blocks/set-group` | `{bid, group}` → `{previous}` |
| `POST /api/sujet/header/analyze` | `{raw_tex}` → `{complete, fields, leftovers}` — n'écrit rien |
| `POST /api/sujet/header/to-raw` | `{fields}` → `{raw_tex}` — n'écrit rien |
| `POST /api/sujet/answer-sheet/to-raw` | `{fields, num_copies?}` → `{tex}` — n'écrit rien |

Toutes renvoient les plages de numéros **recalculées pour toutes les versions**
(`server._versions_payload()`, unique implémentation, partagée avec la page) :
changer le nombre de copies d'une version décale toutes les suivantes, et le
front ne peut pas le deviner.

`/sujet` affiche un tableau des versions dans le bandeau et une pastille de
version sur chaque bloc — sans elle, deux questions voisines dans la liste
partent sur deux sujets différents sans que rien ne le montre. Le « Total
barème » de la toolbar devient **par version**.

⚠ **Un bloc « commun » compte dans TOUTES les versions**, pas dans aucune : il
est imprimé sur chacune. La colonne « QCM » du tableau et le total de barème
(serveur *et* `refreshTotalMax` côté client) l'ajoutent donc à chaque version.
Compté à part, il apparaissait comme une pseudo-version au barème propre.

⚠ **Le barème d'une version se calcule depuis SES QUESTIONS**, jamais depuis le
numéro de sa première copie : `sujet_store.version_total_max(subject, group)`,
qui somme `max_score` sur les QCM du groupe **plus les communs** (`max_score`
ne dépend pas de la copie — seule la carte des lettres en dépend). Ne pas
confondre avec `total_max(copy)`, qui passe par le calage pour savoir quelles
questions AMC porte une copie donnée : c'est lui qui note les copies.

Le lire par `total_max(first_copy)` avait deux défauts, tous deux constatés :
une version tout juste créée n'est dans aucun calage, et surtout **changer le
nombre de copies d'une version fait disparaître le barème des suivantes** — le
décalage sort leur première copie du calage compilé, alors que leur barème n'a
pas bougé d'un point.

⚠ **Une seule fonction rafraîchit le tableau des versions**
(`applyVersionsPayload`, nourrie par la réponse de *toute* écriture : update,
set-group). Les mises à jour partielles laissaient des cellules périmées, et
`applyBlockGroup` recomptait les QCM en parallèle du serveur — deux vérités
qui finissent par diverger.

#### Ajouter / supprimer une version, affecter un bloc

- **Le sélecteur de version de chaque bloc EST l'affectation**
  (`.block-group-sel`, `POST /api/sujet/blocks/set-group`). Sans lui, une
  version ajoutée resterait vide à jamais et le groupe d'un bloc ne se
  changerait qu'en éditant le store à la main. Il remplace la pastille en mode
  canonique ; la pastille reste en legacy.
  ⚠ Il est **exclu du marquage `dirty`** (`select:not(.block-group-sel)`) : la
  version vit sur le `Block`, pas dans son `data` (`data` part dans la banque,
  où « matin » n'a aucun sens). Elle est enregistrée seule et tout de suite ;
  la marquer « non enregistrée » réclamait une sauvegarde qui n'avait rien à
  écrire, et faisait surgir la confirmation « blocs modifiés » au milieu d'une
  annulation.
- ⚠ **`add_version` sur un sujet SANS versions en crée DEUX** : la version
  courante — jusque-là implicite, portée par `cfg.header` et `cfg.num_copies` —
  puis la nouvelle. Sans ça le sujet passait à « une version vide + des
  questions orphelines », que le rendu range en `commun` : la nouvelle version
  aurait imprimé le sujet entier. Les blocs existants **gardent leur groupe**
  (vide = commun, donc imprimés des deux côtés) : ranger d'office les questions
  dans la première version les ferait disparaître de la seconde en silence.
  La modale le dit avant d'agir.
- **Supprimer une version pose la question du sort de ses questions**, elle ne
  la devine pas : `mode="reparent"` (défaut) les rend **communes** — aucune
  question n'est jamais supprimée, comme pour les catégories de la banque —,
  `mode="delete_blocks"` les emporte. La modale nomme la version et compte ses
  questions ; sans question propre, le choix n'est pas proposé.
- **Supprimer la dernière version** ne laisse pas un sujet sans `\exemplaire` :
  le sujet redevient un sujet à une version (`versions = []`) et **reprend
  l'en-tête et le nombre de copies** de celle qu'on retire. C'est ce qui rend
  l'opération réversible.
- `delete_version` rend un **payload d'annulation** (identité, position, état
  exact des blocs touchés) que `restore_version` réapplique **sous le même
  verrou** : le front ne reconstruit rien, sinon il restaurerait une version
  amputée. Vérifié sur le vrai projet à 2 versions : supprimer (les deux modes)
  puis annuler rend un `subject.json` identique **et un `.xy` recompilé
  byte-identique**.
- `_unique_group()` refuse un groupe déjà pris, `COMMON_GROUP` et les
  `RESERVED_GROUPS` : deux versions au même groupe restitueraient les **mêmes**
  questions, et LaTeX ne dirait rien.
- `set_block_group` **refuse un groupe qu'aucune version ne restitue** : le
  rendu le traiterait en commun, donc imprimé partout au lieu de nulle part —
  silencieux dans les deux cas.

#### ⚠ Ctrl+Z qui survit à un rechargement (`pushPersistentUndo`)

Les actions de version rechargent la page — la table, les sélecteurs de chaque
bloc et la numérotation par version en dépendent —, ce qui **vide la pile
Ctrl+Z**, qui vit en mémoire. Sans relais, l'action la plus lourde de l'éditeur
aurait été la seule non annulable.

Seules les actions dont l'annulation est un **appel serveur paramétrable**
passent par là (une fermeture ne se sérialise pas) : on persiste dans
`sessionStorage` le *nom* de l'action et sa charge utile, **jamais du code**.
`UNDO_KINDS` est le registre des annulations rejouables (`version-restore`,
`version-remove`).

- ⚠ **C'est une pile, pas une case unique.** Enchaîner deux suppressions de
  version est courant ; une case unique n'aurait gardé que la seconde et le
  Ctrl+Z suivant n'aurait plus rien eu à défaire, sans un mot. Chaque entrée
  porte un `id` pour être retirée par identité, et n'est retirée qu'**une fois
  l'appel serveur passé** — un échec doit laisser l'action annulable, comme
  dans `UNDO.run`.
- Au chargement, les entrées sont remises en pile **dans l'ordre** et le bouton
  « ↩ Annuler » de la dernière est réaffiché : sinon le filet disparaissait au
  moment précis où l'on en a besoin. Passé 15 min, l'entrée est purgée — elle
  n'est plus dans la tête de l'utilisateur et resterait annulable par un Ctrl+Z
  distrait.
- Vérifié de bout en bout : supprimer les deux versions, ajouter une version
  (chemin bootstrap), puis trois Ctrl+Z → sujet identique à l'octet près.

### Bandeau global (`<details>` repliable en haut)

- **Randomisation** : `num_copies`, `random_seed` (+ bouton ♻ régénérer),
  `shuffle_answers`, `shuffle_questions` (= insertion `\melangegroupe{questions}`
  + wrap `\element{questions}{...}` autour des questions).
- **En-tête du sujet** : 2 sous-groupes pliables :
  - *Champs structurés* (établissement, année, auteur, **date**, titre, durée,
    sous-titre, **filets**, instructions) → génère un tableau LaTeX +
    centerblock.
  - *LaTeX brut* (textarea) → `header.raw_tex` prime si rempli. C'est le cas
    par défaut après migration legacy (l'en-tête original est préservé
    verbatim). En legacy : affiché en `<pre>` readonly.

⚠ **Avec des versions, `cfg.header` n'est imprimé NULLE PART** : chaque
`\exemplaire` rend le sien (`_render_multi_version_subject`). Le formulaire
éditait donc un en-tête qu'aucune copie ne porte, et les en-têtes réellement
imprimés — un par version, en LaTeX brut après import — n'étaient éditables
par aucune UI. D'où le sélecteur **« En-tête de la version »** en tête du
bloc : le formulaire patche alors `versions/update {vid, header}`.
⚠ **La version visée est capturée à la FRAPPE, pas à l'envoi** : la
sauvegarde est différée de 500 ms, et changer de version pendant ce délai
écrivait la fin de l'en-tête du matin sur celui de l'après-midi. Le
changement de version *vide* d'abord la file (`_hdrFlush`), puis remplit les
champs.

⚠ **L'état grisé du bloc structuré suit la frappe** (`syncHeaderRawMode`) :
vider le LaTeX brut pour « revenir aux champs » laissait tout désactivé
jusqu'au rechargement.

**Ce qui manquait pour se passer du LaTeX brut** — audit mené sur les deux
en-têtes réels du dépôt (EXAM_2026 et le sujet ENSAI importé) :

| élément de l'en-tête réel | champ |
|---|---|
| `ENSAI - 1A`, `ENSAI - 2A — MCQ on…` | `establishment` |
| `Année 2025-2026` | `year` |
| `Emmanuel Pilliat` | `author` |
| `8/9/2026` | **`date` (ajouté)** |
| `Examen : Introduction aux Tests…` | `title` |
| `(durée : 2 heures)`, `Duration: 10 min` | `duration` |
| `Calculatrice autorisée.`, `Morning` | `subtitle` |
| `\hrule` autour des consignes | **`rules` (ajouté)** |
| les 3 paragraphes de consignes, gras compris | `instructions` (LaTeX libre) |

Les deux en-têtes se réécrivent donc entièrement en champs. Ce qui reste
propre au brut est la **mise en page** (position exacte des `\vspace`,
`\hfill` sur la même ligne), pas l'information.

#### « 🔎 Détecter les champs » — `sujet_store.analyze_header_tex`

Un sujet importé garde son en-tête **verbatim** dans `raw_tex` : c'est ce qui
garantit un `.xy` identique au bit près, donc utilisable sur des copies déjà
imprimées. Le prix, c'est un en-tête qu'on ne peut plus éditer autrement qu'en
LaTeX. Le bouton propose de le répartir dans les champs.

`analyze_header_tex(raw) -> {"fields", "leftovers", "ok"}` est **pure** — zéro
I/O, testable seule. Route `POST /api/sujet/header/analyze {raw_tex}`, qui
n'écrit rien : le LaTeX vient du **champ de saisie**, pas du store, pour que
l'analyse porte sur ce que l'utilisateur a sous les yeux (éditions non
enregistrées comprises). ⚠ Son verdict s'appelle `complete` dans la réponse,
pas `ok` — `ok` dit déjà que la requête a abouti, et les confondre ferait
passer « je n'ai rien su décomposer » pour une panne.

Découpage appris des en-têtes réels : zone d'identité (jusqu'au
`\begin{center}`, à défaut jusqu'au premier `\hrule`) découpée en lignes sur
`\\`/`\par` puis en cellules sur `\hfill` ; bloc centré (gras = titre,
italique = sous-titre) ; le reste = consignes.

- ⚠ **C'est une proposition, jamais une conversion silencieuse.** Elle change
  forcément la mise en page, donc le `.xy` : l'appliquer sur un sujet déjà
  imprimé désaligne les copies. La modale le dit, et rien ne part sans
  confirmation. Annulable par Ctrl+Z (pile en mémoire — pas de rechargement
  ici).
- ⚠ **Ce qui n'a pas trouvé de champ est LISTÉ, jamais avalé** : un fragment
  perdu en silence, c'est une ligne qui disparaît de l'en-tête imprimé.
  L'encart orange s'affiche **au-dessus** du bouton « Appliquer ».
- ⚠ **Une structure n'est pas coupée en deux champs.** `_hdr_placeable` refuse
  les accolades déséquilibrées, les `\begin{…}` et les commandes qui ne sont
  pas du texte de ligne (`\includegraphics`, `\input`…) : un `tabular`
  réparti sur deux champs donne un sujet qui ne compile plus, et un logo à la
  place du nom de l'établissement est *plausible et faux*. Le nettoyage ne
  rogne plus les accolades à l'aveugle non plus — `\includegraphics{logo.png}`
  y perdait la sienne.
- ⚠ **Les motifs sont testés sur du texte REPLIÉ** (`_hdr_fold` : accents
  LaTeX et Unicode ôtés). Les vrais sujets écrivent `dur\'ee`, `Ann\'ee` :
  chercher « durée » n'y trouvait rien et la durée restait collée au titre. Ce
  qui est rangé dans les champs reste le texte d'origine, accents compris.
- ⚠ **Une ligne vide des consignes est conservée** : en LaTeX c'est une fin de
  paragraphe. Les jeter avec la présentation collait les trois consignes
  d'EXAM_2026 en un seul bloc — constaté en compilant.
- « 8/9/2026 - Morning » est scindé en `date` + `subtitle` : laissés ensemble,
  « Morning » s'imprimait en haut à gauche.

**Mesuré** sur les trois en-têtes réels du dépôt (les deux versions de QCM1 et
EXAM_2026) : `ok = True`, aucun fragment non reconnu, et le sujet recompilé
donne les mêmes 4 pages avec les mêmes codes imprimés. Ces trois en-têtes sont
figés verbatim dans `tests/test_versions.py` — ce sont eux qui ont dicté le
découpage.

#### Bascule champs ⇄ LaTeX brut (en-tête ET feuille de réponses)

Quatre boutons, deux par bloc. `sujet_store.header_to_raw(h)` et
`answer_sheet_to_raw(a, num_copies)` rendent le LaTeX que les champs
produisent ; routes `POST /api/sujet/header/to-raw` et
`/api/sujet/answer-sheet/to-raw`, qui **n'écrivent rien** — le front applique
par l'écriture habituelle, ce qui rend la bascule annulable par Ctrl+Z comme
le reste (pile en mémoire, aucun rechargement).

⚠ **Les deux sens ne se valent pas**, d'où une confirmation d'un seul côté :

| sens | effet sur le PDF |
|---|---|
| champs → brut | **aucun** : on fige exactement ce qui aurait été imprimé |
| brut → champs | la mise en page est régénérée → le **calage** change |

Pour la feuille de réponses le retour aux champs est bien plus lourd que pour
l'en-tête : c'est elle qui porte les **cases**. Des copies imprimées avec
l'ancienne feuille ne se lisent plus. La confirmation le dit et donne la
taille du LaTeX qui sera remplacé.

⚠ **Le `\newpage` fait partie du texte figé** (`answer_sheet_to_raw` le met en
tête). `render_subject` l'émet à CÔTÉ de la feuille canonique et cesse de
l'émettre dès que `cfg.answer_sheet_tex` est rempli — une feuille importée
porte déjà sa coupure. Sans lui, « passer au brut » supprimait un saut de
page : **mesuré, le `.xy` changeait**, donc toutes les positions de cases.
La promesse affichée (« le PDF est inchangé ») est vérifiée par
`TestSwitchKeepsRendering` et, de bout en bout, par une compilation
avant/après : `b7035b8d9cbeef08` des deux côtés, aller **et** retour.

⚠ **`_strip_meta_markers` ne touche QUE les lignes de marqueurs** — ni les
lignes vides, ni les blancs de début et de fin. Une ligne vide est une fin de
paragraphe LaTeX ; les « ranger » faisait que le texte figé ne rendait plus
tout à fait comme les champs dont il sortait.

⚠ **`header_to_raw` rend un en-tête déjà brut tel quel** : le « repasser » en
brut l'écraserait par le rendu, vide, des champs.

⚠ La feuille de réponses ne se **recharge plus** au passage rempli ⇄ vide (le
`location.reload()` abandonnait sans prévenir la pile Ctrl+Z et les éditions
de blocs en cours) : `syncAnswerSheetRawMode()` suit la frappe, comme
`syncHeaderRawMode()`.

⚠ `.bd-mode-switch` doit poser `flex-direction: row` : `.bd-field` impose
`column`, et sans l'écraser le `flex-basis` de la note s'appliquait à sa
**hauteur** — la ligne mesurait 261 px au lieu de 69.

⚠ Trois défauts du rendu structuré corrigés au passage, chacun **silencieux** :
le tableau d'identification était émis même vide (bande blanche en haut d'un
sujet qui n'a qu'un titre) ; `duration` et `subtitle` étaient subordonnés à
`title`, donc les renseigner sans titre ne rendait **rien** ; et un `\\`
traînait avant `\end{center}`, ce qui ouvre une ligne vide. Un en-tête
structuré **déjà en service** rend toujours exactement les mêmes octets
(test `test_en_tete_sans_date_reste_inchange`) — son `.xy`, donc le calage des
copies imprimées, en dépend.
- **Feuille de réponses** : `id_grid_digits`, `name_field`, `columns`. En
  legacy : disabled. Préservée verbatim après migration via `answer_sheet_tex`.
- **Sélecteur copie** `[Copie : N ▼]` pour debug (visualiser le mapping
  case ↔ lettre selon la copie).
- **Zone dangereuse** (legacy seul) : bouton **🔥 Migrer vers le format canonique**
  → `migrate_to_canonical()` ajoute les marqueurs `%%QCM-…` autour de chaque
  morceau structurel. **Backup auto** `sujet/exam.tex.legacy-backup`. **Calage
  `.xy` byte-identique avant/après** (vérifié SHA256 sur EXAM_2026 :
  `bb78eb3d97b26e34` → `bb78eb3d97b26e34`) → 0 risque de désaligner les copies
  scannées (tant qu'on ne modifie rien après migration).

### Liste centrale unifiée (text / qcm / open)

- Chaque bloc dans une `<section class="sujet-block" data-bid="…" data-kind="…">`.
- **`block-toolbar`** par bloc : drag handle `☰` (**seulement la poignée** est
  `draggable="true"`, pas la section entière — sinon les textareas ne reçoivent
  pas le clic), badge kind coloré, `q-dirty-dot ●`, et en canonique ▲ ▼ ⎘ ✕
  (déplacer haut/bas, dupliquer, supprimer).
- **QCM** : édition complète (tag, type, env, énoncé, réponses), boutons
  `+ réponse` / `✕ réponse` par bloc, toggle bonne/mauvaise (clic badge),
  barème par réponse (`+1/2` vert / `0` gris / `-1/2` rouge).
- **OPEN** : tag, lines, points, statement, `grading_cases` éditables
  (label + value, `+ case` / `✕`).
- **Text** : textarea + preview KaTeX live. En legacy = lecture seule
  (`<pre>` repliable « voir / replier le LaTeX brut »).

### Toolbar d'ajout (canonique seul)

`[+ texte libre] [+ QCM choix unique] [+ QCM choix multiple] [+ question ouverte]`
→ insertion en fin de liste avec `/api/sujet/blocks/add` puis reload.

### Aperçu PDF : lien bidirectionnel avec l'éditeur

Le panneau droit empile **toutes** les pages du PDF (`/sujet/page/<n>.png`,
images `loading=lazy`) et pose un rectangle absolu `.pv-zone[data-q]` par
question, positionné en `%` depuis `/sujet/regions.json`. Deux états, **les
mêmes couleurs des deux côtés** (bloc éditeur `.sujet-block` ↔ rectangle
`.pv-zone`) :

| État | Couleur | Classe bloc | Classe zone | Déclencheur |
|---|---|---|---|---|
| courant | bleu `#2769d8` | `is-preview-active` | `is-current` | survol d'un bloc, ou question traversée en scrollant l'aperçu |
| sélectionné | magenta `#c026a8` | `is-preview-selected` | `is-selected` | clic sur un bloc **ou** sur une zone de l'aperçu |

Le magenta reprend le code couleur de la correction (cases douteuses). Le
survol prime sur le scroll ; le magenta prime sur le bleu pour une même
question. `Escape` lève la sélection.

Les **deux vues** — panneau droit et vue agrandie — rendent le même DOM et
partagent l'état : `pvRenderInto(host, data)` construit l'une ou l'autre,
`pvApply()` applique les classes via `document.querySelectorAll('.pv-zone')`,
et `pvActiveHost()` désigne celle que le scroll-spy écoute (la vue agrandie
si elle est ouverte, sinon le panneau). La vue agrandie n'embarque donc plus
le PDF natif — il reste accessible par « 📄 Voir le PDF » de la toolbar.

Sens du recentrage — asymétrique **à dessein** : on ne bouge jamais la vue
que l'utilisateur est en train de regarder.
- clic sur un **bloc** → l'aperçu scrolle sur la question ;
- clic sur une **zone** → l'éditeur scrolle sur le bloc ;
- clic dans le panneau **hors** de toute zone → vue agrandie sur cette page.

⚠ **Le bandeau du panneau d'aperçu ne porte plus qu'un avertissement.** Le
titre « Aperçu PDF — Q1 · Morning — tag » répétait ce que la sélection magenta
montre déjà des deux côtés, et le mode d'emploi du clic occupait une ligne à
demeure. Il ne reste que « *… pas dans le PDF compilé* » pour un bloc
sélectionné absent du calage — sans ça rien n'expliquerait l'absence de cadre.
Vide, la ligne disparaît (`.preview-title:empty { display: none }`).

**Géométrie des régions** (`sujet_store.pdf_regions`) : les cases du calage ne
disent pas où commence un énoncé. `_pdf_region_hints()` lit donc le texte du
PDF (PyMuPDF) et `_statement_top()` remonte ligne à ligne depuis les cases,
s'arrêtant au premier saut de paragraphe (gap > 1,15 × hauteur de ligne) ou au
premier titre (police > 1,15 × la médiane de la page). Sans ça la 1re question
d'une page part du haut de la feuille et englobe l'en-tête et le titre du
sujet. Le bas d'une région s'arrête avant l'énoncé de la suivante.

`_apply_answerbox_regions()` traite le cas des `answerbox` : leurs seules
cases sont la ligne de barème « Réservé correcteur » de la feuille de
réponses, donc leur numéro AMC pointe vers l'évaluation et pas vers le cadre
où l'étudiant écrit. La région est remplacée par ce cadre, retrouvé dans le
PDF (rectangle pleine largeur ≥ 60 % de la page et ≥ 150 px de haut, plus sa
ligne d'en-tête), apparié au bloc par son **titre** puis par ordre.

`_apply_text_block_regions()` localise les blocs `text`, qui n'ont **aucune
case** dans le calage. Deux temps : (1) le bloc est forcément entre la région
du bloc localisé qui le précède et celle de celui qui le suit — ça borne la
recherche ; (2) on apparie les mots du LaTeX (`_plain_words`) aux lignes du
PDF dans cette bande. L'étape 2 est indispensable — la bande du 1er bloc part
du haut de page et contient l'en-tête de l'examen — et ne suffit pas seule :
pour `\section*{Questions}`, « questions » apparaît aussi dans les consignes.
On départage par la **proximité au bloc suivant** (un intertitre introduit ce
qui vient après). Sans correspondance, pas de région : mieux vaut aucun cadre
qu'un cadre faux.

⚠ Ces régions sont indexées par **bid** (chaîne), pas par numéro AMC — d'où
les clés mixtes `int | str` de `pdf_regions()`. Ne pas faire
`sorted(regions.items())` (TypeError) : trier par `(page, y0)`.

⚠ **Le calage numérote les pages par copie, le PDF les concatène.**
`\page{2/1/58}` est la 1re page de la copie 2, soit la 3e page du PDF quand la
copie 1 en fait deux. `Layout.pdf_page()` / `pdf_page_map()` font la
conversion, à partir de la position dans `page_ids` (trié par copie puis page,
c'est-à-dire l'ordre d'impression) — donc sans supposer un nombre de pages
constant d'une version à l'autre. Sans cette conversion, toutes les régions de
la seconde version se posaient sur les pages de la première.

⚠ **Une passe par version** (`sujet_store.region_copies()` : la 1re copie de
chaque version, `[1]` sans versions). Prendre toutes les copies ferait pointer
chaque question sur la dernière copie imprimée, ne prendre que la copie 1
laissait la seconde moitié du sujet **sans aucun aperçu** — c'est ce qui se
voyait comme « un seul groupe dans la preview ».

⚠ **Le filtre des cases porte sur le RÔLE, pas sur la page.** Il excluait la
page de la feuille de réponses (`b.page != asp`), ce qui perdait toute question
imprimée sur cette même page — la 5e question de chaque version, qui tombe
juste avant `\AMCdebutFormulaire`, n'avait aucun cadre. On garde désormais les
cases `ROLE_QUESTIONONLY` (position de la question dans le questionnaire) et on
écarte les `ROLE_ANSWER`, sauf la ligne de barème d'un answerbox.

⚠ **`block_preview_keys` mappe les QCM par leur TAG**, pas par leur position.
Les deux coïncident sur un sujet simple ; avec plusieurs versions la 6e
question du document porte le numéro AMC 10, et l'indexer par sa position
collait son cadre d'aperçu sur la question de l'autre version.

#### Numéro affiché : le rang DANS SA VERSION

⚠ **Trois numérotations, et les confondre a été constaté.** `server.question_stats()`
rend les trois explicitement, et **aucune page ne doit en dériver une quatrième** :

| champ | c'est | sert à |
|---|---|---|
| `q` | l'ordre du **document** | clé de `parse_tex()`, du barème, de `answers` |
| `preview_q` | le **numéro AMC** (via `block_preview_keys()`) | trouver la région de l'aperçu PDF |
| `q_in_version` + `version` | ce qui est **imprimé sur la copie** | tout ce qui s'affiche |

Le défaut, signalé en usage sur un sujet à deux versions de 5 questions :
l'onglet Questions demandait l'aperçu de « Q10 » et recevait le cadre de la
**question AMC 10**, c'est-à-dire la *première* question de l'après-midi
(imprimée « Question 1 ») ; et les questions 6 à 9 du document tombaient sur les
**colonnes du code étudiant** (`etu[1]`…`etu[4]`, qui occupent les numéros AMC
6-9), donc n'avaient aucun aperçu — « Q8 ne donne pas de rendu PDF ».

`server.qcm_rank_in_version(blocks)` est l'**unique** implémentation du rang
imprimé, partagée par l'onglet Sujet, l'onglet Questions et la page Évaluation.
Trois comptages parallèles, c'est trois pages qui nomment la même question
différemment.

#### ⚠ Le haut d'une région ne remonte pas au-dessus des cases de la précédente

`_statement_top()` remonte ligne à ligne dans le **texte** du PDF et s'arrête au
premier saut de paragraphe. Il ne voit pas les cases à cocher : quand deux
questions sont serrées — pas de blanc entre les réponses de l'une et l'énoncé de
l'autre — il remontait jusqu'à l'énoncé précédent. Résultat mesuré sur deux
sujets réels : la région de la question N faisait **20 px de haut** (donc vide,
aucun aperçu) et celle de la question N+1 affichait **les deux questions**.

Le calage, lui, sait exactement où finissent les cases de la précédente : il sert
désormais de **plancher** au haut de région. Effet mesuré : sur un sujet de 31
questions, **18 régions sur 35 étaient fausses** ; après correction, les 32
cadres montrent chacun exactement leur question (`Question N` et une seule), et
10/10 sur le sujet à deux versions. Le contrôle qui le vérifie est le bon
réflexe : extraire le texte de chaque région avec PyMuPDF et comparer l'en-tête
« Question N » au numéro affiché.

`item["q_in_version"]` (route `/sujet`) est le rang du QCM parmi ceux de son
groupe — c'est le numéro imprimé sur la copie de l'étudiant. Le rang global
`q` reste la clé de `parse_tex()` et du barème, et reste porté par `data-q`
(le chemin legacy `/api/sujet/save` l'utilise) ; l'affichage passe par
`data-qv`. Afficher « Q6 » sur la 1re question de l'après-midi n'a aucun sens :
cette question est imprimée « Question 1 » sur son sujet.

`qLabelOf(blk, {withVersion})` construit le libellé : `Q1` pour la pastille du
bloc (la version est déjà dite par la pastille de groupe à côté) et
`Q1 · Afternoon` pour les cadres de l'aperçu, qui n'ont aucun autre contexte.
La Structure ajoute une puce de version à droite de chaque ligne — sans elle,
elle affiche deux fois « Q1 … Q5 » sans dire laquelle est laquelle.

⚠ **Le total du barème est recalculé côté client** à chaque édition
(`refreshTotalMax`). Il sommait toutes les questions de la page : sur deux
versions à 5 points, le bandeau annonçait « 10.00 par version », écrasant la
valeur correcte rendue par le serveur. Il somme désormais **par groupe**, et
masque la mention « par version » quand les totaux diffèrent.

#### Aperçu : panneau redimensionnable et vue agrandie en fenêtre

- **Poignée `.sujet-gutter`** entre l'éditeur et l'aperçu : glisser règle
  `--pv-w` (le `flex-basis` de `.sujet-right`), persisté. Sous 150 px on replie,
  et c'est le **même état** que le bouton 👁 (`.no-preview`) — sinon « replié à
  0 px » et « masqué » seraient deux états distincts qui se contrediraient au
  rechargement. Double-clic replie, flèches ← → au clavier.
- **La vue agrandie est une fenêtre**, plus une lightbox : ni fond opaque ni
  `inset: 0`, donc l'éditeur reste visible **et cliquable** derrière — c'est ce
  qui permet l'aller-retour entre les deux. Déplaçable par son bandeau,
  redimensionnable par la poignée d'angle, réductible au seul bandeau
  (double-clic sur le bandeau), bascule plein écran. Position, taille et état
  réduit sont persistés.
- ⚠ `clampToViewport` garde toujours **80 px de bandeau** dans l'écran : une
  fenêtre poussée dehors serait irrécupérable sans vider le `localStorage`.
  Une fenêtre rouverte alors qu'elle était réduite est ré-ouverte déployée,
  sinon le clic sur « vue agrandie » ne montrerait rien.
- ⚠ **Une seule déclaration `position` sur `.sujet-gutter`.** La règle en
  portait deux (`sticky` puis `relative`) et la seconde gagnait : la poignée
  restait en haut de la colonne au lieu de suivre le défilement, donc
  inattrapable dès qu'on descendait dans le sujet.

#### Annulation (Ctrl+Z) — pile d'actions, pas d'action inverse

Les champs de saisie ont déjà l'annulation native du navigateur. Ce qui n'en
avait **aucune**, ce sont les actions par clic : basculer une réponse en
mauvaise, ajouter ou supprimer une réponse, déplacer un bloc.

- ⚠ **Chaque entrée restaure l'état capturé avant l'action**, elle ne rejoue
  pas l'action inverse. C'est ce qui rend correct le cas du **choix unique** :
  cocher une réponse y décoche l'ancienne, et « re-basculer » ne saurait pas
  laquelle remettre. `snapCorrect()` / `restoreCorrect()` reposent tout l'état
  des réponses du bloc.
- ⚠ **Ce qui départage native et applicative, c'est la dernière ACTION, pas
  l'endroit où se trouve le curseur.** La première version cédait à
  l'annulation native dès que le focus était dans un champ : ça ne marchait
  que tant que le focus restait sur le bouton cliqué, et il suffisait de
  cliquer ensuite dans un énoncé pour que Ctrl+Z ne fasse plus **rien du
  tout** — la native n'avait aucune frappe à défaire et la nôtre était
  court-circuitée. C'est le défaut qui a été signalé et reproduit. On ne cède
  donc à la native que si l'utilisateur a réellement tapé **depuis** la
  dernière action empilée (`LAST_TEXT_EDIT` vs `UNDO.lastPushAt()`).
- ⚠ **Porte de sortie obligatoire** (`LAST_DEFER`) : céder à la native tant
  qu'elle *pourrait* avoir de l'historique enfermait Ctrl+Z dans le champ, car
  elle finit par n'avoir plus rien à défaire sans qu'on puisse l'interroger.
  On mémorise donc la valeur du champ à chaque cession : inchangée au coup
  suivant = la native est à court, on reprend la main. Coût mesuré : **une
  frappe Ctrl+Z de plus** pour franchir la frontière, et seulement quand on a
  tapé du texte avant de vouloir annuler une action antérieure.
- ⚠ **Une entrée capture une POSITION, pas un déplacement de ±1**
  (`moveBlockTo(blk, afterBid)`). Rejouer « une case dans l'autre sens » était
  faux dès qu'autre chose bougeait entre-temps : un ▼ puis un glisser-déposer,
  et l'annulation produisait un ordre jamais visité. Les **deux** chemins de
  glisser-déposer (bloc et Structure) empilent aussi — ils ne le faisaient pas,
  alors que c'est la façon annoncée de réordonner.
- ⚠ **`UNDO.run` est `async` et remet l'entrée en pile sur échec.** Elle
  dépilait avant le `try` et annonçait « Annulé ✓ » de façon synchrone : une
  annulation asynchrone refusée par le serveur était perdue de la pile *et*
  annoncée comme réussie.
- ⚠ **La suppression d'un bloc est dans la pile.** Sans ça, le Ctrl+Z qui suit
  une suppression dépilait l'entrée *précédente* — sur un nœud détaché, donc
  sans effet — annonçait « Annulé ✓ », et son `setStatus` effaçait le bouton
  « ↩ Annuler » avant les 12 s : le réflexe naturel après une suppression
  accidentelle était le geste qui rendait le bloc irrécupérable.
- ⚠ **Ctrl+Z ne fait rien quand une modale est ouverte** (`aModalIsOpen`) :
  il mutait l'éditeur derrière elle, la confirmation s'affichant dans une barre
  de statut masquée. Ctrl+Shift+Z / Ctrl+Y répondent « pas de rétablissement »
  au lieu de rester muets — muet, rien ne distingue « non implémenté » de
  « raccourci non reçu ».
- Une réponse supprimée est **réinsérée telle quelle** : le nœud détaché garde
  ses écouteurs, donc elle reste éditable et basculable sans reconstruire le
  markup (vérifié).
- La suppression d'un **bloc** garde son bouton « ↩ Annuler » de 12 s, qui doit
  repasser par le serveur (le bloc est réellement supprimé côté store) — ce
  n'est pas la même mécanique qu'une annulation purement DOM.

`GET /sujet/regions.json` n'expose que les pages couvertes par le calage :
avec `\exemplaire{N}` le PDF contient N copies, mais le calage ne décrit que
la copie 1 — les autres pages n'auraient aucun cadre.

Le recentrage n'a lieu que si la sélection *change* — sinon chaque clic dans
un textarea du bloc déjà sélectionné ferait sauter l'aperçu.

⚠ Pièges de ce module (état dans l'objet `PV` de `sujet.html`) :
- **`aspect-ratio` sur `.pv-page`** posé en JS depuis les dimensions réelles
  de la page : sans lui, les images `lazy` non chargées ont une hauteur nulle
  et la géométrie du scroll (spy + recentrage) est fausse sur toutes les
  pages sauf la première.
- **`PV.syncing`** gèle le scroll-spy pendant un scroll programmatique, sinon
  le cadre bleu clignote sur chaque question traversée.
- **`pdf_mtime` vaut `0`** quand le PDF n'existe pas → `PDF_V === '0'`, qui est
  *truthy* en JS. Tester `!PDF_V || PDF_V === '0'`.
- Le panneau est masqué par défaut (`display:none` via `.no-preview`) : tous
  les `getBoundingClientRect()` valent 0. `pvVisible()` court-circuite le spy,
  et le toggle 👁 rejoue `pvBuild()` + recentrage au ré-affichage.
- L'IIFE du toggle 👁 s'exécute **avant** les `const PV` / `previewBody`
  déclarés plus bas dans le même `<script>` → l'appel à `pvBuild()` y est
  différé en `requestAnimationFrame` (sinon ReferenceError de TDZ).

### Déplacement d'un bloc (▲ / ▼) — animation et repérage

Trois mécanismes, dans `sujet.html` :
- **`animateBlockSwap(a, b, apply)`** anime l'échange en **FLIP** : mesurer les
  positions, appliquer la mutation DOM, remesurer, repartir de l'ancienne
  position (`translateY`) vers la nouvelle. Sans ça les deux blocs permutent
  instantanément et on perd de vue lequel a bougé.
- **`flashMoved(blk)`** pose la classe `just-moved` (keyframe `blockMovedFlash`)
  sur le seul bloc déplacé — l'animation d'échange ne dit pas *lequel* des deux
  a été déplacé quand ils se ressemblent. Appelé aussi après un drag&drop.
- **`bringBlockIntoView(blk)`** recentre, mais **seulement si le bloc n'est pas
  déjà bien visible** : scroller pour rien désoriente plus que ne pas scroller.
  La bande occultée par les deux barres collantes (`.topbar` 44 px + la
  `.sujet-toolbar` qui passe sur 2 lignes en fenêtre étroite) est **mesurée**
  par `stickyTopOffset()`, pas codée en dur.

⚠ **`layoutTop(el)` plutôt que `getBoundingClientRect()`** pour calculer la
cible du scroll : le rect **inclut les `transform`**, donc pendant le FLIP il
renvoie l'ANCIENNE place du bloc et le recentrage tombe à côté (constaté : le
bloc arrivait 91 px au-dessus des barres collantes). La chaîne des `offsetTop`
donne la position de mise en page, insensible au transform.

Le focus est rendu au bouton utilisé (`focus({preventScroll: true})` — sans
l'option, le focus annule le recentrage qu'on vient de faire), pour pouvoir
enchaîner les déplacements. `refreshMoveButtons()` grise ▲ sur le premier bloc
et ▼ sur le dernier ; il est appelé **depuis `buildOutline()`**, qui tourne
après tout changement de structure — un futur appelant ne peut pas l'oublier.

Tout est neutralisé sous `prefers-reduced-motion: reduce`.

### Blocs d'édition — dette de design corrigée

- Le ✕ « supprimer cette réponse » vivait dans `.ans-mark`, un flex column en
  `align-items: stretch` : il s'étirait sur les 134 px de la colonne. Il est
  désormais ancré en absolu dans le coin de `.md-answer` (d'où
  `position: relative` + `padding-right` sur la ligne).
- `plancher` / `plafond` : 54 px tronquaient le placeholder « aucun » → 70 px.
- Libellés `.field-label` / `.ans-char` / `.bar-grouplabel` / `.field-note` :
  #999–#aaa donnaient 2,3–3,5:1 sur blanc, sous le minimum AA de 4,5:1, alors
  que ce sont les seules étiquettes des champs → `#6b7280` (4,83:1).
- `.block-btn` : gabarit carré uniforme (26×23) pour toute la rangée, le ✕
  destructif ne se distinguant qu'au survol.
- **Densité** : une réponse tient sur une ligne (`rows="1"` + `autoGrowAnswer`,
  d'où `resize: none` — une poignée de redimensionnement mentirait, la frappe
  suivante recalculerait la hauteur), et `case D` + barème partagent une ligne
  (`.ans-meta`). Un bloc QCM passe de **680 à 484 px** (−29 %), une ligne de
  réponse de ~130 à **55 px**.
  ⚠ Le markup de `addAnswer()` (sujet.html) doit rester identique à celui de
  `_sujet_block.html` — sinon une réponse ajoutée ne se comporte pas comme les
  autres.
- **Une seule grammaire de formulaire** : libellé au-dessus du champ
  (`.bar-field`), partagée par l'en-tête des QCM, des questions ouvertes et des
  `answerbox`. `.q-horiz` ne sert plus qu'à la case à cocher « horiz », où
  l'alignement en ligne est le bon.
- **Suppression : confirmation PUIS annulation.** Le projet ne comptait d'abord
  que sur le bouton « ↩ Annuler » de 12 s (`setStatusAction`), en jugeant
  qu'une boîte de dialogue « ne protège personne ». Le pari a été perdu en
  usage réel : deux questions ont été supprimées sans que le retour arrière
  soit vu à temps. `deleteBlock` demande donc confirmation avant d'agir, et le
  message **nomme ce qu'on va perdre** (`describeBlock` : « la question Q4
  “pente” (Morning) et ses 4 réponses ») — c'est ce qui distingue une
  confirmation utile d'un réflexe « OK ». Les deux filets sont gardés.
  ⚠ L'annulation **réutilise le bid d'origine** (`POST /api/sujet/blocks/add`
  accepte `bid`) : il est gravé dans le calage compilé (`bareme-<bid>` d'un
  answerbox, marqueur `ffz<bid>` d'une question libre), un identifiant neuf
  romprait le lien avec l'aperçu et le HTR jusqu'à la recompilation.
  ⚠ Elle réutilise aussi le **groupe** (`add_block(..., group=)`) et repart des
  **données stockées**, pas de `collectBlockData` : l'éditeur ne rend que ce
  qu'il affiche, si bien que le couple supprimer → annuler perdait `epilogue`
  (le `\end{multicols}` d'un sujet importé, donc un sujet qui ne compile plus)
  et rangeait le bloc restauré dans `COMMON_GROUP`, imprimé dans toutes les
  versions. Les deux ont été constatés sur un vrai projet.
  ⚠ **Si la lecture de l'état stocké échoue, on ne supprime pas** : la version
  précédente avalait l'échec, restaurait ensuite un bloc amputé et annonçait
  « restauré ✓ » — sujet qui ne compile plus. On ne supprime que ce qu'on saura
  remettre.
  ⚠ **`after_bid = None` veut dire « en fin » pour `add_block` et « en tête »
  pour `move_block`** — deux conventions opposées pour le même argument. La
  restauration passant par `add_block`, remettre le PREMIER bloc du sujet le
  renvoyait à la fin du document, en silence : d'où `at_start`.
  ⚠ **`restore=True` contourne `DISABLED_KINDS`** : sans lui, un
  `question_freeform` supprimé était définitivement perdu, alors que le filtre
  ne vise que la *création* — ce que CLAUDE.md promettait déjà.
  ⚠ L'annulation appelle `ensureSavedBeforeReload()` avant de recharger, comme
  `importFromBank` et l'application d'une édition IA, qui ne le faisaient pas
  non plus : `reloadPage` lève le garde-fou `beforeunload`, les blocs modifiés
  non enregistrés partaient donc en silence.

- **Les numéros de question sont recalculés côté client** (`renumberBlocks`,
  appelé par `buildOutline`, qui tourne après tout changement de structure).
  Ils venaient du seul rendu serveur : après un déplacement, la pastille et
  surtout la confirmation de suppression nommaient une question qui n'était
  plus à ce rang — or `describeBlock` existe précisément pour nommer ce qu'on
  va perdre.

### Jetons de design (`:root` en tête de style.css)

Une trentaine de variables (texte, surfaces, bordures, accent, sémantique).
**Migration par zone** : l'éditeur de sujet est migré, le tableau de bord et
les pages de correction ne le sont pas encore (≈980 couleurs en dur au total,
265 distinctes).

⚠ **Ne pas migrer le reste en masse par recherche du code hexa** : la même
valeur sert à des rôles différents (`#fff` est tantôt un fond de carte, tantôt
du texte sur la barre sombre). Les coupler sous un même jeton créerait une
dépendance fausse — pire que pas de jeton. La migration de l'éditeur a été
vérifiée **pixel à pixel** (capture pleine page avant/après : 0 pixel de
différence sur 3 445 880).

### Sélection : la clé est le `bid`, pas le numéro AMC

Le cadre magenta se pose sur **n'importe quel bloc**, y compris ceux qui n'ont
aucune région dans le PDF : bloc ajouté depuis la dernière compilation, texte
que la recherche n'a pas su localiser. C'est pour ça que la clé de sélection
est le **bid** (tout bloc en a un) et non le numéro AMC (réservé aux questions
compilées). Un bloc sans région est sélectionnable, l'aperçu indique alors
« pas dans le PDF compilé ».

`GET /sujet/regions.json` expose donc pour chaque région un champ **`bid`** en
plus de la clé interne `q`, et les `.pv-zone` sont indexées par bid.

⚠ **`server.block_preview_keys(blocks)` est l'unique implémentation** du
mapping bloc → clé de région, partagée par la page (`data-preview-q`) et par
`regions.json` (champ `bid`). Si les deux divergeaient, un cadre de l'aperçu
pointerait sur le mauvais bloc. Elle s'appuie sur `layout.question_names` et
**pas** sur `amc_question_map()`, dont la classification range la grille de
barème d'un `answerbox` (étiquetée 0,1,2…) parmi les colonnes du code étudiant.

### Sélecteur d'exemplaire et lettres de case (`case D`)

Le badge `case X` d'une réponse fait correspondre son **rang dans l'ordre de
déclaration LaTeX** à la lettre imprimée sur la feuille, lue dans le calage
compilé. Avec `shuffle_answers`, cette lettre **change d'un exemplaire à
l'autre** — mesuré sur un sujet à 3 exemplaires, une même réponse porte C, D
puis B. Le sélecteur en haut de page choisit l'exemplaire affiché
(`GET /api/sujet/charmap?copy=N`) ; il est **masqué quand il n'y a qu'un seul
exemplaire**, et n'a aucun effet sur la correction — celle-ci lit le numéro
d'exemplaire imprimé sur chaque copie scannée (`cv_grade.decode_page_code`) et
note avec la carte correspondante.

⚠ `charmap_for_copy()` est indexé par **numéro de QCM** (la clé de
`parse_tex()`), pas par numéro AMC : `_tex_chars` l'est, lui, et compte aussi
les colonnes du code étudiant et les barèmes (piège #1).

**Lettres périmées** : `sujet_store.letters_stale()` compare le mtime de
`subject.json` à celui d'`exam.xy`. Sujet édité depuis la compilation →
`body.letters-stale`, les lettres sont barrées et le bandeau affiche
« ⚠ lettres périmées — recompilez ». Nécessaire parce que **réordonner** des
réponses laisse des lettres silencieusement fausses (elles suivent la position,
pas la réponse) ; en ajouter ou en retirer est sans risque, `_attach_chars`
exige que le nombre corresponde et retire la lettre sinon.

### Panel outline gauche (📑 Structure)

3 colonnes : `sujet-outline` (sticky 230px) | `sujet-left` (édition) | `sujet-right` (aperçu PDF 420px).

- Item permanent en tête : `⚙ Réglages globaux` → scroll vers le bandeau.
- Chaque bloc dont `_sectionTitle` ≠ null (= `\section[*]?{X}` détectée OU
  `data-title` posé server-side) devient un **nœud `<details>` pliable**.
  Les blocs qui suivent une section deviennent ses enfants jusqu'à la suivante.
- **Caret `▸`/`▾` cliquable séparé du label** : clic caret = toggle pliage
  uniquement (no scroll), clic label = scroll uniquement (preventDefault sur
  toggle). UX claire.
- **Section virtuelle « Questions » auto** quand aucune section explicite +
  ≥3 blocs (cas EXAM_2026 sans `\section` migré, ou sujet simple).
- **IntersectionObserver** : suit le bloc le plus visible → highlight l'item
  actif (`outline-active`) et ouvre la section parent si pliée.
- **Drag&drop dans l'outline** (canonique seul) : chaque item est `draggable`,
  drop sur un autre item appelle `/api/sujet/blocks/move` + reorder DOM dans
  l'outline ET dans `#blocks-list`. Highlight `outline-drop-above/below`.
### Menu contextuel de bloc — partagé Structure ↔ édition

Un seul menu (`#blockContextMenu`, classe `.block-ctx-menu`) sert les **deux**
colonnes : clic droit sur un item de la Structure ou sur un bloc de l'éditeur
ouvre les mêmes 9 actions (7 insertions, `save-to-bank`, `delete`), qui
s'appliquent au bloc visé. Le handler `onContextMenu` est branché sur
`.sujet-outline` et sur `#blocks-list`.

- **Le clic droit sélectionne le bloc visé** (cadre magenta) : sans ça, rien
  n'indique sur quel bloc le menu va agir.
- **Dans un champ de saisie, on ne l'intercepte pas** (`input, textarea,
  select`) : c'est là qu'on attend couper/coller et le correcteur
  orthographique du navigateur.
- ⚠ Les insertions passent par `ensureSavedBeforeReload()` **avant** le
  `reloadPage()`. Sans elle, les blocs modifiés non enregistrés étaient perdus
  **en silence** : `reloadPage()` pose `_allowUnload = true`, ce qui neutralise
  le garde-fou `beforeunload`. (Constaté et corrigé.)
- ⚠ `openBankSaveModal` vit dans l'IIFE « Banque » ; le menu contextuel est
  dans une autre. Le pont est la variable `AMCxBankSave`, déclarée au niveau du
  script et affectée par l'IIFE Banque. Un appel direct levait un
  `ReferenceError` et l'action « 💾 Sauver dans la banque » ne faisait rien.

- **Renommage inline** (canonique seul) : double-clic sur le label → `<input>`
  pré-rempli. Enter/blur valide, Escape annule. Question QCM → modifie `tag`
  via `/api/sujet/blocks/update`. Section → modifie `\section*{X}` dans le tex
  via regex + `/api/sujet/blocks/update`.
- **Rebuild** auto après drag/add/delete/edit de titre (debounced 250 ms).
- **État préservé** : sections ouvertes/fermées restent dans cet état après rebuild.

### Routes API CRUD (10 nouvelles, mode canonique seul sauf indication)

```
GET  /api/sujet                   → {config, header, answer_sheet, blocks, mode,
                                       available_copies, total_max, max}
POST /api/sujet/config            → patch (num_copies, random_seed, shuffle_*) —
                                     OK en legacy via regex sur \exemplaire et \AMCrandomseed
POST /api/sujet/header            → patch HeaderBlock (refus legacy : 409)
POST /api/sujet/answer-sheet      → patch AnswerSheetConfig (refus legacy : 409)
POST /api/sujet/regenerate-seed   → {ok, seed} — OK en legacy
POST /api/sujet/blocks/add        → {kind, after_bid?, data?} → {bid}
POST /api/sujet/blocks/delete     → {bid}
POST /api/sujet/blocks/move       → {bid, after_bid|null}
POST /api/sujet/blocks/update     → {bid, data} (OK legacy pour question_qcm
                                     → délégué à save_questions)
POST /api/sujet/blocks/duplicate  → {bid} → {bid}
POST /api/sujet/migrate-to-canonical  → {ok, log, n_blocks, backup}
```

Helper `_crud_error(e)` mappe `PermissionError→409`, `KeyError→404`, `ValueError→400`.

### Compilation et préservation pixel-perfect

`compile_pdf()` lance `pdflatex` (2 passes, dossier temporaire,
`exam-config.tex` → mode calibration) → remplace `sujet/DOC-sujet.pdf` **et**
`sujet/exam.xy`. Touche jamais `raw_responses/`.

Après migration legacy → canonique, **le PDF compilé est byte-identique** au
legacy parce que :
- `cfg.preamble_tex` préserve le préambule original verbatim.
- `cfg.answer_sheet_tex` préserve la feuille de réponses verbatim.
- `cfg.header.raw_tex` préserve l'en-tête original verbatim (texte avant la
  1ère section/question).
- Chaque `\begin{question*}…\end{...}` est reproduit byte-pour-byte.
- Les marqueurs `%%QCM-…` sont des commentaires LaTeX (ignorés à la compilation).
- Une ligne vide est insérée entre les blocs (`parts.append("")` dans
  `render_subject`) pour préserver les paragraphes LaTeX que les marqueurs
  pourraient avaler.

### Publier le sujet et son corrigé (menu « 📤 Publier »)

Ce qu'on donne aux étudiants **après** l'examen : le questionnaire vierge et le
même document avec les bonnes réponses noircies.
`sujet_store.compile_publication(kind)` → `sujet/DOC-publication-<kind>.pdf`,
route `POST /api/sujet/publication`, servis par `GET /sujet/publication/<kind>.pdf`.

⚠ **Ce ne sont pas des documents à imprimer** : ni mires, ni code de copie, ni
feuille de réponses, donc aucun calage ne les décrit. `DOC-sujet.pdf` reste le
seul document dont les copies scannées peuvent venir. La compilation n'écrit ni
`exam.tex`, ni `exam.xy`, ni `DOC-sujet.pdf`, et n'invalide aucun cache de
géométrie — publier ne peut pas déplacer une note.

- **Le crochet AMC est `\def\CorrigeExterne{1}`** dans `exam-config.tex` (comme
  `\SujetExterne` l'est pour le calage, cf. `automultiplechoice.sty` ligne
  2672). Il allume d'un coup : **une seule copie par `\exemplaire`** — donc une
  par version, sans quoi on publierait les 44 copies —, pas de mires, pas de
  code imprimé, pas de filigrane.
- ⚠ **Le sujet vierge sort du MÊME crochet**, réponses éteintes dans le
  préambule (`\AMC@correcfalse`, `\def\AMC@intituleHead{}`, injectés juste
  avant `\begin{document}`). Il n'existe pas d'option AMC « comme le corrigé,
  mais vierge » : `modele` s'en approche mais garde le bandeau « Correction ».
  Et le fichier de configuration ne peut pas servir — il est lu **avant** que
  ces drapeaux n'existent (`\InputIfFileExists` ligne 42, `\newif` ligne 65).
  C'est ce qui rend les deux PDF **superposables page pour page** : ils
  diffèrent de trois lignes de préambule, rien d'autre.
- ⚠ **La source est `sujet/exam.tex`, pas le store.** Le document publié décrit
  l'examen qui a eu lieu, donc la dernière compilation ; des blocs édités depuis
  ne sont sur la copie de personne.
- ⚠ **On recompile à chaque demande** au lieu de servir le dernier fichier : un
  corrigé périmé part chez les étudiants sans que rien ne le signale, et trois
  secondes de `pdflatex` coûtent moins cher que ça.
- **La feuille de réponses est retirée** (`publication_tex`, entre les marqueurs
  `%%QCM-ANSWER-SHEET…`), avec le `\newpage` qui l'introduit — sinon le document
  se termine par une page blanche. ⚠ `%%QCM-ANSWER-SHEET` est un **préfixe** de
  `%%QCM-ANSWER-SHEET-END` : sans le `(?!-END)`, la borne gauche s'accroche à la
  fermeture du bloc précédent et tout le second `\exemplaire` — en-tête et
  questions compris — disparaît du document publié. Fixé par
  `tests/test_publication.py`.
- ⚠ **Un sujet legacy garde sa feuille de réponses** : sans marqueurs, il n'y a
  pas de quoi l'isoler, et couper au jugé publierait un sujet tronqué. Le
  bandeau de statut le dit.
- ⚠ **Avec `shuffle_answers`, l'ordre publié n'est celui d'aucune copie en
  particulier** (AMC repart de la copie 1 pour chaque `\exemplaire`). C'est
  annoncé dans le statut : un étudiant qui compare le corrigé à sa feuille
  verrait sinon des lettres qui ne correspondent pas et conclurait qu'il est
  faux.
- ⚠ **Le panneau du menu est mesuré puis ramené dans la fenêtre**
  (`openPubMenu`) : il est trois fois plus large que son bouton et la toolbar se
  replie en fenêtre étroite — ancré à droite, il sortait de 61 px à gauche de
  l'écran (mesuré).

### Découpage legacy intelligent (visualisation seule)

`_parse_legacy_subject(tex)` expose le sujet legacy comme s'il était canonique
(blocs ordonnés text + qcm) pour que l'utilisateur **voit** la structure de son
sujet avant de migrer :

- Coupe le body de `\exemplaire{N}{…}` aux frontières `\begin{question*}` ET
  `\section[*]?{X}` / `\subsection[*]?{X}` / `\chapter[*]?{X}`.
- **Chaque section est split en 2 blocs** : un bloc « titre seul » avec juste
  `\section{X}` puis un bloc « contenu » avec ce qui suit jusqu'à la frontière
  suivante. Plus lisible.
- L'intro (avant la 1ère frontière) va dans `cfg.header.raw_tex` (affichée dans
  le bandeau, pas comme bloc) — évite la duplication.
- Tous les blocs text legacy ont `data.readonly = True` et un titre lisible
  (`data.title` = titre de section ou aperçu des premiers chars).
- `parse_tex()` (compat) continue de retourner uniquement les `question_qcm`
  indexés par ordre (1, 2, … N). `score.py` marche
  inchangés.

### `new_project.py` template canonique

`_build_template_tex()` construit le sujet vierge via `render_subject({…})`
avec 4 blocs d'exemple (1 text `\section*{Questions}` + 1 QCM mult + 1 QCM single +
1 question_open). Garantit la cohérence parser/serializer pour tout nouveau projet.

### ⚠ Numérotation des `answerbox` — ne pas revenir à `\ref`

L'en-tête inline d'un bloc `answerbox` affiche « Question N » pour que le
correcteur relie le cadre de réponse à sa ligne de barème sur la feuille de
réponses. Ce N est **calculé par `render_subject`** (dict `qnums`), pas
récupéré par `\ref` sur un `\label` posé dans le groupe barème — la version
`\ref` était fausse : la grille « N° copie » qu'injecte `render_answer_sheet`
dès `num_copies > 1` fait avancer le compteur LaTeX, décalant le renvoi de
`num_copies − 1` (mesuré : « Question 5 » inline contre « Question 3 » sur la
feuille, pour 3 exemplaires).

⚠ Le numéro n'est **pas** l'ordre du document. AMC numérote dans l'ordre où la
**feuille de réponses** assemble les questions : `\formulaire` (groupe
« questions » = les QCM) → `\insertgroup{open}` → `\insertgroup{bareme}`
(les answerbox). D'où `qnum = (nombre de question_qcm) + (rang parmi les
answerbox)`. Vérifié sur 5 configurations, dont *answerbox placé en tête du
sujet* (AMC imprime quand même « Question 3 » avec 2 QCM) et *2 answerbox*
(3 puis 4). Les `question_open` / `question_freeform` ne produisent aucune
entrée numérotée sur la feuille et ne décalent donc rien.

Si `shuffle_questions` est actif, l'ordre change d'un exemplaire à l'autre :
aucun numéro fixe ne peut être juste → l'en-tête n'affiche que le titre.
Le parseur (`_parse_block_body`) accepte les deux formes, littérale et `\ref`,
pour relire un tex produit par une version antérieure.

### Convention de barème — piège UX

Les inputs `.ans-bareme` affichent la **valeur signée** (positive pour les
bonnes, **négative pour les mauvaises** = pénalité). Conséquence : si on édite
une mauvaise réponse en mettant `1/2` (sans le `-`), le tex devient
`\bareme{b=0,m=1/2}` → **+0.5 pt pour avoir coché une mauvaise réponse**
(bonification). Cette convention vient du legacy d'origine et est préservée.

## Banque de questions (MVP)

Une banque permet de réutiliser une question entre projets sans copier-coller
le `.tex`. Modules : [auto_grading/bank.py](auto_grading/bank.py) (local) et
[auto_grading/bank_online.py](auto_grading/bank_online.py) (Supabase).

### Multi-banques (V2) — sélection + ajout depuis l'UI

`config.banks` est un dict `{slug: entry}` (V2) — plusieurs banques peuvent
coexister (perso, ENSAI, communautaire, …) et l'user switche entre elles
dans la **topbar de la page Banque** (dropdown à côté du titre). Chaque
entry porte son propre type/credentials :

```jsonc
{
  "active_bank": "perso-local",
  "banks": {
    "perso-local":        {"name": "…", "type": "local",  "path": "~/Documents/AMCx-banque/"},
    "hypothesis-testing": {"name": "…", "type": "local",  "path": "~/Documents/AMCx-banques/hypothesis-testing/"},
    "ensai-public":       {"name": "…", "type": "online", "supabase_url": "…", "supabase_anon_key": "…",
                           "user_token": "…", "refresh_token": "…", "user_id": "…", "user_email": "…",
                           "token_expires_at": …}
  }
}
```

**Migration auto** : au 1er `load_config()` avec une config V1 (clés flat
`bank_mode`, `bank_supabase_url`, …), `_migrate_banks()` crée `banks["default"]`
depuis ces valeurs et pose `active_bank="default"`. Les clés flat sont
conservées en DEFAULTS pour permettre un rollback. Les nouvelles écritures
vont dans `banks[active]` via `config.update_active_bank(updates)`.

**Helpers `config.py`** : `active_bank_cfg()` → dict de la banque active,
`active_bank_slug()` → slug, `update_active_bank(updates)` → patch dans
`banks[active]` (sans toucher aux autres banques).

**Routes serveur** (CRUD sur les banques elles-mêmes, distinctes des routes
`/api/bank*` qui agissent sur les questions de la banque active) :
- `GET    /api/banks`                  → `{active, banks: [{slug, name, type, path? | supabase_url?, logged_in?, user_email?}]}`
- `POST   /api/banks`                  → `{name, type:'local'|'online', path? | supabase_url?, supabase_anon_key?}` → crée + génère un slug unique
- `DELETE /api/banks/<slug>`           → supprime. Si c'était l'active, repointe vers une autre (ou recrée default vide)
- `POST   /api/banks/<slug>/activate`  → switch (le serveur reste up, `_bank()` lit la nouvelle au prochain request)

`_bank()` (dispatcher dans server.py) lit `config.active_bank_cfg()["type"]`
et retourne `bank` ou `bank_online`. Toutes les routes `/api/bank*` (questions)
restent inchangées.

**UI** (`templates/banque.html`) : topbar = `[📚 Banque]  [💾 Nom de la banque ▾]
[+ Ajouter] [🔐 Connexion] [📊 Sync] [👁 Aperçu]`. Le dropdown banque affiche
toutes les banques avec icône (💾/🌐), pastille « active » sur la courante,
🗑 par ligne (suppression). Click sur une ligne autre = `POST activate` +
`window.location.reload()`. Bouton « + Ajouter » → modale (nom + type
local/online + path ou URL+anon).

**Hors-scope V2** : multi-bank read (interroger plusieurs banques en parallèle),
cross-bank stats, browse natif de dossier (le champ « Chemin » est un text
input — utiliser `~` ou chemin absolu), renommage, PATCH des credentials
d'une banque existante (workaround : delete + recréer).

### Schéma de stockage (local)

**Stockage local** : `~/Documents/AMCx-banque/` par défaut (override : champ
`path` de la banque active, ou env `AMCX_BANK_DIR` en fallback final).
1 fichier JSON par question sous `questions/<bank_id>-<slug>.json` +
`index.json` (cache, reconstruit auto si désynchronisé).

**Schéma** d'une question : `{bank_id, kind, data, title, tags, author,
created_at, modified_at, version, source_project}`. `data` = `data` d'un
Block AMCx (sans `bid` ni `_bank_id`). `bank_id` = UUID hex[:8] stable.

**Routes** :
- `GET  /api/bank?q=&kind=&tags=` → liste + tags disponibles.
- `GET  /api/bank/<bank_id>` → question complète.
- `POST /api/bank` `{bid, title, tags, author?}` → exporte un bloc du sujet
  courant dans la banque.
- `DELETE /api/bank/<bank_id>` → suppression définitive (n'affecte pas les
  sujets où la question a déjà été importée).
- `POST /api/bank/<bank_id>/import` → insère la question en fin du sujet
  courant. Bid frais, `data._bank_id` rempli (trace d'origine).

**UI** (onglet Sujet, canonique seul) :
- Bouton `📚 Banque` dans la toolbar → modale plein écran (filtres tags/kind/
  recherche + liste de cartes + preview à droite).
- Clic droit dans l'outline → `💾 Sauver dans la banque` (modale rapide
  titre + tags + auteur).

**Statistiques par question** (taux de réussite, depuis les copies du
projet actif) :
- Schéma : `stats: {by_project: {<projet>: {n_eval, sum_normalized, n_perfect,
  max_score_at_sync, last_sync}}}`. `n_eval` = nb copies, `sum_normalized` =
  Σ(score/max) par copie ∈ [-∞, n_eval], `n_perfect` = nb avec score == max.
- Bouton **`📚 Mettre à jour la banque`** dans la barre de réglages du
  dashboard → `POST /api/bank/sync` → scan toutes les copies du projet
  courant, pour chaque bloc QCM avec `data._bank_id` recalcule les stats
  et remplace l'entrée `stats.by_project[<projet>]` (idempotent).
- Skip `question_open` / `answerbox` (pas de note auto).
- Affichage : pastille `📊 N éval · X%` sur chaque carte du modale, table
  détaillée par projet dans le panneau preview.
- Mapping bloc → numéro de question : position du bloc parmi les QCM en
  ordre document (= clé `answers[q]` dans `raw_responses/`), pas le tag
  (qui peut être dupliqué si on importe une question dans son propre
  projet d'origine).

**Hors-scope MVP** : page `/banque` dédiée, icône 🔗 sur les blocs liés,
bouton « 🔄 Mettre à jour la question dans la banque » (édition d'une question
existante), synchro git, détection de dépendances LaTeX.

### Catégories hiérarchiques — étapes 0 et 1 livrées (backend local)

Spec complète : [auto_grading/BANK_CATEGORIES_PLAN.md](auto_grading/BANK_CATEGORIES_PLAN.md).
Livré : le module pur, le backend **local**, les routes, les tests. **Pas
encore** : le backend online (étape 2, `schema.sql` + `bank_online.py`) ni
aucune UI (étapes 3-4) — les routes répondent **501** sur une banque online.

⚠ **L'identité d'une catégorie est son `id` (UUID), jamais son nom ni son
chemin.** C'est ce qui distingue une catégorie d'un tag : renommer ou déplacer
un nœud ne touche à aucune affectation. Un tag, lui, *est* sa chaîne — le
renommer casserait le lien sur toutes les questions à la fois.

- [bank_taxonomy.py](auto_grading/bank_taxonomy.py) — **logique pure, zéro
  I/O**, partagée par les deux backends : liste plate `{id, parent_id, name,
  position}` (même forme que la table Postgres), `validate_nodes`,
  `descendants`, `would_create_cycle`, `subtree_height`, `annotate`,
  `sanitize_assignment`. `MAX_DEPTH = 6`.
- L'arbre local vit dans **`<bank>/categories.json`, à la racine** — surtout
  pas dans `questions/` : `_read_or_rebuild_index()` compare le nombre de
  fichiers `questions/*.json` au nombre d'entrées de l'index, donc un intrus
  dans ce dossier forcerait un rebuild complet à chaque lecture.
- Les affectations vivent **sur la question** (`categories: [uuid]`, comme
  `tags`) : un fichier de question reste autoportant. `index.json` les recopie
  et porte désormais un `index_version` (2) — un index d'une version
  antérieure se reconstruit tout seul.
- **Tags conservés, orthogonaux** : l'arbre porte la structure du cours, les
  tags restent les facettes transversales (`facile`, `L2`). Aucune conversion
  automatique — elle remplirait l'arbre d'étiquettes de difficulté. Promotion
  **opt-in** d'un tag en catégorie : `POST /api/bank/categories/<id>/assign
  {tag}`.
- **Filtre par sous-arbre** : `?category=<uuid>` inclut les descendants par
  défaut (`&descendants=0` pour ne prendre que le nœud), `?uncategorized=1`
  isole les questions sans catégorie **vivante** — un id mort (nœud supprimé
  ailleurs) ne doit pas rendre une question introuvable des deux côtés du
  filtre.
- **Suppression** : 409 si le nœud n'est pas vide ; `?mode=reparent` remonte
  enfants et questions au parent. **Aucune question n'est jamais supprimée.**
- Classer une question **ne touche ni `modified_at` ni `version`** : la liste
  est triée par date de modification, ranger une vieille question ne doit pas
  la faire remonter en tête. C'est aussi pourquoi les affectations ne passent
  pas par `/api/bank/<id>/save-data`.
- `bank.save(question, reindex=False)` pour les lots : `rebuild_index()`
  scanne tout le dossier, donc 40 affectations feraient 40 scans complets.
  L'appelant reconstruit une fois à la fin.

Routes (`_cat_error` mappe TaxonomyConflict→**409**, TaxonomyError→400,
KeyError→404, NotImplementedError→501) :

```
GET    /api/bank/categories              → {nodes[{id,parent_id,name,position,depth,path,n_direct,n_total}], max_depth, can_edit}
POST   /api/bank/categories              → {name, parent_id?, position?}
PATCH  /api/bank/categories/<id>         → {name?, parent_id?, position?}
DELETE /api/bank/categories/<id>?mode=refuse|reparent
POST   /api/bank/categories/<id>/assign  → {bank_ids:[…]} | {tag}, remove?
GET|PUT /api/bank/<bank_id>/categories   → {categories:[uuid]}
GET    /api/bank/facets                  → {all_tags, nodes, max_depth, can_edit}
```

⚠ Sur `PATCH`, **`parent_id` absent = ne pas toucher au parent**, `parent_id:
null` = remonter à la racine. D'où la sentinelle `bank.UNSET` plutôt qu'un
`.get()`.

⚠ `n_total` est un **ensemble, pas une somme** : une question classée dans
deux sous-catégories d'un même chapitre n'y compte qu'une fois.

### Deux bugs corrigés au passage

- **`AMCX_BANK_DIR` était sans effet.** `bank_root()` documentait la
  précédence « config > env > défaut », mais `config.active_bank_cfg()`
  synthétise un repli qui porte **déjà** un `path` quand aucune banque n'est
  configurée — la branche env n'était donc jamais atteinte. Corrigé : le
  chemin de la config ne l'emporte que si une banque est *explicitement*
  configurée. C'est ce qui rend les tests isolables.
- **`GET /api/bank` parcourait la banque deux fois** à chaque frappe (une fois
  filtrée, une fois entière juste pour `all_tags`). Quand aucun filtre n'est
  actif, la liste filtrée *est* la liste complète : le second parcours est
  supprimé. Le cas filtré disparaîtra quand l'UI lira `/api/bank/facets`.

### Tests

Premier `tests/` du dépôt — **`unittest` de la stdlib**, pas de pytest (aucune
dépendance ajoutée) :

```bash
.venv/bin/python -m unittest discover -s tests -v     # 596 tests
./tests/sql/run.sh                                    # + 24 contrôles SQL (docker)
```

[tests/fake_postgrest.py](tests/fake_postgrest.py) simule le sous-ensemble de
PostgREST utilisé : le backend en ligne est donc testable sans instance
Supabase — mais **ni RLS ni trigger**, d'où le harnais SQL.

⚠ Chaque `setUp` **repointe `AMCX_BANK_DIR` sur sa propre banque jetable**.
`unittest discover` importe *tous* les modules de test avant d'en exécuter un
seul : un module qui pose la variable au niveau module la pose pour tout le
monde, et le dernier importé gagne. (Constaté : 45 échecs selon l'ordre.)

### Catégories hiérarchiques (livré : backends, UI, migration, glisser-déposer)

Spec complète : [auto_grading/BANK_CATEGORIES_PLAN.md](auto_grading/BANK_CATEGORIES_PLAN.md).
Les tags restent des **facettes transversales** (`facile`, `L2`) ; l'arbre porte
la **structure du cours** (chapitre → sous-chapitre). Les deux coexistent, aucune
conversion automatique.

⚠ **L'identité d'une catégorie est son `id` (UUID), jamais son nom ni son
chemin.** Renommer ou déplacer un nœud ne touche donc à aucune affectation. Un
tag, lui, *est* sa chaîne : le renommer casserait le lien sur toutes les
questions à la fois — c'est précisément ce que l'arbre corrige.

- [bank_taxonomy.py](auto_grading/bank_taxonomy.py) — **logique pure, zéro I/O**,
  partagée par les deux backends : validation, cycles, profondeur
  (**`MAX_DEPTH = 6`**, portée de 4 pour qu'une banque puisse rassembler
  plusieurs cours — `cours › chapitre › sous-chapitre` fait déjà 3 niveaux
  pour un seul, et la banque de régression en occupe 3 sur 37 nœuds),
  `descendants`, `annotate` (aplatit en ordre préfixe avec `depth`/`path`/
  `n_direct`/`n_total`). L'UI ne refait aucun calcul d'arbre.
- L'arbre local vit dans **`<bank>/categories.json`, à la racine** — surtout pas
  dans `questions/` : `_read_or_rebuild_index()` compare le nombre de fichiers
  `questions/*.json` au nombre d'entrées de l'index, donc un intrus dans ce
  dossier forcerait un rebuild complet à chaque lecture. Fichier absent = arbre
  vide, **sans écriture** (une banque sur un partage en lecture seule reste
  consultable) ; fichier corrompu → mis de côté en `.corrupt-<horodatage>`.
- Les affectations vivent **sur la question** (`categories: [uuid]`, comme
  `tags`) : un fichier de question reste autoportant.
- `n_total` est un **ensemble, pas une somme** : une question classée dans deux
  sous-catégories d'un même chapitre n'y compte qu'une fois.
- Un id de catégorie **mort** (nœud supprimé ailleurs) est **ignoré en lecture**
  plutôt que de lever — sinon une question deviendrait illisible à cause d'un
  nœud effacé dans un autre projet. Le filtre « sans catégorie » les ignore aussi.
- `set_question_categories` ne touche **ni `modified_at` ni `version`** : classer
  n'est pas éditer. Sinon ranger une vieille question la ferait remonter en tête
  de la liste, triée par date de modification. Même raison pour ne pas router les
  affectations par `/api/bank/<id>/save-data`.
- **Suppression** : 409 par défaut si le nœud n'est pas vide ; `?mode=reparent`
  remonte enfants et questions au parent. **Aucune question n'est jamais
  supprimée.** Un nœud racine supprimé en `reparent` laisse ses questions sans
  catégorie (retrouvables par `uncategorized=1`).
- Un déplacement vérifie la profondeur du **sous-arbre entier**
  (`subtree_height`) : contrôler la seule profondeur du nœud déplacé laisserait
  passer un déplacement qui enfonce ses descendants.
- `save(question, reindex=False)` pour les lots : `rebuild_index()` scanne tout
  le dossier, donc affecter 40 questions faisait 40 scans complets. L'appelant
  reconstruit une fois à la fin.

Routes (les deux backends ; un backend sans l'API répond **501**, pas un
`AttributeError` opaque) :

| Route | Rôle |
|---|---|
| `GET /api/bank/categories` | `{nodes, max_depth, can_edit}` |
| `POST /api/bank/categories` | `{name, parent_id?, position?}` |
| `PATCH /api/bank/categories/<id>` | `{name?, parent_id?, position?}` — `parent_id` **absent** = ne pas toucher, `null` = remonter à la racine |
| `DELETE /api/bank/categories/<id>?mode=refuse\|reparent` | 409 si non vide en `refuse` |
| `GET\|PUT /api/bank/<bank_id>/categories` | affectations d'une question |
| `POST /api/bank/categories/<id>/assign` | `{bank_ids}` ou `{tag}` (promotion **opt-in** d'un tag), `{remove}` |
| `GET /api/bank/facets` | `{all_tags, nodes}` en un seul aller-retour |
| `GET /api/bank?category=&descendants=0\|1&uncategorized=1` | filtre par sous-arbre |

`_cat_error()` mappe : conflit d'invariant (cycle, profondeur, doublon entre
frères, nœud non vide) → **409** ; id malformé → 400 ; id inconnu → 404.
Un id de catégorie est **toujours validé par une regex UUID stricte** avant tout
usage — `bank.is_valid_bank_id` accepte 36 caractères quelconques de
`[0-9a-fA-F-]`, ce qui suffit contre un glob `*` mais pas pour interpoler une
valeur dans une URL PostgREST (backend en ligne, étape 2).

⚠ **`AMCX_BANK_DIR` était sans effet** : `active_bank_cfg()` synthétise un repli
qui porte déjà un `path`, si bien que la branche env de `bank_root()` n'était
jamais atteinte. Corrigé — l'env ne s'applique que si **aucune** banque n'est
configurée, ce qui rend les tests isolables sans toucher aux banques réelles.

#### Backend en ligne — section 8 de [supabase/schema.sql](supabase/schema.sql)

Deux tables : `bank_categories` (l'arbre) et `question_categories` (la
jonction). `bank_online.py` expose exactement la même API que `bank.py` — les
routes ignorent sur quel backend elles tournent.

- **Le trigger `bank_categories_check_tree` est la garantie**, pas le client :
  il refuse cycle, auto-parent et profondeur > 6 même si quelqu'un tape la base
  directement. Le client refait les mêmes contrôles uniquement pour rendre un
  message lisible au lieu d'une erreur SQL. ⚠ La constante 6 y double
  `bank_taxonomy.MAX_DEPTH` : les deux doivent bouger ensemble.
- **Unicité entre frères, racine incluse** : `NULL` n'entrant dans aucune
  contrainte d'unicité, l'index passe par `coalesce(parent_id, '000…0'::uuid)`
  — sans ça, deux chapitres homonymes à la racine passeraient.
- `parent_id` en **`on delete restrict`** (supprimer un nœud qui a des enfants
  échoue côté base) ; la jonction en **`cascade`** des deux côtés : supprimer
  une catégorie n'efface jamais une question.
- Les affectations sont **embarquées dans le `select`** de `list_questions`
  (`question_categories(category_id)`) : aucune requête supplémentaire.
- ⚠ **`categories` n'est pas une colonne de `bank_questions`** : elle est dans
  le `skip` de `_question_to_row`. L'y laisser ferait échouer tout `save()` en
  PGRST204.

⚠ **Un refus RLS sur DELETE/UPDATE ne lève pas d'erreur : il filtre les
lignes** (vérifié sur Postgres 16). Deux conséquences, toutes deux traitées :
retirer un classement posé par un tiers est un **no-op silencieux**, donc
`set_question_categories` se comporte en *fusion* et non en remplacement dès
qu'une autre personne a classé la question ; et `delete_category(mode=
"reparent")` **perd** les affectations d'autrui, que le `cascade` efface sans
qu'on ait pu les recréer sur le parent. D'où : ces deux fonctions **relisent
l'état réel** et retournent ce qui s'est vraiment passé, jamais ce qui avait
été demandé. L'UI doit afficher ça, et le dire dans la confirmation de
suppression.

**Le harnais SQL** ([tests/sql/run.sh](tests/sql/run.sh), docker requis)
applique la seule section 8 sur un prélude minimal
([tests/sql/00_stub.sql](tests/sql/00_stub.sql) : `auth.uid()`, `profiles`,
`bank_questions`), vérifie l'idempotence du schéma, puis le trigger, l'index
d'unicité, les FK, le cascade et les policies **avec deux utilisateurs**. Rien
de tout ça n'est simulable côté Python.

#### UI — [static/bank_tree.js](auto_grading/front/static/bank_tree.js)

Composant unique `AMCxBankTree`, deux modes : `filter` (navigation + édition,
panneau gauche de `/banque`) et `pick` (cases à cocher, sélecteur de la fiche
question). Il sera réutilisé tel quel dans les modales de `/sujet` (étape 4) —
d'où le fichier partagé plutôt qu'une troisième copie de widget (cf. la dette
des widgets Phase B dupliqués entre `banque.html` et `sujet.html`).

⚠ **Aucun `innerHTML` dans ce fichier, nulle part.** Les noms de catégories
viennent d'une banque potentiellement partagée : c'est de l'entrée non fiable,
tout passe par `createElement` + `textContent`. Vérifié : une catégorie nommée
`<script>alert(1)</script>` s'affiche littéralement et n'injecte aucun nœud
`<script>`.

- Le composant **ne recalcule aucune structure d'arbre** : le serveur renvoie
  déjà `depth`, `path`, `n_direct`, `n_total`. Il ne fait que replier, indenter
  et émettre les filtres.
- **Caret et libellé sont deux zones de clic distinctes** : plier n'est pas
  filtrer. La boîte du caret est réservée même sans enfant, sinon les libellés
  se décalent d'une ligne à l'autre.
- **Repli persisté** dans `localStorage`, avec une clé **par banque**
  (`amcx-bank-tree-<slug>`) — sans ça, deux banques partageraient leur état de
  repli. Le slug n'étant connu qu'après `/api/bank/auth-status`, la clé est
  reposée après coup (`setStorageKey`).
- **Arbre vide** : ni « Sans catégorie », ni case de portée, ni « Toutes » — ils
  ne peuvent rien filtrer. Un message d'amorçage et le bouton de création.
- Les outils d'édition (`+ ▲ ▼ ✕`) n'apparaissent **qu'au survol** de la ligne :
  quatre boutons permanents par ligne noieraient l'arbre.
- La **suppression d'un nœud non vide** passe par un `confirm` qui dit ce que le
  nœud contient, que rien ne sera supprimé, et qu'en ligne les classements
  d'autrui seront perdus.
- ⚠ La fiche question affiche **l'état renvoyé par le serveur**, jamais celui
  demandé (cf. la fusion silencieuse en ligne ci-dessus).
- ⚠ `.banque-q-row` est un conteneur **flex** : le chemin de catégorie s'y pose
  en colonne de droite, pas sur une seconde ligne (`display:block` n'y change
  rien). Il est borné à 45 % pour ne pas écraser le titre.

Non fait, et pas dans le périmètre demandé : la facette « tags publics » de
`/banque` (elle n'existe que dans la modale de `/sujet`).

#### UI — modales de `/sujet`

Le **même** `AMCxBankTree` sert les deux modales, sans une ligne de widget
dupliquée :

- **« 📚 Banque »** : arbre en mode `filter`, `canEdit: false` — on ne remanie
  pas l'arbre d'une banque depuis un sujet, ça se fait dans l'onglet Banque. Il
  se combine en ET avec la recherche, le type et les tags. Les cartes affichent
  le chemin de catégorie, masqué quand on filtre déjà dessus.
- **« 💾 Sauver dans la banque »** : arbre en mode `pick`, **pré-coché sur les
  dernières catégories utilisées** (`localStorage`, clé par banque) — classer
  dix questions du même chapitre à la suite ne doit pas demander dix fois les
  mêmes clics. Le message de confirmation nomme les catégories retenues.
- Banque sans aucune catégorie : le sélecteur est masqué et remplacé par une
  phrase qui renvoie vers l'onglet Banque.

⚠ **`setPicked` filtre sur l'arbre courant.** Les ids pré-cochés viennent d'un
`localStorage` qui peut être plus vieux que l'arbre : garder l'id d'une
catégorie supprimée entre-temps ferait échouer l'enregistrement en 404. Vérifié
avec un id fantôme : la question part sans catégorie, sans erreur.

⚠ **Bug antérieur corrigé au passage — deux sens pour `.bank-modal`.** La règle
`.bank-modal { width: 480px }` visait la *carte* « Ajouter une banque » de
`/banque`, mais s'appliquait aussi à `<div class="pm-modal bank-modal">`,
l'*overlay plein écran* de la modale Banque de `/sujet` : son `inset: 0` était
écrasé, l'overlay tombait à 480 px et la carte de 1100 px débordait de **310 px
à gauche de l'écran** (mesuré avant/après). La règle est désormais limitée à
`.bank-modal-bg .bank-modal`.

#### Migration locale → en ligne

[bank_migrate.py](auto_grading/bank_migrate.py) monte l'arbre **avant** les
questions (étape 0/3), en **conservant les identifiants** : les ids locaux sont
déjà des UUID v4, donc ils se transposent tels quels et les affectations des
questions migrées pointent sur les bons nœuds — **aucune table de
correspondance n'est nécessaire pour les catégories**, contrairement aux
questions dont l'id local ne fait que 8 hex. L'ordre préfixe d'`annotate` place
chaque parent avant ses enfants, ce que la clé étrangère exige.

⚠ Un **conflit de nom entre sœurs** (l'arbre en ligne contient déjà un chapitre
du même nom, créé indépendamment) n'est **pas** fusionné : il est signalé, à
l'humain de trancher. Fusionner deux chapitres homonymes en silence mélangerait
deux cours.

#### Glisser-déposer et facettes

- Glisser une ligne de question sur un nœud de `/banque` l'**ajoute** à cette
  catégorie ; ça ne la retire d'aucune autre — c'est le modèle. Le retrait passe
  par le ✕ d'une chip sur la fiche. Les catégories courantes sont **relues avant
  écriture** : l'état affiché peut dater, et un `PUT` bâti dessus perdrait les
  autres appartenances.
- Le type MIME du transfert est `application/x-amcx-bank-id`, pas `text/plain` :
  pendant `dragover`, `getData` est interdit et seuls les **types** sont
  lisibles — c'est la seule façon de savoir si le survol nous concerne avant
  d'accepter le dépôt.
- ⚠ **`GET /api/bank` ne renvoie plus `all_tags`.** Le calculer imposait un
  **second parcours complet** de la banque à chaque frappe dans la recherche
  (deux requêtes HTTP entières en banque en ligne). Les facettes — tags publics
  *et* arbre — sont servies une fois par ouverture de modale par
  `GET /api/bank/facets`, et `AMCxBankTree.load(payload)` consomme cette même
  réponse au lieu d'aller rechercher l'arbre. Effet de bord voulu : la liste
  des tags ne se réduit plus au fil du filtrage, elle décrit la banque et non
  le résultat courant.

**Volontairement non fait** : la vue SQL `category_counts`. Les comptages sont
agrégés côté client à partir d'une seule requête sur `question_categories` —
avec des banques de quelques centaines de questions, une vue `security_invoker`
serait de la complexité sans gain mesurable. À revoir si une banque dépasse le
plafond de 500 lignes de `list_questions`, qui mordra bien avant.

⚠ **Après toute édition de template ou de statique, redémarrer le serveur** :
Jinja est en `auto_reload=False` (debug off) et met `banque.html` en cache dès
le premier rendu. Constaté en testant : un serveur lancé avant l'ajout de
l'arbre servait indéfiniment l'ancienne page, arbre absent et aucune erreur.

### Variantes d'une même question (backend local)

Le sujet du matin et celui de l'après-midi posent souvent **la même** question à
des valeurs près. Ce n'est ni un doublon (les deux doivent rester imprimables)
ni deux questions (la banque triplerait et on ne verrait plus le cours). D'où
les **variantes** : la liste n'affiche qu'un représentant par groupe, annoté
`🔀 N`, et l'on déplie le groupe pour choisir celle qu'on imprime.

Moteur : [bank_variants.py](auto_grading/bank_variants.py) — **logique pure,
zéro I/O**, partagée par les deux backends comme `bank_taxonomy.py`. Le modèle
tient dans un champ : `variant_of` vaut `""` si la question est **chef** de son
groupe (cas par défaut, une question seule comprise) et sinon le `bank_id` du
chef.

⚠ **Pas de chaîne.** Rattacher A à B alors que B est déjà une variante range A
sous le **chef** de B. Une arborescence obligerait chaque page à remonter les
parents pour répondre à « quelles sont les variantes de celle-ci ? », et deux
pages finiraient par en compter deux nombres différents.

⚠ **Rattacher une question qui a déjà des variantes FUSIONNE les deux groupes.**
Laisser ses variantes derrière elle créerait un second chef au même énoncé —
exactement le doublon que ce mécanisme existe pour éviter. `set_variant_of`
rend donc **l'état réel du groupe après écriture**, jamais ce qui a été
demandé, et l'UI affiche ce qu'elle a obtenu.

⚠ **Supprimer un chef ne supprime jamais ses variantes** : la plus ancienne est
promue, les autres la suivent (`promote_on_delete`). Même règle que pour les
catégories — aucune question n'est jamais supprimée implicitement. Sans ça,
supprimer une question en ferait disparaître plusieurs de la liste, toujours
sur le disque mais repliées sous un chef qui n'existe plus.

⚠ **Un pointeur mort ou un cycle est RÉPARÉ à la lecture, pas levé**
(`normalize`, appelé par tout listing). Ces fichiers se suppriment à la main,
se synchronisent par git, se restaurent depuis une sauvegarde : une banque ne
doit pas devenir illisible parce qu'un `variant_of` désigne un fichier absent.
`bank.repair_variants()` persiste la réparation pour ne pas la refaire à chaque
listing.

⚠ **Le filtre porte sur chaque question, le repli vient APRÈS**
(`expand_matches`). Une recherche qui ne touche qu'une variante ramène donc son
groupe entier : sinon la variante serait repliée sous un chef que le filtre n'a
pas retenu, et elle disparaîtrait de l'écran — introuvable alors qu'elle
correspond exactement à ce qu'on cherche.

⚠ **Rattacher ne touche ni `modified_at` ni `version`**, pour la même raison que
classer dans une catégorie : la liste est triée par date de modification, et
ranger de vieux QCM les ferait tous remonter en tête.

⚠ `variant_of` et `created_at` sont **dans `index.json`** (version 3) : replier
les variantes est fait à chaque listing, et relire 300 fichiers à chaque frappe
de la recherche annulerait l'index. L'ordre au sein d'un groupe est
`(created_at, bank_id)` — `_now()` est à la seconde, donc deux questions créées
dans le même lot ne se départagent que par leur identifiant : un import qui
tient à l'ordre doit poser `created_at` lui-même.

| Route | Rôle |
|---|---|
| `GET /api/bank/<id>/variants` | `{head, members:[…]}` — le groupe entier, chef d'abord |
| `POST /api/bank/<id>/variants` | `{head_id}` rattache · `{head_id: null}` détache |
| `GET /api/bank?variants=all` | liste à plat (défaut : repliée) |

**UI** : badge `🔀 N` sur la ligne de liste de `/banque`, encart « Variantes »
dans le panneau de détail (groupe cliquable, ✕ pour détacher, bouton
« Rattacher à une autre question… » qui met la liste en mode désignation —
bandeau + curseur, parce qu'une liste qui change de sens en silence se paye au
premier clic). Dans la modale « 📚 Banque » de `/sujet`, une rangée de pastilles
choisit **la variante à insérer** : sans elle on insérerait toujours le
représentant et les autres formulations seraient stockées pour rien.

⚠ **Non fait : le backend en ligne.** `bank_online` n'expose pas `list_variants`,
donc `_var_backend()` répond **501** (même contrat que `_cat_backend`). En
revanche `variant_of` est dans le `skip` de `_question_to_row` : `from_block` le
pose sur toute question locale, et sans ce filtre `bank_migrate` échouerait en
PGRST204 dès la première question.

#### ⚠ Le panneau gauche a DEUX ascenseurs, pas un

Il empile trois zones : filtres, arbre de catégories, liste des questions. Avec
un seul ascenseur pour les trois, un arbre réel — une trentaine de nœuds
dépliés, **1 114 px mesurés** — poussait **toute la liste sous la ligne de
flottaison** : on ouvrait `/banque` et on ne voyait aucune question, sans que
rien ne le signale. Le défaut n'existait pas tant que la banque était vide ;
il est apparu avec l'import des catégories.

Le panneau est donc un flex vertical de hauteur fixe qui ne scrolle plus
(`overflow: hidden`) ; l'arbre et la liste scrollent chacun chez soi.

⚠ **La borne de l'arbre est posée sur le `<details>` lui-même**, pas sur le
`div` intérieur : Chromium enveloppe le contenu d'un `details` dans une boîte
anonyme, si bien qu'un `display: flex` sur le `details` ne fait **pas**
rétrécir ses enfants. Première tentative : le `max-height` clampait le parent à
191 px pendant que l'arbre en gardait 957 et se **dessinait par-dessus** la
liste.

⚠ **L'arbre doit pouvoir rétrécir** (`flex: 0 1 auto; min-height: 0`), pas
seulement être plafonné. À hauteur fixe, filtres + arbre + liste dépassaient
les 420 px du panneau en 900×700 et le bas de la liste devenait inatteignable,
le panneau ne scrollant plus. Vérifié à 1500×1000, 1200×620 et 900×700 : la
liste tient dans le panneau dans les trois cas. Le `<summary>` est `sticky` —
c'est lui qui replie la section, il ne doit pas défiler hors de portée.

#### La recherche porte sur l'ÉNONCÉ, pas seulement sur le titre

`bank.search_text(kind, data)` assemble énoncé + réponses + tag + titre, replie
(minuscules, accents ôtés) et borne à `SEARCH_TEXT_MAX` (2 000 caractères) ;
`_build_index_entries` le range dans `index.json` sous la clé `text`
(`INDEX_VERSION = 4`).

⚠ **C'était le premier obstacle réel au passage à l'échelle.** Le filtre ne
regardait que `title` et `tags` : sur la banque d'un seul cours ça se rattrape
à l'œil, sur plusieurs non — on se souvient d'une formulation (« celle où T
vaut −4 »), pas d'un titre écrit une fois.

⚠ **Les accents sont repliés des DEUX côtés** (`_fold`) : « regression »
trouve « régression ». Le texte est replié une fois, à l'écriture de l'index ;
la requête l'est à chaque appel.

⚠ **Le texte est dans l'INDEX, pas relu dans les fichiers** : la recherche
tourne à chaque frappe, rouvrir 3 000 fichiers par touche annulerait l'index.
Coût mesuré : ~780 octets par question (63 Ko → 146 Ko sur 107 questions).

⚠ **D'où le cache de l'index parsé** (`_IDX_CACHE`, clé = chemin + mtime +
nombre de fichiers). Le contrôle de fraîcheur est inchangé — c'est lui qui
évite de servir un index périmé — seul le `json.loads` est sauté. Sans lui, une
banque à 3 000 questions re-parsait 4 Mo de JSON **à chaque touche**.
`rebuild_index()` vide le cache plutôt que de le renseigner : la clé porte le
mtime du fichier qu'on vient de réécrire.

⚠ **En ligne, `or=(title.ilike.…,data->>statement.ilike.…)`** : deux paramètres
PostgREST séparés se combineraient en **ET** et ne rendraient jamais rien. La
valeur est **entre guillemets** (`_ilike_value`) — une virgule ou une
parenthèse tapée dans la recherche couperait sinon la liste `or=` en deux
conditions. ⚠ Et là, **les accents ne sont pas ignorés** : `ilike` de Postgres
l'est pour la casse, pas pour les diacritiques (il faudrait `unaccent`).

**Mesuré sur une banque synthétique de 3 000 questions** (10 cours × 4
chapitres × 3 sections, 170 nœuds) : `index.json` 4,1 Mo, `rebuild_index`
240 ms, **listing complet 15 ms, recherche 16 ms**, sous-arbre d'un cours
17 ms, `annotate` 8,5 ms. Le stockage n'est pas ce qui limite.

#### ⚠ En ligne, rien n'est tronqué en silence

`limit=500` en dur **tronquait sans le dire** : au-delà, les questions
manquantes n'existaient pas du point de vue de l'interface — introuvables, sans
le moindre signe. C'est le plafond qui mord en premier dès qu'une banque
rassemble plusieurs cours (le local, lui, n'a aucune limite : il lit l'index
entier). Les quatre `limit` en dur (`bank_questions` 500, `bank_categories`
2 000, `question_categories` 10 000 ×2) ont disparu au profit de
`_fetch_paged(path, params)` : pages de `PAGE = 500`, plafond dur
`MAX_ROWS = 20000`, et un `truncated` **rendu**.

- `list_questions(filters, report=…)` remplit `report["truncated"]` — et
  `bank.py` le pose à `False` pour la même raison : l'appelant ne doit pas
  avoir à deviner selon le backend qu'il a en face.
- `GET /api/bank` rend `truncated` **même à False**, et `/banque` affiche
  « ⚠ liste tronquée » à côté du compte. Une liste incomplète qui se présente
  comme complète est pire qu'une erreur.
- ⚠ Le plafond est lu **à l'appel**, pas figé en valeur par défaut d'argument
  (évaluée à la définition) : changer `MAX_ROWS` n'aurait rien changé, le genre
  de dépendance qui ne se voit qu'au moment où l'on croit l'avoir réglée.
- `tests/fake_postgrest.py` **applique** `limit`/`offset` au lieu de les
  ignorer : un faux backend qui rend tout d'un coup ferait passer un code qui
  tronque.

#### L'arbre à l'échelle : repli par défaut, filtre, et portée persistée

Trois changements dans [bank_tree.js](auto_grading/front/static/bank_tree.js),
qui tiennent ensemble : à dix cours, l'arbre fait des centaines de nœuds.

- **Repli par défaut aux deux premiers niveaux** (`_applyDefaults`) : on voit
  les cours et leurs chapitres, pas les sections. ⚠ Il ne s'applique qu'à la
  **première** ouverture d'une banque — d'où `this.stored`, qui distingue
  « aucune préférence » de « rien n'est replié », deux états qu'un ensemble
  vide confondait et qui demandent des affichages opposés.
- **Champ de filtre sur les noms** (`.bt-search`), à partir de 12 nœuds — en
  dessous, il coûte une ligne et ne sert à rien. Accents repliés des deux
  côtés, comme la recherche de questions. Il montre les nœuds qui
  correspondent, **leurs ancêtres** (sans eux on perd le chapitre auquel
  appartient une section homonyme) et **leurs descendants** (sans eux on ne
  peut pas descendre dans le cours qu'on vient de trouver).
  ⚠ **Sous filtre, le repli ne s'applique plus** : un nœud qui correspond mais
  dort dans une branche repliée resterait introuvable — exactement ce qu'on
  venait chercher. Le caret affiche alors « ▾ », sinon il dirait le contraire
  de ce qu'on voit.
  ⚠ Le re-rendu détruit le champ : le focus et la position du curseur sont
  rendus après coup, sinon on ne peut pas taper deux lettres de suite.
- **La sélection EST la portée, et elle est persistée** (`selected` dans le
  même `localStorage` que le repli, clé par banque). C'est ce qui rend les
  neuf autres cours invisibles quand on n'en travaille qu'un : la page
  s'ouvre déjà filtrée. La portée est rappelée **à côté du compte**
  (`renderCount` → `.bq-scope`, avec un ✕) — l'arbre peut être défilé loin de
  la ligne surlignée, et on se demanderait pourquoi la banque ne montre que
  12 questions sur 3 000.
  ⚠ `setStorageKey` relit **tout** l'état, pas seulement le repli : sans ça la
  portée de la banque précédente filtrerait la nouvelle, sur un id qui n'y
  existe pas. Et elle ré-émet le filtre — la page a déjà chargé ses questions
  sans filtre au moment où la banque active devient connue.
  ⚠ Une portée qui désigne un nœud **disparu** est effacée à la lecture
  (`_reindex`) : filtrer sur un id fantôme rendrait une liste vide sans qu'on
  voie pourquoi.

⚠ **`.banque-list-rows` a `flex-basis: 0`, pas `auto`.** Avec `auto`, la liste
réclame la hauteur de ses 3 000 lignes comme base, et le rétrécissement —
proportionnel à la base — écrasait l'arbre à un cinquième de sa place (mesuré :
125 px au lieu de 340). À 0, l'arbre garde sa hauteur et la liste prend ce qui
reste ; quand l'écran est court, c'est elle qui touche son `min-height` et
l'arbre cède à son tour.

#### Séparateurs glissables — le partage de l'espace appartient au lecteur

Deux poignées sur `/banque`, un seul mécanisme (`makeGutter` dans
`banque.html`) : `.bq-hsplit` partage le panneau gauche entre l'arbre et la
liste, `.bq-vsplit` partage la page entre la liste et la fiche. Un partage figé
ne peut pas être bon : ce qu'on veut voir change d'une minute à l'autre — la
structure du cours quand on range, la liste quand on cherche.

- ⚠ **Le JS écrit une variable CSS sur un hôte, il ne touche jamais au style
  des panneaux.** Poser un `style.flexBasis` à la main mettrait la règle hors
  de portée des media queries : une largeur choisie sur grand écran survivrait
  au passage en **une colonne** (< 1100 px). D'où `--bq-list-w` +
  `.has-list-w`, que la media query ré-écrase.
- ⚠ **La butée garde toujours 260 px au panneau et 120 px à la liste** : un
  séparateur poussé à fond ne doit pas reproduire le défaut qu'il corrige
  (liste invisible, cf. ci-dessus).
- **Double-clic = retour au défaut**, et la valeur est retirée du
  `localStorage`. Un réglage qu'on ne sait pas défaire est un réglage qu'on
  n'ose pas toucher.
- **Flèches au clavier** (Maj = pas de 40 px) : une poignée qu'on n'attrape
  qu'à la souris n'est pas atteignable pour qui n'en a pas.
- La zone de saisie fait 7 px, le trait visible 3 px (`::after`) : on attrape
  sans viser, sans dessiner une barre de plus.

⚠ **`.banque-list-panel` est passé de 280 à 340 px** et la ligne n'affiche plus
que la **feuille** de la catégorie (chemin complet en infobulle). 280 px
suffisaient tant que la banque était vide ; avec de vrais titres et une vraie
catégorie par ligne, le titre tombait à « Conclure… » pendant que le chemin
était tronqué à « Modèle linéaire › V… », qui ne distingue rien.

### Backend en ligne (Supabase) — multi-user

À côté des banques locales, un backend Supabase est disponible pour partager
une banque entre plusieurs profs (communauté ouverte). Chaque banque online
a ses propres credentials (URL + clé anon + tokens user) — on peut donc avoir
plusieurs banques online en parallèle (cf. V2 multi-banques ci-dessus).
Setup : voir [supabase/README.md](supabase/README.md).

**Architecture** :
- [auto_grading/bank.py](auto_grading/bank.py) reste le backend local.
- [auto_grading/bank_online.py](auto_grading/bank_online.py) : client HTTP qui
  tape sur PostgREST de Supabase, même API que `bank.py`. Tous les
  reads/writes passent par `config.active_bank_cfg()` (URL / anon /
  user_token / refresh_token).
- [auto_grading/bank_auth.py](auto_grading/bank_auth.py) : flot OTP code à
  6 chiffres par email (pas de magic link cliquable → zéro redirect URL à
  configurer). Lit/écrit dans la banque active via
  `config.active_bank_cfg()` / `config.update_active_bank(...)`.
- [auto_grading/front/server.py](auto_grading/front/server.py) helper `_bank()` :
  dispatcher qui retourne `bank` ou `bank_online` selon
  `config.active_bank_cfg()["type"]`.

**Schéma Postgres** ([supabase/schema.sql](supabase/schema.sql)) :
- `profiles` : extend `auth.users` (display_name + institution)
- `bank_questions` : `{id uuid, author_id, kind, data jsonb, title, tags[],
  status ∈ {draft,public,archived}, ...}`
- `question_evals` : `{question_id, user_id, project_name, n_eval,
  sum_normalized, n_perfect, ...}` (unique sur le triplet)
- **RLS** : tout le monde lit les `status='public'` + ses propres lignes.
  L'auteur seul modifie ses questions. Chaque user voit seulement SES propres
  évals. Toute la logique d'autorisation tient en 4 blocs SQL.

**Auth** : flot OTP — `POST /api/bank/auth/send-otp {email}` → Supabase envoie
un code 6 chiffres → user le saisit → `POST /api/bank/auth/verify-otp
{email, code}` → access_token + refresh_token persistés dans la banque
active (`banks[active].user_token`, `.refresh_token`, `.user_id`, `.user_email`)
via `config.update_active_bank(...)`. Refresh transparent via
`bank_auth.refresh_token_if_possible()` au 1er 401.

**⚠ Mode invite-only (FORTEMENT recommandé)** : par défaut, n'importe qui
peut signup avec son email — y compris tes étudiants — et lirait les
questions `status='public'` AVEC les bonnes réponses (champ `data` jsonb
contient `correct: true/false`). Catastrophe pour la confidentialité.
Solution : Dashboard Supabase → Authentication → Providers → Email →
**décocher "Enable email signups"** + inviter chaque prof via
Authentication → Users → Invite user. Le message d'erreur côté AMCx est
clair pour l'étudiant qui essaierait (`"Cette banque est en mode invite-only.
Demande à l'admin de t'inviter."`). Voir
[supabase/README.md § 4.0](supabase/README.md).

**Routes additionnelles** :
- `GET  /api/bank/auth-status` → `{mode, configured, logged_in, user_id, email}`
- `POST /api/bank/auth/send-otp` `{email}` → code 6 chiffres par mail
- `POST /api/bank/auth/verify-otp` `{email, code}` → persiste tokens
- `POST /api/bank/auth/logout` → efface tokens locaux

Les routes existantes `/api/bank*` (list, load, save, delete, sync, import)
dispatchent automatiquement vers le backend choisi — code UI inchangé.

**Migration locale → en ligne** : script
[auto_grading/bank_migrate.py](auto_grading/bank_migrate.py) :
```bash
python auto_grading/bank_migrate.py --also-patch-projects
```
Itère `~/Documents/AMCx-banque/*.json` → upload sur Supabase (status `draft`),
préserve les `stats.by_project.*` → `question_evals`, persiste le mapping
`{ancien_8hex: nouveau_uuid}` dans `~/.config/amcx/bank_migration.json`.
Avec `--also-patch-projects`, parcourt les projets connus (recent_projects())
et patche `data._bank_id` des blocs concernés. Idempotent.

**Différences avec le local** :
- `bank_id` = UUID v4 (36 chars) au lieu de 8 hex (collision-free pour la
  communauté).
- `stats.by_project` n'est PAS embarqué dans la question — c'est une table
  séparée (`question_evals`). `bank_online.load()` la reconstruit pour le user
  courant (RLS) avant de retourner — compat UI 100%.
- `status` ∈ {draft, public, archived} : par défaut `draft` (visible que par
  l'auteur). L'user passe à `public` quand prêt à partager.

**Free tier Supabase** : 500 Mo DB + 2 Go egress/mois + 50k MAU. Couvre
largement <1000 profs. Self-hostable plus tard (`supabase start` local).

### Phase B — ratings, favoris, tags persos, stats agrégées (livré)

3 nouvelles tables activées dans [supabase/schema.sql](supabase/schema.sql) :
- **`question_ratings`** : `(question_id, user_id)` PK, `stars 1-5`, `favorite
  bool`, `comment text`. RLS : tous lisent (pour agréger), seul l'auteur du
  rating écrit.
- **`question_personal_tags`** : `(question_id, user_id)` PK, `tags text[]`.
  RLS : strictement perso (lecture + écriture par soi seul).
- **`get_question_eval_stats(qid)`** : fonction RPC `SECURITY DEFINER` qui
  bypass RLS sur `question_evals` pour retourner des agrégats anonymes
  (n_users, n_projects, total_n_eval, avg_normalized). Sans elle, un user
  normal ne pourrait pas compter combien de profs ont utilisé une question.

**Nouvelles fonctions** dans [bank_online.py](auto_grading/bank_online.py) :
- `get_my_rating(bank_id)` / `rate(bank_id, stars?, favorite?, comment?)` /
  `delete_my_rating(bank_id)` — upsert via `Prefer: resolution=merge-duplicates`.
- `get_my_personal_tags(bank_id)` / `set_personal_tags(bank_id, tags)`.
- `get_global_stats(bank_id)` — combine RPC + agrégation client-side des
  `question_ratings` (avg_stars + n_favorites + n_ratings).
- `set_status(bank_id, status)` — toggle draft ↔ public (auteur seul via RLS).
- `update_question_content(bank_id, data, title?, tags?, bump_version=True)`
  — PATCH d'une question existante (incrémente `version`).

**Nouveaux filtres** dans `list_questions(filters)` :
- `mes_favoris=True` → pré-fetch mes question_ids favoris puis restrict
- `mon_tag='cours-L3'` → pré-fetch mes question_ids avec ce tag perso
- `status='draft'` → ne voir que mes brouillons (via RLS naturel)

**Nouvelles routes serveur** (online only, retournent 400 en local) :
- `GET/POST/DELETE /api/bank/<id>/rating`
- `GET/POST /api/bank/<id>/personal-tags`
- `GET /api/bank/<id>/global-stats`
- `POST /api/bank/<id>/status` (toggle draft/public/archived)
- `POST /api/bank/<id>/update-from-block` `{bid, title?, tags?}` — push les
  modifs d'un bloc local vers la version banque (bump version)

**UI — modale Banque enrichie** :
- Sidebar filtres : ❤ Mes favoris, "Mes tags persos" (input), 📝 Mes brouillons
  (visibles seulement en online + logged)
- Panneau preview :
  - Widget rating (5 étoiles cliquables + clear)
  - Toggle ❤ Favori
  - Textarea commentaire perso + bouton Sauver
  - Chips tags persos (ajout/suppression inline)
  - Card stats globales : ⭐ moyenne + N notes · ❤ K favoris · 👥 X profs · 📊 N évals
  - Si je suis l'auteur : badge status + version + bouton Publier/Dépublier
    + bouton Supprimer

**Limitations connues** :
- Pas de page profil publique (cliquer sur un nom d'auteur n'affiche pas ses
  autres questions)
- Pas de bouton "Mettre à jour cette question dans la banque" depuis le
  toolbar des blocs du sujet (l'API `/api/bank/<id>/update-from-block` existe
  mais l'UI n'est pas câblée)
- Filtre "stars ≥ N" : on filtre uniquement sur la moyenne globale (pas
  implémenté ; demande RPC additionnelle)

## Édition IA assistée (Sonnet/Opus, 1 appel par modif)

Bouton **🤖** dans la toolbar de chaque bloc QCM (canonique seul) → modale
« Modifier la question avec Claude » :
- Textarea pour la demande (« reformule plus clairement », « ajoute 2
  distracteurs », « convertis en mult », …)
- 1 seul appel API à Sonnet/Opus avec tool use (`propose_edit`) → JSON
  structuré garanti valide
- Diff side-by-side (avant/après) avant application
- Bouton « Appliquer » → `POST /api/sujet/blocks/update` → exam.tex réécrit

**Auth** : 2 voies, détection automatique via `/api/ai/auth-status` :

1. **Clé API Anthropic** dans Réglages (dashboard `<details>` repliable).
   Stockée dans `config.anthropic_api_key`. Fallback sur `$ANTHROPIC_API_KEY`.
   `config.ai_model` choisit Sonnet 4.6 / Opus 4.7 / Haiku 4.5. ~1.5¢/édition.

2. **Claude Code subprocess** (fallback si pas de clé API). Spawn `claude
   --print --output-format json --system-prompt "…" --disallowed-tools …
   -p "…"` au lieu d'un appel API. Utilise l'auth OAuth de l'utilisateur
   (abonnement Pro/Max) — facture sur quota, pas en argent. Le binaire est
   cherché via env `CLAUDE_CODE_EXECPATH`, puis `which claude`, puis glob
   de l'extension VSCode `~/.vscode/extensions/anthropic.claude-code-*/…/claude`.

**Coûts comparés** :
- API key : ~1.5¢/édition (Sonnet 4.6, 1.5k input + 800 output)
- Claude Code : ~$0.05/édition sur quota abonnement (~6k tokens
  d'overhead de cache par appel, no shared state entre subprocess) →
  ~400 éditions/mois sur Pro à €20

**UI** : panneau « Connecter Claude » dans la modale 🤖 si **ni** clé API
**ni** CC détecté. 2 cartes côte à côte avec liens directs vers
<https://console.anthropic.com/settings/keys> (clé) et
<https://claude.com/claude-code> (install CC). Bandeau bleu dans la
modale d'édition indiquant le backend actif (clé API ou Claude Code).
Le résultat affiche `backend` et `cost_usd` pour transparence.

**Actions supportées par le tool `propose_change`** :
- `action="edit"` (1 bloc) : remplace la question courante (diff side-by-side
  dans l'UI, validation `update_block`).
- `action="add_after"` (1-6 blocs) : insère N nouvelles questions APRÈS la
  question courante (aperçu liste verte, validation = N appels successifs
  à `/api/sujet/blocks/add` qui chaînent par `after_bid`).
- Claude détecte l'intent depuis le prompt (mots-clés "reformule" → edit,
  "ajoute / propose une question / en dessous / 3 sur le même thème" → add_after).

**Token counter** (mémoire de session, reset au redémarrage server) :
- `GET /api/ai/usage` → `{n_calls, input_tokens, cache_creation, cache_read,
  output_tokens, cost_usd, by_backend, by_model, started_at}`.
- `POST /api/ai/usage/reset` → vide le compteur.
- Widget « 📊 Consommation session » dans Réglages → IA du dashboard
  (auto-refresh quand on déplie le panneau).
- Chaque réponse de `/api/ai/edit-block` inclut `total: {...}` pour
  affichage live dans la modale.

**Routes** :
- `GET /api/ai/auth-status` → `{has_api_key, cc_binary_path, ai_model}`.
- `GET /api/ai/usage` → compteur session.
- `POST /api/ai/usage/reset` → reset compteur.
- `POST /api/ai/edit-block` `{bid, prompt}` → `{ok, action, current, proposed,
  new_data, after_bid, rationale, model, backend, cost_usd, usage, total}`.
  `proposed` est soit `{qtype, statement, answers}` (action=edit) soit
  `{blocks: [...]}` (action=add_after). Validation par bloc (≥2 réponses,
  ≥1 correcte, single = exactement 1). Tags sanitizés (`re.sub` ascii+_).
- `POST /api/config` accepte `anthropic_api_key` + `ai_model` (modèles
  whitelistés).

## Reconnaissance d'écriture manuscrite (HTR via Claude Vision)

Module [auto_grading/htr.py](auto_grading/htr.py) qui ajoute deux capacités via
**Claude Vision** (extra `[api]` déjà câblé pour l'édition IA assistée) :
- **Feature A** : auto-détection de l'identité depuis le `\champnom` manuscrit
  — Claude reçoit le crop + la liste fermée des 174 étudiants et pick le bon.
- **Feature B** : lecture des cases libres `question_freeform` + auto-grade
  contre `expected_answer` (modes `exact`, `numeric_tol`, `contains`, `regex`).

**Historique** : une 1ʳᵉ version basée sur TrOCR (HuggingFace, local CPU)
plafonnait à 72% top-1 sur EXAM_2026 → remplacée par Claude Vision (~95%+
attendu, ~$0.001/copie avec Haiku).

**Activation** : automatique dès qu'une clé API est posée dans
`config.anthropic_api_key` (ou env `ANTHROPIC_API_KEY`) ET que le SDK
`anthropic` est installé (`uv pip install -e ".[api]"`). Sans ça, l'UI
désactive les boutons 🪄 avec un tooltip explicatif.

**Modèle** : `config.ai_model_htr` (défaut `claude-haiku-4-5`). Distinct
de `config.ai_model` (édition assistée du sujet) pour permettre Haiku ici +
Sonnet ailleurs. Coût Haiku : ~$0.001/copie → ~$0.20 pour un examen de 174
copies. Sonnet : ~3× plus cher (toujours négligeable).

### Feature A — Auto-id depuis le nom manuscrit

**Routes** :
- `GET  /api/htr/status` → `{available, has_api_key, sdk_installed, model_id,
  install_hint}`
- `POST /api/htr/recognize-name` `{batch, page}` → `{ok, best_id, best_full,
  raw_text, confidence, n_candidates}` (Claude pick directement, plus de
  fuzzy match côté serveur).
- `POST /api/htr/recognize-names-all` → task async sur les copies non
  résolues, polling via `GET /api/htr/recognize-names-all/<task_id>`.

**Stratégie prompt — smart top-K** : `_build_htr_candidates(matcher,
student_id)` pré-filtre la liste de 174 étudiants par les digits du
`student_id` partial (≥ 2 digits non-`?` → narrow par préfixe matching,
typiquement ≤ 20 candidats). Sinon liste complète (avec Haiku c'est de
toute façon négligeable). Réduit l'ambiguïté quand Claude voit p.ex.
3 ABBOUD différents et que la grille pointe vers les 4 derniers chiffres.

**Compteur tokens** : `_record_ai_usage("api", model, usage, cost)` ré-utilisé
(le widget « Consommation session » du dashboard cumule les tokens HTR +
édition IA).

**UI `/identites` (refonte)** :
- **Panel droit = TOUS les étudiants** (pas seulement les libres) :
  `.rf-chip.unassigned` orange, `.rf-chip.assigned` vert avec « → batch/pNNN ».
- **Click-to-select / click-to-assign** : click sur une carte gauche → border
  bleue (`.selected`) ; click sur un chip droite → assigne. Click sur un chip
  déjà assigné (sans carte selected) → confirm désassignation.
- **Bouton 🪄 Auto-détecter tout** : modal de confirmation + task batch +
  polling. Chaque carte reçoit sa suggestion sous forme d'1 chip cliquable
  `.rf-htr-chip` (Claude est ~95% top-1, plus besoin d'afficher top-3/5).
- **Drag&drop préservé** en alternative (chips libres seulement, draggable=false
  sur les assignés).

### Feature B — Cases freeform (question_freeform)

Nouveau kind dans [sujet_store.py](auto_grading/sujet_store.py) :
`question_freeform` avec data `{tag, statement, expected_answer, match_mode,
numeric_tol, lines, points}`.

Rendu LaTeX : `\AMCOpen{question=..., lines=N}{0/points}`. Sans clé API
Claude, le correcteur ticke manuellement la case 0/points sur la feuille
(rétrocompat). Avec clé API, `htr.recognize_text` (= Claude vision) lit le
texte de l'étudiant + `htr.match_answer` recalcule le score auto.

**Round-trip** : `expected_answer`/`match_mode`/`numeric_tol` ne se rendent
pas dans le PDF → stockés dans une ligne `%%QCM-FREEFORM-DATA <json>`
(commentaire LaTeX) en début de body du bloc.

**Calibration géométrique** : après `compile_pdf()`, `calibrate_open_zones()`
parse le PDF avec PyMuPDF, cherche le marker invisible `ffz<bid>` (rendu en
1pt gris clair dans le `question=`), en déduit la bounding box de la zone de
réponse (rectangle large sous le marker, hauteur `lines × 24pt`). Écrit
`sujet/open_zones.json`.

**Au grade time** : `cv_grade.grade_image()` (si `htr.is_available()` ET
`sujet/open_zones.json` existe) — crop → Claude vision → match → score.
Écrit dans la clé `open_answers` du JSON ; baseline immuable dans
`_cv_open_answers`. Override via `POST /api/open-answer-override` + onglet
« Réponses libres » dans `/zoom`.

### Pièges

- **Connexion internet requise** (Claude API). Pas de mode offline.
- **Latence batch** : ~1.5 s/copie séquentiel. 174 copies = ~4 min. Améliorer
  en parallélisant les reqs Claude (asyncio) — pas fait en V1.
- `numeric_tol` normalise virgule fr → point + strip whitespace avant
  comparaison.
- HTR **jamais appelé par `_run_pipeline` auto** : uniquement à la demande
  (bouton 🪄 ou onglet zoom). La pipeline reste rapide même sur projet avec
  `question_freeform`.
- Marker invisible `ffz<bid>` : `\color{gray!30}\fontsize{1pt}` — nécessite
  `xcolor` (chargé par AMC). Si une compile flatten les couleurs, le marker
  reste lisible mais visible — pas grave.

## Onglet « Courriels » — envoyer les notes aux étudiants

Page `/mail` + moteur [mail_results.py](auto_grading/mail_results.py) (portage
du script Julia d'origine). Lit `compte_rendu/notes.csv`, recolle le prénom
depuis la liste étudiants, et envoie à chacun un message sobre en anglais.

L'onglet édite le gabarit, l'objet, l'expéditeur, le serveur et le secret ;
il montre l'aperçu sur un vrai destinataire, la liste de qui recevra quoi, et
lance l'envoi avec une barre de progression. Le CLI fait la même chose sans
navigateur :

```bash
python auto_grading/mail_results.py                          # simulation
python auto_grading/mail_results.py --only moi@x.fr --send   # essai
python auto_grading/mail_results.py --send                   # envoi
```

⚠ **Les réglages de l'onglet font foi pour les deux chemins** : le CLI lit
`mail_*` en config, les options ne font que les surcharger. Sans ça, la ligne
de commande et l'interface enverraient deux messages différents.

### Gabarit : `$champ`, pas `{champ}`

`string.Template` plutôt que `str.format` : un texte de courriel contient des
accolades bien plus souvent qu'un `$`, et `str.format` obligerait à les
doubler. Champs : `$name` (prénom), `$full_name`, `$email`, `$id`, `$score`,
`$max_score`, `$date`, `$sender`. Un `$` littéral s'écrit `$$`.

- Le gabarit vit **dans le projet** (`mail_template.txt`), initialisé depuis
  celui fourni ([mail_results.txt](auto_grading/mail_results.txt)) au premier
  accès : le texte et la date sont propres à un examen.
- ⚠ **L'objet passe par le même moteur** que le corps (`$name`, `$date` y sont
  utiles). Les trois chemins le rendent : aperçu, worker de l'onglet, CLI.
- ⚠ **Un `$champ` inconnu et un `$` isolé lèvent un message qui dit quoi
  faire**, jamais un `KeyError` nu ni un `ValueError` au milieu d'un envoi.
- ⚠ `default_subject("")` rend « MCQ results », **sans tiret cadratin
  orphelin** : « MCQ results — » serait parti tel quel dans 38 boîtes le jour
  où la date n'est pas renseignée.
- ⚠ **L'aperçu porte sur le contenu du formulaire**, pas sur le disque : le
  gabarit voyage dans le corps de `POST /api/mail/preview`, éditions non
  enregistrées comprises.

### Enregistrement automatique — Ctrl+Shift+R ne perd rien

Les réglages et le gabarit s'enregistrent **seuls** : 400 ms après la dernière
frappe, et **immédiatement** sur `change` (sortie de champ, choix dans un menu).

⚠ **Un garde-fou `beforeunload` n'aurait pas suffi** : un rechargement forcé
(Ctrl+Shift+R) ne restaure aucun champ de formulaire — contrairement à un F5 —,
et un gabarit réécrit en entier partait sans un mot. Avertir n'était pas ce
qu'on voulait : on voulait qu'il n'y ait rien à perdre. Rien sur cette page
n'envoie de courriel, donc persister au fil de la frappe est sans risque.

⚠ **Le mot de passe est exclu de l'automatisme** : il ne part que par le
bouton, pour qu'une saisie à moitié tapée ne soit jamais enregistrée.

⚠ **Le rechargement peut devancer l'enregistrement.** Deux filets, parce qu'un
seul ne suffisait pas :
- `visibilitychange` → `navigator.sendBeacon` (et non `fetch`, qui serait
  annulé par la navigation) pour la dernière rafale ;
- `reconcile()` au chargement : la page est rendue depuis `config.json`, et un
  beacon parti juste avant peut y arriver **après**. Sans ce contrôle, l'écran
  réaffichait l'ancienne valeur — puis la frappe suivante la réenregistrait,
  effaçant en silence les dernières touches. Il ne recale que les champs
  auxquels personne n'a encore touché (`touched`), jamais par-dessus une
  saisie en cours. Mesuré : un rechargement lancé 0 ms après la frappe affiche
  désormais la bonne valeur.

⚠ **Une valeur DÉRIVÉE ne doit pas se figer** : le champ « Identifiant SMTP »
vide signifie « la même que l'adresse d'expédition ». Le formulaire affichait
la valeur résolue, que l'enregistrement automatique gravait aussitôt en dur —
changer l'adresse ensuite ne l'entraînait plus. D'où `smtp_user` (effectif,
pour la connexion) **et** `smtp_user_raw` (stocké, pour le champ). Tout défaut
dérivé ajouté ici demandera la même paire.

### Le mot de passe

`~/.config/amcx/smtp_password`, créé en **0600 avant d'être écrit** (`os.open`
avec le mode : poser les droits après laisserait une fenêtre où le secret est
lisible de tous).

⚠ **Il y est EN CLAIR, et l'interface le dit.** Le chiffrer sans demander une
phrase secrète à chaque envoi rangerait la clé juste à côté — de l'apparence de
sécurité. Ce qui est réellement acquis, et qui est le point :

- il est **hors du dossier de projet**, qui se partage (sujet, scans, config) ;
- **aucune route ne le renvoie** : `_mail_settings()` n'expose que
  `password_set: bool`, et le champ du formulaire est en écriture seule ;
- ⚠ **un champ vide ne l'efface pas** — la clé `password` n'est traitée que si
  elle est présente dans le corps, sinon enregistrer les réglages détruirait le
  secret à chaque fois. L'effacement est un bouton explicite.

Précédence : `AMCX_SMTP_PASSWORD` > fichier > saisie au clavier (CLI seul).
Pour Gmail, c'est un **mot de passe d'application**, jamais celui du compte.

⚠ **Rien ne part sans `--send`.** Un envoi à toute une promo est irréversible
et sort du poste. Le mode par défaut se connecte à zéro serveur : il imprime
les destinataires, les écartés et le message rendu, puis s'arrête.

⚠ **Le mot de passe n'est écrit nulle part** — ni dans le code, ni dans
`config.json`, qui suit le projet quand on le partage et que `public_config()`
ne masquerait pas. Il vient de `AMCX_SMTP_PASSWORD`, sinon il est demandé au
clavier. Pour Gmail, c'est un *mot de passe d'application*.

⚠ **Rien n'est écarté en silence** : une copie sans adresse, sans note ou non
reliée à un étudiant est **listée** avant l'envoi, avec son motif. Sur un envoi
de masse, une ligne filtrée sans un mot est un étudiant qui ne recevra rien sans
que personne ne le sache — et « il était absent » ne ressemble pas à
« l'export est cassé ».

⚠ **Le barème annoncé est celui du sujet** (`mail_results.default_max_score` →
`sujet_store.subject_total_max()`) : depuis que la note d'un examen est son
score brut, `note_finale` et `QCM_brut` vivent sur la **même** échelle. Ce
n'est surtout pas `final_threshold`, un plafond dur souvent laissé à 20 alors
que le sujet vaut 5 — « 5 / 20 » à un étudiant qui a tout juste. Le repli
historique (moyenne pondérée des `max` de chaque colonne) ne sert plus qu'aux
projets dont le sujet n'est pas lisible. Pour toute autre colonne, `--out-of`
est **exigé** plutôt que deviné.

⚠ **Le prénom vient du roster, pas du csv.** `nom_prenom` colle les deux ; le
découper serait faux dès qu'un nom de famille est composé (« ADJEBA MBA
Christian »). Sans roster chargé, repli sur le nom complet — jamais « Dear , »,
qui signalerait à l'étudiant un envoi bâclé.

- **Journal** `compte_rendu/mail_log.csv` : une adresse déjà servie n'est pas
  re-servie (`--force` pour passer outre). Relancer la commande après trois
  échecs ne doit pas re-notifier toute la promo. Un **échec** reste à renvoyer.
- Un échec n'interrompt pas la boucle : sur 40 destinataires, s'arrêter à la
  première adresse morte laisserait 39 personnes sans leur note.
- Les notes négatives sont ramenées à 0 (comme le script d'origine) ;
  `--no-floor` pour les annoncer telles quelles.
- Testé de bout en bout contre un **serveur SMTPS jetable** : 39 messages
  (1 essai + 38 étudiants), 39 destinataires distincts, en-têtes et corps
  vérifiés sur les messages réellement reçus. `tests/test_mail_results.py`
  couvre la logique (gabarit, secret, journal, échelle, simulation).

**Routes** : `GET /mail` · `GET /api/mail` · `POST /api/mail/settings`
(réglages + gabarit + secret) · `POST /api/mail/preview` (n'écrit rien) ·
`POST /api/mail/send` `{mode: "test"|"all", to?, force?}` → `task_id` ·
`GET /api/mail/send/<task_id>`.

⚠ **Aucun destinataire par défaut** : `mode` est obligatoire, `test` exige une
adresse, et `all` saute les adresses déjà servies sans `force`. Le front
confirme en **nommant** ce qui part (nombre, expéditeur, objet) — un « OK »
réflexe sur « Confirmer ? » ne protège personne.

### ⚠ Les absents : dans les exports, hors des courriels

Un étudiant de la liste qu'**aucune copie ne réclame** (`server.absent_students`)
n'existait nulle part. Les deux moitiés du correctif comptent :

- **Dans les exports** (`/export.csv` et `compte_rendu/notes.csv`), il a une
  ligne comme les autres, à sa place alphabétique, avec `ABS`
  (`server.ABSENT_MARK`) pour les colonnes qui dépendent de la copie. Sans
  elle, il disparaît du fichier remis à la scolarité, et « absent » devient
  indiscernable de « oublié dans l'export ».
- **Hors des courriels** : `mail_results.load_recipients` l'écarte **sous son
  nom**, motif « absent (ABS) ». Lui envoyer un message annonçant une note
  qu'il n'a pas serait pire que ne rien envoyer.

⚠ **`ABS` est une chaîne, jamais `0`** : un absent n'a pas eu zéro, il n'a pas
composé. Les confondre fausse toute moyenne recalculée en aval sur le fichier
exporté — et, côté correction, tirerait la moyenne de la promo vers le bas.

⚠ **Ils n'entrent pas dans les statistiques** de l'onglet Évaluation : ceux-ci décrivent les copies corrigées. `absent_students()`
n'est appelé que par les deux écrivains de CSV.

⚠ **Les notes importées d'un absent sont conservées** dans `notes.csv` : il peut
très bien avoir rendu le projet sans venir au QCM. Seules les colonnes issues de
la copie valent `ABS`.

⚠ **`export_scolarite.py` les compte à part** et laisse la cellule **vide** :
la colonne est numérique dans le modèle de la scolarité, y écrire « ABS »
pourrait faire échouer leur import. L'absence est dite dans le récapitulatif
(`n absent(s) laissé(s) vides`), pas devinée par le destinataire du fichier.

## Si tu dois changer le barème

L'éditer dans l'onglet *Sujet* : le barème est écrit dans `sujet/subject.json` et le
recalcul des notes est immédiat partout (`score.py` relit le store).

⚠ **Ne pas éditer `sujet/exam.tex` à la main** : il est régénéré depuis le store à
chaque compilation, une modification directe serait écrasée (un backup
`exam.tex.bak` est écrit avant réécriture). Pour repartir d'un `.tex` édité
dehors, l'importer comme nouveau projet (`new_project.py --from-amc`).

## Décisions de design qui peuvent surprendre

- **CV+ML est source primaire des `answers`** (pas AMC) — choix utilisateur « CV par défaut, flag si diff AMC ». La ground truth AMC est dans `_amc_answers`, la diff dans `_cv_amc_diff`.
- **Le ML tourne sur toutes les cases** (et pas seulement une bande grise) ; l'ambiguïté est le désaccord ML/seuil — définition nette voulue par l'utilisateur.
- **Pas d'API Anthropic** dans le pipeline de correction : `grader.py`/`vision_prompt.py` (voie multimodale abandonnée) sont dans [archive/](auto_grading/archive/) ; l'import paresseux de `batch_run.py` échoue désormais avec un message explicite. L'API Anthropic ne sert qu'à l'édition IA du sujet et au HTR.
- **`index.html` supprimé** — `/` rend `evaluation.html` (ex-`dashboard.html`, toutes les pages héritent de `base.html`).
- **`to_review/`** + `prepare_to_review.py` / `import_reviewed.py` / `update_to_review_with_cv.py` / `build_index_md.py` = ancien workflow fichiers, superseded par l'UI → déplacés dans [auto_grading/archive/](auto_grading/archive/) (⚠ `import_reviewed.py` écrivait dans `raw_responses/` sans rien préserver).
- Le serveur Flask est en `debug=off` → **les templates ne se rechargent pas à chaud**, redémarrer après édition.
