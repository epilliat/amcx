"""Clé de correction — extraite d'AMC (data/scoring.sqlite + data/layout.sqlite).

⚠️ AMC RANDOMISE l'ordre des options : le 1er \\bonne dans LaTeX n'est PAS forcément A
   sur la feuille. La source de vérité pour les bonnes réponses = jointure des tables
   scoring_answer (correct=1) et layout_box (char par answer_idx).

Pour régénérer ce fichier après modification du LaTeX/AMC :
   python auto_grading/regen_answer_key.py
(le script lit les sqlite AMC et imprime ce fichier).

Format par question :
  type     : "mult" (questionmult) ou "single" (question)
  options  : chaîne des lettres autorisées (A, B, C, ...)
  correct  : chaîne des lettres correctes (sous-ensemble de options)
  Pour "mult" uniquement :
    b      : points par bonne case cochée
    m      : pénalité par mauvaise case cochée (valeur négative)

Single-choice: 1 pt si selected == correct, 0 sinon (pas de pénalité).
Mult: score question = somme(b si bonne et cochée, m si mauvaise et cochée) ;
      pas de plancher → une question peut être négative.
"""

ANSWER_KEY = {
    1:  {"type": "mult",   "options": "ABCD",         "correct": "AC",     "b": 1/2,  "m": -1/2,  "tag": "definition_stat"},
    2:  {"type": "mult",   "options": "ABCDE",        "correct": "AC",     "b": 1/2,  "m": -1/3,  "tag": "def_multiple"},
    3:  {"type": "mult",   "options": "ABCDE",        "correct": "BCE",    "b": 1/3,  "m": -1/3,  "tag": "zone_rejet"},
    4:  {"type": "mult",   "options": "ABCDEF",       "correct": "BCF",    "b": 1/3,  "m": -1/3,  "tag": "pvalue"},
    5:  {"type": "mult",   "options": "ABCDE",        "correct": "AB",     "b": 1/2,  "m": -1/3,  "tag": "stat_pivot_pvalue"},
    6:  {"type": "mult",   "options": "ABCDEFG",      "correct": "BCDF",   "b": 1/4,  "m": -1/3,  "tag": "neyman_pearson"},
    7:  {"type": "mult",   "options": "ABCDE",        "correct": "BDE",    "b": 1/3,  "m": -1/3,  "tag": "lois_usuelles"},
    # Q8 vaut 2 pts: 6 bonnes × 1/3 = 2
    8:  {"type": "mult",   "options": "ABCDEFGHIJKL", "correct": "ABCFGH", "b": 1/3,  "m": -1/3,  "tag": "intervalle2sigma"},
    9:  {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "fisher_variances"},
    10: {"type": "single", "options": "ABCD",         "correct": "B",                              "tag": "test_machines"},
    11: {"type": "single", "options": "ABCD",         "correct": "B",                              "tag": "test_fumeurs"},
    12: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "bonferroni"},
    13: {"type": "mult",   "options": "ABCDE",        "correct": "CE",     "b": 1/2,  "m": -1/3,  "tag": "binom_affirmations"},
    14: {"type": "mult",   "options": "ABCDEFG",      "correct": "CE",     "b": 1/2,  "m": -1/5,  "tag": "probleme"},
    15: {"type": "single", "options": "ABCDE",        "correct": "D",                              "tag": "binom_proba_n"},
    16: {"type": "single", "options": "ABCD",         "correct": "A",                              "tag": "binom_pvaleur"},
    17: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "binom_stat"},
    18: {"type": "single", "options": "ABCD",         "correct": "B",                              "tag": "binom_pvaleur_approx"},
    19: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "binom_conclusion_bilat"},
    20: {"type": "single", "options": "ABCD",         "correct": "B",                              "tag": "binom_unilat_majoritaire"},
    21: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "binom_unilat_minoritaire"},
    22: {"type": "mult",   "options": "ABCDEFGH",     "correct": "DFGH",   "b": 1/4,  "m": -1/4,  "tag": "condensateurs_modelisation"},
    23: {"type": "mult",   "options": "ABCDEF",       "correct": "BD",     "b": 1/2,  "m": -1/4,  "tag": "condensateurs_test"},
    24: {"type": "single", "options": "ABCDE",        "correct": "B",                              "tag": "chi2_probas"},
    25: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "chi2_effectifs"},
    26: {"type": "single", "options": "ABCD",         "correct": "C",                              "tag": "chi2_stat"},
    27: {"type": "single", "options": "ABCD",         "correct": "C",                              "tag": "chi2_loi"},
    28: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "chi2_calcul"},
    29: {"type": "single", "options": "ABCD",         "correct": "A",                              "tag": "chi2_conclusion"},
    30: {"type": "single", "options": "ABCD",         "correct": "B",                              "tag": "chi2_mle_mu"},
    31: {"type": "single", "options": "ABCD",         "correct": "D",                              "tag": "chi2_mle_mu_sigma"},
}


def max_score_per_question():
    """Score max théorique par question (sanity check)."""
    out = {}
    for q, d in ANSWER_KEY.items():
        if d["type"] == "single":
            out[q] = 1.0
        else:
            out[q] = round(len(d["correct"]) * d["b"], 6)
    return out


if __name__ == "__main__":
    maxes = max_score_per_question()
    total = sum(maxes.values())
    for q, m in maxes.items():
        print(f"Q{q:2d}: max = {m}  ({ANSWER_KEY[q]['tag']})")
    print(f"\nTOTAL QCM max: {total}")
