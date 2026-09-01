# CLAUDE.md — AMCx (éditeur QCM + correction auto)

Notes pour un agent qui reprend le projet. Lis ce fichier en entier avant d'éditer quoi que ce soit.

**AMCx = AMC eXtended** : éditeur de sujet QCM (interface web) + correction automatique
des copies scannées (OpenCV + ML), sans dépendance au binaire `auto-multiple-choice`.
Seule la compilation `pdflatex` est utilisée. Un projet AMCx = un dossier contenant un
`sujet/exam.tex` (et les artefacts dérivés).

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
| liste étudiants (xlsx) | `config.student_xlsx` : `id_etudiant`, `nom`, `prenom_etat_civil` |

Le sujet vit dans `auto_grading/sujet/subject.json` (**source de vérité unique** —
voir *Store du sujet* plus bas ; `exam.tex` en est un **produit**, régénéré à la
compilation). Le
code vit dans [auto_grading/](auto_grading/). `pyproject.toml` est à la racine.

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

Topbar : menu déroulant à côté du brand **AMCx** affiche le nom du projet
actif + actions (Ouvrir, Créer, Récents, Oublier). Routes API :
- `GET /api/projects` → `{active, active_name, recent, default_root}`
- `POST /api/projects/open` → `{path}` → restart sur le nouveau projet
- `POST /api/projects/forget` → `{path}` → retire des récents (ne touche pas le disque)
- `POST /api/projects/create` → `{name, template, file?}` → crée puis restart
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

**Section « 📁 Fichiers du projet » en haut du dashboard** : 2 cartes
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
Q8 a 6 bonnes réponses (`\bareme{b=1/3,m=-1/3}` dans [exam.tex](EXAM_2026/exam.tex)). Total max = **32**.

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
  - **E4** `predict_proba` ∈ [0.30, 0.70] (GBM peu sûr) ;
  - **E6** structurel : question `single` avec ≠ 1 cellule cochée → question entière flaggée.
- Sortie → `_ambiguous_cells` (liste de dicts `{q, char, decision, ratio, masked, proba, reasons}`) écrite directement dans le JSON (cv_grade et seed_raw_responses la propagent ; l'UI l'affiche en magenta).
- Repli sans classifieur : `ticked = ratio > seuil adaptatif`.
- Modèle chargé depuis `models/cell_clf_full.pkl`.

### 6. PDF → JPEG via PyMuPDF, 300 dpi
[extract_pages.py](auto_grading/extract_pages.py) utilise **PyMuPDF (`fitz`)** + Pillow — pas de poppler. Défaut **`--dpi 300`** : résolution canonique du pipeline. Ne pas réextraire à 200. Les PDF de copies sont **auto-découverts** dans `amc_dir` (`config.scan_pdfs` pour une liste explicite ; option `--pdfs`). Chaque `<nom>.pdf` → `pages/<nom>/`.

### 8. La page de la feuille de réponses est **dérivée**, pas figée
`layout_store` déduit `answer_sheet_page` (la page portant des cases « réponse ») —
pour EXAM_2026 c'est la page 12, pour le gabarit vierge la page 2. De même le
nombre de questions QCM et de colonnes du code étudiant sont dérivés du calage
(cases à lettres = QCM ; cases à chiffres = colonnes ID). Rien n'est figé à 31/35.

### 7. Détection des mires
`cv_grade.detect_mires(edge_margin=50)` filtre les candidats trop proches des bords. Si tu changes cette valeur, relance `cv_benchmark.py`.

## Le classifieur ML (GBM)

Détecte si une case est cochée à partir de **23 features** :
- **18 historiques** (multi-shrink fill ratio, centroïde, composantes connexes, edge density, light-gray Tipp-Ex, contexte par question…) ;
- **5 masquées** (cf. [masked_detect.py](auto_grading/masked_detect.py) — `MASKED_FEATURE_COLS`) : `masked_ratio_e3/e5/e7` à 3 érosions de l'intérieur, `frame_detected` (0/1), `align_residual` (MSE des 4 coins après similarité réf→scan).

