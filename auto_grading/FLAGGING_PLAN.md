# Plan — Levier 2 : flagging fiable des erreurs de lecture de cases

> Document de passation. Lis-le **en entier** avant de coder. Le travail vit dans
> `projet_modele/` (copie généralisée du pipeline QCM ; l'original `auto_grading/`
> et `EXAM_2026/` restent figés — ne pas les toucher).

---

## 1. Contexte

### 1.1 Le projet
Pipeline de correction automatique de QCM au format AMC. Chaque copie est une
feuille de réponses scannée (photo CamScanner). Le pipeline détecte, pour chaque
case, si elle est cochée. Lis d'abord **`projet_modele/CLAUDE.md`** (architecture,
`config.amc_dir`, `layout_store`, etc.).

Pipeline actuel : `extract_pages.py` → `cv_grade.py` (OpenCV + classifieur GBM) →
`front/seed_raw_responses.py` → UI Flask.

### 1.2 La détection de case aujourd'hui (`cv_grade.py`)
- `box_fill_ratio(warped, box, shrink=0.18, offset)` : binarise un crop de la case
  rétréci de 18 %, renvoie la fraction de pixels noirs.
- `extract_features()` : 18 features (multi-shrink fill ratio, centroïde,
  composantes connexes, densité de contours, gris-clair Tipp-Ex, contexte…).
- `grade_image()` : warp homographique sur les 4 mires → pour chaque case, fill
  ratio + features → un **GBM** (`models/cell_clf_full.pkl`) décide cochée/vide.
- Flag actuel `ambiguous` = ML ≠ seuil adaptatif. **Très mauvais rappel** : sur
  67 cases corrigées par l'utilisateur lors de sa relecture, seules **3** étaient
  flaggées `ambiguous` (et 41 par le jaune `cv_differs_amc`). 24 erreurs sans
  aucun signal.

### 1.3 Le problème
Une case **vide** lue par `box_fill_ratio` n'est pas « propre » : le crop contient
le **cadre imprimé** ET une **lettre imprimée** (A, B, C… au centre de la case).
Le `shrink` enlève le cadre mais **pas la lettre**. Pire, l'encre de la lettre
varie fortement (`I` ≈ 8 %, `W`/`B` ≈ 25 %+) → biais par-lettre que le seuil
adaptatif par-question ne peut pas modéliser → erreurs « ML + seuil faux ensemble »
= silencieuses (non flaggées).

---

## 2. Ce que le prototype a établi  (`proto_mask_benchmark.py`)

**Lis et exécute `proto_mask_benchmark.py`** — c'est la spécification de la
détection masquée. Idée : mesurer la noirceur **uniquement hors de l'encre
imprimée**.

1. **Référence** = rendu 300 dpi de la feuille de réponses du **PDF du sujet**
   (`render_reference()`), dans l'espace canonique du calage → cases vides
   parfaites (cadre + lettre, sans bruit).
2. **Calage par case** : on détecte le **cadre carré** dans le scan
   (`detect_frame()` : Otsu + `findContours` + `minAreaRect`, contour ~carré de
   la bonne taille, le plus centré → 4 coins). On détecte le cadre de la même
   façon **dans la référence**. Similarité réf→scan (`estimateAffinePartial2D`,
   translation + rotation + échelle) sur les 4 coins.
3. **Masque** = encre imprimée de la référence calée (`ref_al < 200`, dilatée).
4. **Mesure** = fraction de points sombres **hors masque**, à l'intérieur érodé
   de la case ; seuil **relatif** au papier (p85 du crop), pas de soustraction
   d'image (`masked_ratio()`).

### Résultats mesurés (9071 cases ground-truth AMC, seuil oracle/question)
| Méthode | Précision | Erreurs | Biais par-lettre (σ) |
|---|---|---|---|
| Ancienne (`shrink`) | 99.30 % | 60 (12 FP, 48 FN) | 0.054 |
| **Masquée cadre** | **99.68 %** | **27 (10 FP, 17 FN)** | 0.037 |

- Détection du cadre : **93 %**. Les 7 % d'échecs sont à **85 % des cases
  pleines** (ratio 0.81 — trivialement cochées, sans impact). Seules ~91 cases
  vides échouent vraiment.
