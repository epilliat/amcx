# auto_grading — correction QCM par vision multimodale

Pipeline Python qui lit les feuilles de réponses des 3 batches PDF
(`EXAM_2026/batch{1,2,3}.pdf`, 173 copies) avec Claude Opus 4.7 vision et
produit `results/students.csv` avec le score par question + total.

## Setup

```bash
# depuis qcm_tests/ (parent de auto_grading/)
python3 -m venv .venv
.venv/bin/pip install anthropic pdf2image pillow
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

```bash
cd auto_grading

# 1. extraire les pages en JPEG (~1.5 min)
../.venv/bin/python extract_pages.py

# 2. pilote sur quelques pages (~3 min, ~0.3$)
../.venv/bin/python batch_run.py --batches batch1 --pages 1-5

# 3. production complète (~5-30 min, ~20-40$)
../.venv/bin/python batch_run.py
# -> results/students.csv
```

## Architecture

| Fichier | Rôle |
|---|---|
| `answer_key.py` | Clé de correction extraite de `EXAM_2026/exam.tex` (31 Q, total 32 pts) |
| `score.py` | Application du barème AMC (single = 1/0 ; mult = somme avec plancher à 0) |
| `vision_prompt.py` | Prompt système strict + format JSON attendu |
| `grader.py` | Appel API Opus 4.7 (image base64 + prompt cached) + validation JSON |
| `extract_pages.py` | PDF → JPEG 200 dpi |
| `batch_run.py` | Orchestre 173 pages, idempotent (cache `raw_responses/`), écrit CSV |

## Idempotence

Chaque page traitée produit `raw_responses/<batch>/page_NNN.json`. Relancer
`batch_run.py` skip les pages déjà traitées. Pour forcer un re-grade: `--force`.

## Score d'une copie

- Single-choice (Q9-Q12, Q15-Q21, Q24-Q31) : 1 pt si la lettre cochée
  est exactement la bonne, 0 sinon (pas de pénalité).
- Multi-choice (Q1-Q8, Q13-Q14, Q22-Q23) : pour chaque case cochée,
  +b si bonne, +m si mauvaise (m ≤ 0). Score question = max(0, somme).
- Total = somme sur les 31 questions. Maximum = 32 (Q8 vaut 2 pts).
