# archive/ — code hors service

Ces modules ne font plus partie du pipeline. Ils ne sont **pas** sur le
`sys.path` du code vivant : rien dans `auto_grading/` ne doit les importer.

| Module | Statut |
|---|---|
| `answer_key.py` | Ancienne clé de correction figée sur EXAM_2026. Servait de repli à `score.py` / `sujet_store.py` : dans un autre projet, elle produisait des scores faux et silencieux (question inconnue → barème d'un autre examen). Le repli a été supprimé — la source de vérité est `sujet/subject.json` via `sujet_store`. |
| `regen_answer_key.py` | Régénérait le fichier ci-dessus. Sans objet. |
| `grader.py`, `vision_prompt.py` | Voie Claude-vision abandonnée (extra `[api]`). `vision_prompt.SYSTEM_PROMPT` est écrit en dur pour EXAM_2026 (31 questions, ID à 4 chiffres). La correction se fait par OpenCV + GBM (`cv_grade.py`). |
| `prepare_to_review.py`, `import_reviewed.py`, `update_to_review_with_cv.py`, `build_index_md.py` | Ancien workflow de relecture par fichiers (`to_review/`), remplacé par l'UI Flask. ⚠ `import_reviewed.py` écrivait dans `raw_responses/` sans préserver la relecture utilisateur — ne pas le lancer. |

Pour en réutiliser un, il faut d'abord le rebrancher sur `sujet_store`
(questions, barème et options viennent du sujet du projet actif).