- Le masquage **divise les faux négatifs par ~3** (48 → 17 ; FN = case cochée
  lue vide = l'étudiant perd un point mérité, l'erreur la plus injuste).

### Constat décisif sur le flagging
Les 27 erreurs restantes sont **concentrées sur les 8 questions à choix multiple
(Q1–Q8) et la grille de code**. Les questions à choix unique sont quasi sans
erreur. Ces erreurs sont **« confiantes »** (loin du seuil) — un score de
confiance mono-feature ne les rattrape pas :
- proximité du seuil ±0.08 → 7/27 ; cohérence QCM (`single`≠1) → 0/27 (les
  erreurs ne sont pas sur des `single`) ; désaccord ancienne/masquée → 6/27.

**Conclusion : le flagging fiable = convergence d'estimateurs INDÉPENDANTS.**
Plus on a d'estimateurs indépendants, plus le « désaccord » couvre d'erreurs.
C'est l'objet de ce plan.

---

## 3. Objectif (Levier 2)

Construire un flagging des cases douteuses qui **converge plusieurs estimateurs
indépendants** et signale toute case où ils divergent ou sont incertains. Cela
exige d'abord d'**intégrer la détection masquée** (un estimateur propre + base
d'un GBM ré-entraîné dont la `predict_proba` devient exploitable).

Cible : qu'un correcteur se fiant aux flags rate ≲ 10 cases / 9071 (vs ~30 — ~24
silencieuses — aujourd'hui), **sans** dégrader la précision (≥ 99.89 % après
ré-entraînement).

---

## 4. Étapes

### Étape 1 — Module de détection masquée
Créer `auto_grading/masked_detect.py` en portant, **proprement**, depuis
`proto_mask_benchmark.py` :
- `render_reference()` — rendu du PDF sujet, **mis en cache** (mtime du PDF).
  Source : `<amc_dir>/DOC-sujet.pdf`, sinon `sujet/DOC-sujet.pdf`.
- `detect_frame(warped, box)` → 4 coins du cadre, coords globales, ou None.
- `ref_frame_corners` — détecté une fois par case dans la référence (cache).
- `masked_ratio(warped, ref, box, ref_corners, scan_corners)` → fraction sombre
  relative hors masque.
- Repli quand `detect_frame` échoue : si la case est globalement très sombre →
  traiter comme pleine (mesure ≈ 1) ; sinon calage par translation seule
  (`compute_per_question_offsets`) et **flag « cadre non détecté »**.

⚠️ La détection masquée a besoin du **PDF du sujet compilé** (`sujet/exam.xy` est
déjà produit ; le PDF aussi via `compile_pdf()`). Pour EXAM_2026, utiliser
`EXAM_2026/DOC-sujet.pdf`.

### Étape 2 — Brancher dans `cv_grade.py` + ré-entraîner le GBM
- `extract_features()` : **ajouter** les features masquées (ne pas supprimer les
  anciennes — donner au GBM les deux jeux) : `masked_ratio`, `frame_detected`
  (0/1), `align_residual` (qualité du calage = MSE des 4 coins), et idéalement le
  masked ratio à 2-3 érosions différentes. Mettre à jour `FEATURE_COLS`.
- `build_dataset.py` : régénère `results/labeled_cells.parquet` avec les
  nouvelles features. (Labels inchangés : AMC `manual∈{0,1}` + copies `validated`.)
- `train_classifier.py --cv` : ré-entraîne `models/cell_clf_full.pkl`. Vérifier
  l'accuracy CV **par copie** (≥ celle d'aujourd'hui).
- `cv_benchmark.py` : re-valider sur les 9071 cases — viser ≥ 99.89 %.

### Étape 3 — Le flagging multi-estimateurs (cœur du levier 2)
Pour chaque case, calculer des estimateurs **indépendants** de l'état cochée/vide :
- **E1** masked_ratio vs son seuil adaptatif par-question.
- **E2** ancien `box_fill_ratio` vs son seuil adaptatif.
- **E3** décision finale du **GBM** ré-entraîné.
- **E4** `predict_proba` du GBM — incertitude continue.
- **E5** AMC `capture.sqlite` (`_amc_answers`) — **si présent** (examen analysé).
- **E6** contraintes structurelles : question `single` ⇒ exactement 1 cochée ;
  colonne du code ⇒ exactement 1 chiffre.