- [build_dataset.py](auto_grading/build_dataset.py) — assemble `results/labeled_cells.parquet` : features + labels depuis **relecture UI** (`validated`/`manually_edited`) en **priorité 1**, AMC `manual∈{0,1}` en **repli** (AMC parfois erroné sur les marques pâles ; cf. 27 conflits sur EXAM_2026). La référence masquée (rendu du PDF sujet) est mise en cache au mtime via `masked_detect.get_reference`.
- [train_classifier.py](auto_grading/train_classifier.py) — `HistGradientBoostingClassifier` (sklearn), split **par copie** ; écrit `models/cell_clf_full.pkl` (prod) + `models/cell_clf.pkl` (test) + `results/clf_report.txt`.
- `extract_features()` / `FEATURE_COLS` vivent dans [cv_grade.py](auto_grading/cv_grade.py) ; signature `(warped, box, q_ratios_s18, copy_baseline, offset, ref, ref_corners, masked_feats)` — les 3 derniers servent à brancher la détection masquée ; `masked_feats` permet de réutiliser un calcul (évite le double-calcul dans `grade_image`).
- [cv_benchmark.py](auto_grading/cv_benchmark.py) — accuracy vs ground truth AMC ; `--no-ml` = seuil seul. Sur EXAM_2026 le « 99.89 % » est limité par les 27 erreurs d'AMC lui-même — la mesure honnête est la **CV par copie** dans `clf_report.txt`.

## Configuration runtime

[auto_grading/config.py](auto_grading/config.py) + `config.json` (créé au 1er save ;
`save_config` ne persiste que les clés de `DEFAULTS`, les clés obsolètes sont purgées). Clés :
- **`amc_dir`** (dossier de l'examen : PDF des copies, `data/` AMC éventuel), `scan_pdfs` (liste explicite de PDF, sinon auto-découverte), `answer_sheet_page` (0 = dérivée du calage) ;
- `export_template_xlsx` (modèle xlsx scolarité pour `export_scolarite.py`, "" = aucun) ;
- `student_xlsx`, `xlsx_id_col`, `xlsx_nom_col`, `xlsx_prenom_col` (roster id↔nom) ;
- `grade_files` (fichiers de notes importés, voir [grade_imports.py](auto_grading/grade_imports.py)) — chaque entrée `{path, join_mode:"id"|"name", join_col:<idx>, data_start:<idx>, grade_cols:[{idx, label, seuil, max, agg_weight}], name_overrides:{<nom brut>:<id|null>}}` (colonnes par **index**, jointure par id ou nom fuzzy) ;
- `hist_granularity` (largeur d'une barre d'histogramme, en points) ;
- `qcm_seuil`, `qcm_max`, `qcm_agg_weight` (paramètres de la colonne QCM) ;
- `final_threshold` (plafond dur de la note finale) ;
- `pass_mark` (seuil de réussite : ligne verticale sur l'histo final + comptage des copies en dessous).

Importé par `student_list.py` (roster), `grade_imports.py` (notes importées) et `front/server.py`. Modifiable via l'UI (dashboard ⚙ + boutons « Liste étudiants » / « Fichiers de notes »).

**Dashboard — 2 histogrammes + formule.** Chaque colonne de note (QCM + importées) a `seuil`, `max`, `agg_weight`. Rescaling : `note* = note × max ∕ seuil`.
- Histogramme du haut (calibration) : `note*` superposées, **non plafonnées** ; granularité = largeur de barre.
- Histogramme du bas : note finale = `min( Σ(agg_weightᵢ·noteᵢ*) ∕ Σ agg_weightᵢ , final_threshold )` — moyenne pondérée des `note*` (non plafonnées dans la moyenne), plafond dur appliqué **seulement** sur le résultat.
- La formule est affichée en bas du dashboard (à donner aux étudiants).
- **Nuage de points** : corrélation (Pearson) entre deux notes choisies par l'utilisateur ; survol d'un point → nom de l'étudiant.
- Réglages : « Appliquer » ou **Entrée** dans un champ recalcule tout. Bouton **Sauvegarder le compte rendu** → `/api/save-report` (dossier `compte_rendu/`).

L'import de notes, les réglages et la sauvegarde du compte rendu ne touchent jamais `raw_responses/`.

## Architecture fichiers

```
pyproject.toml                 ← deps (wheels pures, zéro poppler) + extra [api]
auto_grading/
├── config.py / config.json    ← config runtime partagée (amc_dir, etc.)
├── layout_store.py            ← géométrie des cases : parseur .xy (port de meptex)
│                                 + lecteur layout.sqlite ; get_layout() (précédence)
├── new_project.py             ← crée un projet vierge DONNÉES SEULES (config + sujet gabarit, aucun code copié)
├── archive/                   ← code hors service (answer_key, voie Claude-vision,
│                                 ancien workflow to_review/) — voir son README
├── sujet_store.py             ← parse/édite sujet/exam.tex : parse_tex, get_bareme,
│                                 max_score, total_max, save_questions, compile_pdf
├── sujet/                     ← subject.json (SOURCE DE VÉRITÉ) + exam.tex (généré)
│                                 + DOC-sujet.pdf + exam.xy (calage)
├── score.py                   ← applique le barème (single=value/0 ; mult=Σ b/m, peut être négatif)
├── student_list.py            ← StudentMatcher : match par id (last4) + fuzzy nom ; config-driven
├── grade_imports.py           ← import csv/xlsx de notes externes : auto-détection de structure,
│                                 jointure par id OU par nom (fuzzy), résolution manuelle des ambigus
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
│   ├── templates/             ← base.html + dashboard/zoom/flagged/student/identites/sujet/banque
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

# UI (port 5050) — Jinja n'auto-reload PAS (debug off) : redémarrer après édition de template
.venv/bin/python auto_grading/front/server.py --port 5050
pkill -f "front/server.py"

# Re-extraire les PDF (300 dpi)
.venv/bin/python auto_grading/extract_pages.py

# Re-grader (n'écrit que dans raw_responses_cv/) puis re-seed (préserve les modifs user)
.venv/bin/python auto_grading/cv_grade.py --all
.venv/bin/python auto_grading/front/seed_raw_responses.py --preserve-manual

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

**Ordre des onglets** (dans `base.html`) : **Sujet** | Dashboard | **Questions** | Review rapide | Zoom global | Identités | Export CSV.

| Route | Rôle |
|---|---|
| `/sujet` | **Onglet Sujet** : modèle canonique (text/qcm/open) + outline + bandeau global |
| `/` | **Dashboard** : liste étudiants + fiche + 2 histogrammes + nuage de points |
| `/questions` | **Onglet Questions** : ranking par taux de réussite + aperçu PDF + histo par question |
| `/api/questions/stats` | GET : `[{q, tag, type, statement, max_score, n_eval, n_perfect, mean, scores, bank_id}]` pour chaque QCM du sujet |
| `/flagged` | **Review rapide** : cases flaggées / Identité ; filtres validés |
| `/student/<b>/<p>` | Vue copie : image canonique + ronds magenta + zoom embedded |
| `/student/<b>/<p>/zoom` | Onglets *Réponses* (2 zones) / *Identité* (crop nom + grille ID) |
| `/identites` | Review finale : copies non reliées ↔ noms, drag&drop |
| `/sujet/pdf` | PDF du sujet (`sujet/DOC-sujet.pdf`), inline |
| `/sujet/region/<q>.png` | crop PNG de la région d'une question (aperçu) |
| **API Sujet — édition** | |
| `/api/sujet` | GET : `{config, header, answer_sheet, blocks, mode, available_copies, total_max, max}` |
| `/api/sujet/save` | POST batch `{questions:[…]}` (compat legacy, redirige vers blocks/update) |
| `/api/sujet/compile` | POST : `pdflatex exam.tex` → PDF + `.xy` |
| `/api/sujet/config` | POST patch (num_copies, random_seed, shuffle_*) — OK en legacy |
| `/api/sujet/header` | POST patch (canonique seul, refus legacy = 409) |
| `/api/sujet/answer-sheet` | POST patch (canonique seul) |
| `/api/sujet/regenerate-seed` | POST → nouveau seed aléatoire |
| `/api/sujet/blocks/add` | POST `{kind, after_bid?, data?}` → `{bid}` |
| `/api/sujet/blocks/delete` | POST `{bid}` |
| `/api/sujet/blocks/move` | POST `{bid, after_bid|null}` |
| `/api/sujet/blocks/update` | POST `{bid, data}` (OK legacy pour qcm) |
| `/api/sujet/blocks/duplicate` | POST `{bid}` → `{bid}` |
| `/api/sujet/migrate-to-canonical` | POST → ajoute marqueurs `%%QCM-…` + backup |
| **API correction (inchangées)** | |
| `/api/toggle` | toggle case réponse + flag `manually_edited` |
| `/api/set-id-digit` | fixe un chiffre du numéro étudiant |
| `/api/assign-student` | assigne/retire un étudiant |
| `/api/mark_validated` | flag `validated` |
| `/api/config` | GET/POST config dashboard |
| `/api/upload-xlsx`, `/api/student-list` | xlsx liste étudiants |
| `/api/upload-grade-file`, `/api/grade-file`, `/api/grade-file/remove`, `/api/grade-file/resolve` | notes externes |
| `/api/save-report` | écrit `compte_rendu/` : notes.csv + SVG |
| `/api/student-card/<b>/<p>` | fragment HTML fiche étudiant |
| `/export.csv` | CSV récap |
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

La route `/api/save` (écriture d'un JSON arbitraire à un chemin fourni par le
client, sans aucun appelant côté front) a été **supprimée**.

## Identités — match étudiant & doublons

- `student_list.StudentMatcher` : `by_id` (4 derniers chiffres), fuzzy `by_name`, `by_full_id` (id 5 chiffres, pour les overrides).
- `server.resolve_student(d, matcher)` : honore `_student_override` en priorité, sinon `matcher.resolve()`.
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

Marqueurs `%%QCM-PREAMBLE` / `%%QCM-PREAMBLE-END`, `%%QCM-HEADER` / `%%QCM-HEADER-END`,
`%%QCM-BLOCKS-START` / `%%QCM-BLOCKS-END`, `%%QCM-BLOCK bid=… kind=…` / `%%QCM-END bid=…`,
`%%QCM-ANSWER-SHEET` / `%%QCM-ANSWER-SHEET-END`.

### Multi-copies (`\exemplaire{N}` + grille `numerocopie`)

- Quand `cfg.num_copies > 1`, `render_answer_sheet` auto-injecte
  `\AMCcode{copie}{ceil(log10(N+1))}` juste avant `\AMCcodeGridInt{etu}{...}`.
- AMC produit une grille de chiffres `copie[1..N]` sur la feuille de réponses ;
  `cv_grade.detect_copy_id(warped, layout)` la lit par argmax/gap (même algo
  que la grille étudiant) et retourne le numéro de copie scanné.
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

### Bandeau global (`<details>` repliable en haut)

- **Randomisation** : `num_copies`, `random_seed` (+ bouton ♻ régénérer),
  `shuffle_answers`, `shuffle_questions` (= insertion `\melangegroupe{questions}`
  + wrap `\element{questions}{...}` autour des questions).
- **En-tête du sujet** : 2 sous-groupes pliables :
  - *Champs structurés* (établissement, année, auteur, titre, durée, sous-titre,
    instructions) → génère un tableau LaTeX + centerblock.
  - *LaTeX brut* (textarea) → `header.raw_tex` prime si rempli. C'est le cas
    par défaut après migration legacy (l'en-tête original est préservé
    verbatim). En legacy : affiché en `<pre>` readonly.
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
- **`index.html` supprimé** — `/` rend `dashboard.html` (toutes les pages héritent de `base.html`).
- **`to_review/`** + `prepare_to_review.py` / `import_reviewed.py` / `update_to_review_with_cv.py` / `build_index_md.py` = ancien workflow fichiers, superseded par l'UI → déplacés dans [auto_grading/archive/](auto_grading/archive/) (⚠ `import_reviewed.py` écrivait dans `raw_responses/` sans rien préserver).
- Le serveur Flask est en `debug=off` → **les templates ne se rechargent pas à chaud**, redémarrer après édition.