Une case est **flaggée « douteuse »** si **au moins un** :
- E1, E2, E3 ne sont pas tous d'accord (désaccord d'estimateurs) ;
- `predict_proba` ∈ [0.30, 0.70] (GBM incertain) ;
- E5 présent et en désaccord avec E3 (déjà fait : `cv_differs_amc`) ;
- E6 violée (la question entière est flaggée) ;
- cadre non détecté sur une case non-pleine.

Écrire ça dans `cv_grade.grade_image` → champ `_ambiguous_cells` **complet**
(⚠️ supprimer la troncature `ambiguous[:15]` dans `notes` ; et **ne plus
supprimer** `_ambiguous_cells` à l'écriture de `raw_responses_cv/`). Ajouter un
flag copie `low_confidence` + compter les cases douteuses.

### Étape 4 — Surfacer dans l'UI (`front/`)
- Les cases flaggées « douteuses » : surlignage (couleur distincte du jaune
  `cv_differs_amc` actuel — ex. orange). Voir `templates/_zoom_grid.html`,
  `server.py` `build_zoom_questions`/`diff_set`.
- L'onglet `/flagged` : ajouter un filtre « cases douteuses ».
- Dashboard : compteur de cases douteuses restantes à relire.

### Étape 5 — Re-valider de bout en bout
- `cv_benchmark.py` ≥ 99.89 %.
- Comparer flags vs erreurs : reproduire l'analyse de `proto_mask_benchmark.py`
  (section FLAGGING) avec les estimateurs réels → viser ≥ 80 % des erreurs
  flaggées. Comparer aussi aux **67 corrections de la review** de
  `../../auto_grading/raw_responses/` (`_cv_answers` vs `answers`).
- `score.py` auto-test vert ; rien d'écrit dans `EXAM_2026/` ni dans l'original.

---

## 5. Fichiers

**Nouveaux** : `auto_grading/masked_detect.py`.
**Modifiés** : `cv_grade.py` (features + flagging + grade_image), `build_dataset.py`
(nouvelles features), `front/server.py` + `front/templates/*` (surlignage), 
`front/seed_raw_responses.py` (préserver `_ambiguous_cells`), `CLAUDE.md`.
**Régénérés** : `models/cell_clf_full.pkl`, `results/labeled_cells.parquet`.
**Référence/spec** : `proto_mask_benchmark.py` (ne pas supprimer — banc d'essai).

---

## 6. Pièges

- **Ré-entraînement obligatoire** : changer les features invalide le GBM
  existant. Toujours `build_dataset` → `train_classifier` → `cv_benchmark`.
- **Ne jamais écraser** `raw_responses/<batch>/page_*.json` `answers` (relecture
  utilisateur). `cv_grade.py` n'écrit que dans `raw_responses_cv/`.
- **Non-régression** : `auto_grading/pages/batch2/` d'origine est *périmé* vs
  `batch2.pdf` courant ; valider sur calage/pages **identiques** (cf. memory
  `projet-modele-generalise`). EXAM_2026 sqlite en `mode=ro`.
- **Détection masquée sans cadre** (~7 %) : 85 % sont des cases pleines triviales
  → ne pas les flagger bêtement ; ne flagger que les cases non-pleines sans cadre.
- **Défaut connu du masquage** : une petite coche posée *sur* la lettre imprimée
  est masquée avec elle → FN. C'est pour ça qu'on garde les features de forme
  (densité de contours, composantes) et le GBM — elles repèrent la coche même
  quand `masked_ratio` est bas. Ne pas réduire le GBM au seul `masked_ratio`.
- **Examen sans AMC** (`capture.sqlite` absent) : E5 indisponible → le flagging
  repose sur E1–E4 + E6 ; c'est le mode normal d'un nouvel examen.
- **Grille de code** : cellules plus petites → `detect_frame` moins fiable ;
  ajuster les tolérances de taille par case (déjà relatif à `bw,bh`).

---

## 7. Ordre conseillé
1. `masked_detect.py` + vérifier `masked_ratio` sur quelques cases (visuel).
2. Features dans `cv_grade.py` + `build_dataset.py`.
3. Ré-entraîner + `cv_benchmark` (verrou : ≥ 99.89 %).
4. Flagging multi-estimateurs dans `grade_image`.
5. UI.
6. Re-validation complète (étape 5).
