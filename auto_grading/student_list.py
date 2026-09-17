"""Liste des étudiants : chargement du fichier, et rattachement d'une copie.

Deux responsabilités, volontairement séparées :

  - **charger** la liste (`load_students`, `analyze_roster`) depuis un xlsx ou
    un csv dont on ne présume ni l'ordre des colonnes ni la position de
    l'en-tête ;
  - **rattacher** une copie à un étudiant (`StudentMatcher`), par le numéro lu
    sur la grille ou, à défaut, par le nom manuscrit.

⚠ **Rien n'est deviné en silence.** Une colonne configurée qui a disparu du
fichier lève `RosterError` au lieu de retomber sur la première colonne venue :
le repli positionnel produisait des étudiants dont l'identifiant valait
« DUPONT », sans le moindre message, et l'interface annonçait « ✓ 2 étudiants ».
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from config import load_config, resolve_path
from grade_imports import (_col_label, _first_run, jsonable_cell, list_sheets,
                           read_table)

ROOT = Path(__file__).resolve().parent

# Un numéro plus court que ça ne discrimine rien dans une promo de 200.
MIN_ID_WIDTH = 3


class RosterError(Exception):
    """La liste est inexploitable — message destiné à l'utilisateur."""


@dataclass(frozen=True)
class Student:
    id: str      # identifiant complet, tel qu'il figure dans la liste
    nom: str
    prenom: str
    # Courriel institutionnel, quand la liste en porte un. ⚠ Facultatif et
    # jamais deviné : `""` si la colonne n'est pas configurée. Il ne sert pas au
    # rattachement des copies (rien ne l'écrit sur une feuille) — seulement à
    # l'export, pour pouvoir renvoyer les notes.
    email: str = ""

    @property
    def last4(self) -> str:
        return self.id[-4:]

    @property
    def full(self) -> str:
        return f"{self.nom} {self.prenom}"


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z]+", " ", s).strip().lower()
    return s


def _cell_text(v) -> str:
    """Cellule → texte, en retirant le `.0` des flottants Excel."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


# --------------------------------------------------------------------------
# Lecture du fichier
# --------------------------------------------------------------------------
def roster_columns(cfg: dict, rows: list[list]) -> tuple[int, int, int | None, int]:
    """(index id, index nom, index prénom | None, 1re ligne de données).

    Les index de la config priment. À défaut — config d'une version antérieure,
    qui ne stockait que des intitulés — les noms sont résolus contre la ligne
    juste au-dessus des données. **Un intitulé absent est une erreur**, jamais
    un repli sur la colonne 0.
    """
    ncol = max((len(r) for r in rows), default=0)
    start = int(cfg.get("xlsx_data_start", 1) or 0)
    if not (0 <= start < len(rows)):
        raise RosterError(
            f"la 1re ligne de données ({start}) est hors du fichier "
            f"({len(rows)} lignes)")

    def by_idx(key):
        try:
            v = int(cfg.get(key, -1))
        except (TypeError, ValueError):
            return -1
        return v if 0 <= v < ncol else -1

    i_id, i_nom, i_prenom = by_idx("xlsx_id_idx"), by_idx("xlsx_nom_idx"), by_idx("xlsx_prenom_idx")
    if i_id >= 0 and i_nom >= 0:
        return i_id, i_nom, (i_prenom if i_prenom >= 0 else None), start

    header = [_cell_text(c) for c in rows[max(0, start - 1)]]
    header += [""] * (ncol - len(header))

    def by_name(key, required=True):
        name = (cfg.get(key) or "").strip()
        if not name:
            if required:
                raise RosterError(f"aucune colonne choisie pour « {key} »")
            return None
        if name not in header:
            raise RosterError(
                f"la colonne « {name} » n'existe pas dans ce fichier. "
                f"Colonnes disponibles : {', '.join(h for h in header if h) or '(aucune)'}. "
                "Re-choisis les colonnes dans la modale « Liste étudiants ».")
        return header.index(name)

    return (by_name("xlsx_id_col"), by_name("xlsx_nom_col"),
            by_name("xlsx_prenom_col", required=False), start)


def mail_column(cfg: dict, ncol: int) -> int | None:
    """Index de la colonne courriel, ou `None`.

    Volontairement à part de `roster_columns` : le courriel est une colonne
    neuve, sans équivalent dans les configs d'avant (qui désignaient les
    colonnes par intitulé). Absent ou hors bornes = pas de courriel, jamais une
    erreur — une liste sans courriel reste une liste parfaitement valide.
    """
    try:
        v = int(cfg.get("xlsx_mail_idx", -1))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v < ncol else None


def load_students() -> list[Student]:
    """Charge la liste depuis le fichier + les colonnes de la config.

    Rend une liste vide si aucun fichier n'est configuré (projet vierge).
    Lève `RosterError` si un fichier est configuré mais inexploitable — c'est
    un état que l'utilisateur doit voir, pas une liste vide de plus.
    """
    cfg = load_config()
    raw = (cfg.get("student_xlsx") or "").strip()
    if not raw:
        return []
    path = resolve_path(raw)
    if not path.exists() or path.is_dir():
        raise RosterError(f"fichier introuvable : {path}")
    try:
        rows = read_table(path, cfg.get("xlsx_sheet") or None)
    except (OSError, ValueError) as e:
        raise RosterError(f"lecture impossible : {e}") from e
    if not rows:
        raise RosterError("fichier vide")
    i_id, i_nom, i_prenom, start = roster_columns(cfg, rows)
    ncol = max((len(r) for r in rows), default=0)
    return _students_from_rows(rows, i_id, i_nom, i_prenom, start,
                               mail_column(cfg, ncol))


def _students_from_rows(rows, i_id: int, i_nom: int,
                        i_prenom: int | None, start: int,
                        i_mail: int | None = None) -> list[Student]:
    def cell(row, i):
        return _cell_text(row[i]) if (i is not None and i < len(row)) else ""

    out = []
    for row in rows[start:]:
        sid = cell(row, i_id)
        if not sid:
            continue
        out.append(Student(id=sid, nom=cell(row, i_nom),
                           prenom=cell(row, i_prenom), email=cell(row, i_mail)))
    return _pad_leading_zeros(out)


def students_from_file(path, cols: dict) -> list[Student]:
    """Charge une liste avec un mapping de colonnes DONNÉ, sans toucher la config.

    Sert à contrôler un import avant de l'enregistrer : ce qui est annoncé à
    l'utilisateur est alors ce qui sera réellement chargé, et non un compte de
    lignes.

    ⚠ `cols["sheet"]` fait partie du mapping au même titre qu'un index de
    colonne : sur un classeur à plusieurs onglets, le contrôle doit porter sur
    celui que l'utilisateur a désigné, pas sur l'onglet actif du fichier.
    """
    try:
        rows = read_table(path, (cols.get("sheet") or None))
    except (OSError, ValueError) as e:
        raise RosterError(str(e)) from e
    if not rows:
        raise RosterError("fichier vide")
    ncol = max(len(r) for r in rows)
    i_id, i_nom = int(cols.get("id_idx", -1)), int(cols.get("nom_idx", -1))
    i_prenom = int(cols.get("prenom_idx", -1))
    i_mail = int(cols.get("mail_idx", -1))
    start = int(cols.get("data_start", 1))
    if not (0 <= i_id < ncol):
        raise RosterError("choisis la colonne du numéro étudiant")
    if not (0 <= i_nom < ncol):
        raise RosterError("choisis la colonne du nom")
    if not (0 <= start < len(rows)):
        raise RosterError(f"1re ligne de données hors du fichier ({len(rows)} lignes)")
    return _students_from_rows(rows, i_id, i_nom,
                               i_prenom if 0 <= i_prenom < ncol else None, start,
                               i_mail if 0 <= i_mail < ncol else None)


def roster_report(students: list[Student], id_width: int | None = None,
                  cols: dict | None = None) -> dict:
    """Ce qu'il faut savoir AVANT d'enregistrer une liste.

    Le compte affiché doit décrire des étudiants exploitables, pas des lignes
    lues : c'est ce qui manquait pour qu'un import raté puisse encore afficher
    « ✓ 3 étudiants » alors qu'un des trois était la ligne d'en-tête.
    """
    ids = [s.id for s in students]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    non_digit = [s.id for s in students if not s.id.isdigit()]
    empty_names = sum(1 for s in students if not s.nom.strip())
    widths = sorted({len(s.id) for s in students if s.id.isdigit()})
    problems = []
    if cols and cols.get("id_idx") == cols.get("nom_idx"):
        problems.append("la même colonne est choisie pour le numéro et pour le nom")
    if dup:
        problems.append(f"{len(dup)} identifiant(s) en double : {', '.join(dup[:5])}"
                        + (" …" if len(dup) > 5 else ""))
    if non_digit:
        problems.append(
            f"{len(non_digit)} identifiant(s) ne sont pas des nombres "
            f"(ex. {', '.join(repr(x) for x in non_digit[:3])}) — "
            "la colonne du numéro est-elle la bonne ?")
    if empty_names:
        problems.append(f"{empty_names} ligne(s) sans nom")
    if len(widths) > 1:
        problems.append(f"largeurs d'identifiant hétérogènes : {widths}")
    if id_width and widths and id_width > max(widths):
        problems.append(
            f"la grille du sujet a {id_width} chiffres alors que les numéros en "
            f"font {max(widths)} — les copies ne pourront pas être rattachées.")
    return {"n_students": len(students), "n_dup": len(dup),
            "n_empty_names": empty_names, "widths": widths,
            "problems": problems,
            "sample": [{"id": s.id, "nom": s.nom, "prenom": s.prenom}
                       for s in students[:3]]}


def _pad_leading_zeros(students: list[Student]) -> list[Student]:
    """Restaure les zéros de tête perdus par les cellules numériques du xlsx.

    Les identifiants d'un établissement sont de largeur fixe. Si une nette
    majorité (≥ 80 %) des ids numériques fait L caractères, ceux qui sont plus
    courts ont perdu leurs zéros de tête au stockage → on les complète. Sans ça,
    `by_full_id` (donc les `_student_override` posés à la relecture) ne matche
    plus, et le suffixe lu sur la grille est décalé.
    """
    digits = [s.id for s in students if s.id.isdigit()]
    if len(digits) < 2:
        return students
    widths: dict[int, int] = {}
    for d in digits:
        widths[len(d)] = widths.get(len(d), 0) + 1
    width, count = max(widths.items(), key=lambda kv: kv[1])
    if count / len(digits) < 0.8:
        return students        # largeurs hétérogènes : on ne devine rien
    return [
        replace(s, id=s.id.zfill(width))
        if (s.id.isdigit() and len(s.id) < width) else s
        for s in students
    ]


# --------------------------------------------------------------------------
# Analyse d'un fichier de liste (pré-remplissage du formulaire d'import)
# --------------------------------------------------------------------------
def _profile(rows: list[list], ncol: int) -> list[dict]:
    """Par colonne : lignes numériques (avec leur largeur), lignes alphabétiques.

    ⚠ Une adresse de courriel sans chiffre (`jean.dupont@ensai.fr`) passe le
    test « alphabétique » et concourait donc comme colonne de nom. Sur un export
    où le courriel s'intercale entre le numéro et le nom, c'est LUI qui était
    proposé comme nom de famille — plausible dans l'aperçu, faux. Une colonne
    dont les cellules contiennent « @ » est comptée à part.
    """
    prof = []
    for ci in range(ncol):
        digit_rows, alpha_rows, widths, upper, at = [], [], {}, 0, 0
        for ri, r in enumerate(rows):
            t = _cell_text(r[ci]) if ci < len(r) else ""
            if not t:
                continue
            if t.isdigit():
                digit_rows.append(ri)
                widths[len(t)] = widths.get(len(t), 0) + 1
            elif re.fullmatch(r"[^\W\d_][^\d]*", t, flags=re.UNICODE):
                if "@" in t:
                    at += 1
                    continue
                alpha_rows.append(ri)
                if t == t.upper():
                    upper += 1
        modal_w = max(widths.items(), key=lambda kv: kv[1])[0] if widths else 0
        prof.append({
            "digit_rows": [ri for ri in digit_rows
                           if len(_cell_text(rows[ri][ci])) == modal_w],
            "alpha_rows": alpha_rows, "modal_width": modal_w,
            "n_email": at,
            "upper_ratio": upper / len(alpha_rows) if alpha_rows else 0.0,
        })
    return prof


def sheet_summaries(path, max_sheets: int = 20) -> list[dict]:
    """Un récapitulatif par onglet : `{name, nrow, ncol, n_students}`.

    ⚠ C'est ce qui rend le choix d'onglet utilisable. Une liste de noms bruts
    (« Feuil1 », « resultat - 2026-09-14T100003.33 », « Reg-EN », « Reg-FR »)
    ne dit pas lequel porte une liste d'étudiants, ni combien : le classeur
    d'où vient ce besoin en a cinq, dont trois exploitables et deux qui
    décrivent des promotions différentes.

    `n_students` vaut `None` si l'onglet n'a pas été compris — mieux vaut ne
    rien annoncer qu'annoncer un compte faux. Au-delà de `max_sheets` onglets,
    on rend les noms seuls : le récapitulatif relit le classeur une fois par
    onglet, et ce n'est pas un prix à payer sans limite.
    """
    names = list_sheets(path)
    out = []
    for i, name in enumerate(names):
        info = {"name": name, "nrow": None, "ncol": None, "n_students": None}
        if i < max_sheets:
            try:
                a = analyze_roster(path, name)
                info.update(nrow=a["nrow"], ncol=a["ncol"])
                info["n_students"] = len(
                    _students_from_rows_safe(path, name, a["suggested"]))
            except Exception:                            # noqa: BLE001
                pass                                     # onglet illisible
        out.append(info)
    return out


def _students_from_rows_safe(path, sheet, suggested) -> list:
    """Étudiants qu'un onglet rendrait avec les colonnes DÉDUITES pour lui."""
    try:
        return students_from_file(path, {**suggested, "sheet": sheet})
    except RosterError:
        return []


def analyze_roster(path, sheet: str | None = None) -> dict:
    """Devine la structure d'un fichier de liste, pour pré-remplir le formulaire.

    ⚠ La détection est **par contenu**, pas par confrontation à la liste des
    étudiants : c'est justement cette liste qu'on est en train de charger.
    (`grade_imports.analyze_table`, qui sert aux fichiers de notes, reconnaît
    les identifiants en les cherchant dans le roster — circulaire ici.)

    Règles, dans l'ordre : une colonne d'identifiants est faite de nombres de
    largeur constante ; les lignes de données commencent au premier bloc de
    trois lignes consécutives où cette colonne est remplie ; les colonnes de
    noms sont alphabétiques — une colonne de courriels n'en est pas une, cf.
    `_profile` ; entre deux, celle qui est le plus en MAJUSCULES est le nom de
    famille — et à défaut, celle de gauche.
    """
    try:
        rows = read_table(path, sheet)
    except (OSError, ValueError) as e:
        raise RosterError(str(e)) from e
    ncol = max((len(r) for r in rows), default=0)
    if ncol == 0 or not rows:
        raise RosterError("onglet vide" if sheet else "fichier vide")
    prof = _profile(rows, ncol)

    id_scores = [(len(p["digit_rows"]) if p["modal_width"] >= MIN_ID_WIDTH else 0)
                 for p in prof]
    id_idx = max(range(ncol), key=lambda c: id_scores[c])
    if id_scores[id_idx] < 2:
        id_idx = -1
    data_start = _first_run(prof[id_idx]["digit_rows"]) if id_idx >= 0 else 1

    # La colonne de courriels, elle, se reconnaît sans ambiguïté : c'est celle
    # qui porte des « @ ». Pas de repli si aucune n'en a — une liste sans
    # courriel est une liste valide, et deviner ici remplirait l'export de
    # colonnes fausses.
    mail_scores = [prof[c]["n_email"] for c in range(ncol)]
    mail_idx = max(range(ncol), key=lambda c: mail_scores[c]) if ncol else -1
    if mail_idx < 0 or mail_scores[mail_idx] < 2:
        mail_idx = -1

    name_cols = sorted((c for c in range(ncol)
                        if c != id_idx and len(prof[c]["alpha_rows"]) >= 2),
                       key=lambda c: -len(prof[c]["alpha_rows"]))[:2]
    nom_idx = prenom_idx = -1
    if len(name_cols) >= 2:
        a, b = sorted(name_cols)
        # Le nom de famille est le plus souvent en capitales ; sinon, à gauche.
        if prof[b]["upper_ratio"] > prof[a]["upper_ratio"] + 0.3:
            nom_idx, prenom_idx = b, a
        else:
            nom_idx, prenom_idx = a, b
    elif name_cols:
        nom_idx = name_cols[0]

    columns = []
    for ci in range(ncol):
        sample = []
        for r in rows[data_start:]:
            t = jsonable_cell(r[ci]) if ci < len(r) else None
            if t not in (None, ""):
                sample.append(t)
            if len(sample) >= 3:
                break
        columns.append({
            "idx": ci,
            "label": _col_label(rows, ci, data_start),
            "sample": sample,
            "looks_id": ci == id_idx,
            "looks_name": ci in (nom_idx, prenom_idx),
            "looks_mail": ci == mail_idx,
        })
    return {
        "nrow": len(rows), "ncol": ncol, "columns": columns,
        "sheet": sheet,
        "preview": [[jsonable_cell(c) for c in (list(r) + [None] * (ncol - len(r)))]
                    for r in rows[:8]],
        "suggested": {"id_idx": id_idx, "nom_idx": nom_idx,
                      "prenom_idx": prenom_idx, "mail_idx": mail_idx,
                      "data_start": data_start},
    }


# --------------------------------------------------------------------------
# Rattachement d'une copie à un étudiant
# --------------------------------------------------------------------------
class StudentMatcher:
    def __init__(self, students: list | None = None) -> None:
        """`students` fourni = liste explicite, sans lire le projet actif.

        C'est ce qui permet de rattacher des notes importées à une population
        qui n'est pas celle d'un projet — la réunion des étudiants de plusieurs
        examens, par exemple. Sans argument, le comportement est inchangé : la
        liste du projet actif.
        """
        self.error = ""
        if students is not None:
            self.students = list(students)
        else:
            try:
                self.students = load_students()
            except RosterError as e:
                # Ne jamais lever ici : toutes les pages construisent un
                # matcher. L'erreur est portée par l'objet et affichée là où
                # elle se voit.
                self.students, self.error = [], str(e)
        self._by_id = {s.id: s for s in self.students}
        self._by_suffix: dict[int, dict[str, Student | None]] = {}
        # Index de noms normalisés pour le fuzzy. Deux homonymes partagent la
        # même clé : le second écrasait le premier sans un mot.
        self._norm_to_student: dict[str, Student] = {}
        self.homonyms: list[str] = []
        for s in self.students:
            key = _norm(s.full)
            if key in self._norm_to_student:
                self.homonyms.append(s.full)
                continue
            self._norm_to_student[key] = s
        self._norm_keys = list(self._norm_to_student.keys())

    # -- index par suffixe -------------------------------------------------
    def _suffix_index(self, n: int) -> dict[str, Student | None]:
        """{n derniers chiffres → étudiant}, `None` si plusieurs le partagent.

        ⚠ Une valeur ambiguë rend `None` plutôt que le premier arrivé :
        attribuer une copie au hasard entre deux étudiants est pire que ne pas
        l'attribuer, et la copie non résolue remonte dans `/identites`.
        """
        idx = self._by_suffix.get(n)
        if idx is not None:
            return idx
        groups: dict[str, list[Student]] = {}
        for s in self.students:
            if len(s.id) >= n and s.id[-n:].isdigit():
                groups.setdefault(s.id[-n:], []).append(s)
        idx = {k: (v[0] if len(v) == 1 else None) for k, v in groups.items()}
        self._by_suffix[n] = idx
        return idx

    def collisions(self, width: int) -> dict[str, list[Student]]:
        """Groupes d'étudiants que `width` chiffres ne suffisent pas à séparer."""
        groups: dict[str, list[Student]] = {}
        for s in self.students:
            if len(s.id) >= width and s.id[-width:].isdigit():
                groups.setdefault(s.id[-width:], []).append(s)
        return {k: v for k, v in groups.items() if len(v) > 1}

    def warnings(self, id_width: int | None = None) -> list[str]:
        """Ce qui rend un rattachement ambigu — à afficher, pas à imprimer.

        ⚠ La version précédente n'annonçait les homonymes que sur `stdout` :
        personne ne les voyait.
        """
        out = []
        if self.error:
            out.append(self.error)
        if self.homonyms:
            uniq = sorted(set(self.homonyms))
            out.append(f"{len(uniq)} homonyme(s) — le rattachement par le nom est "
                       f"ambigu pour : {', '.join(uniq[:5])}"
                       + (" …" if len(uniq) > 5 else ""))
        if id_width:
            coll = self.collisions(id_width)
            if coll:
                ex = "; ".join(f"{k} → {', '.join(s.full for s in v)}"
                               for k, v in list(coll.items())[:3])
                out.append(
                    f"{len(coll)} groupe(s) d'étudiants partagent les {id_width} "
                    f"derniers chiffres : {ex}"
                    + (" …" if len(coll) > 3 else "")
                    + f". Ces copies ne seront pas rattachées automatiquement — "
                      f"élargir la grille du numéro à plus de {id_width} chiffres "
                      f"les sépare.")
        return out

    # -- rattachement ------------------------------------------------------
    def by_full_id(self, sid: str) -> Student | None:
        """Match par identifiant complet (overrides utilisateur, jointures)."""
        return self._by_id.get(str(sid))

    def by_id(self, id_read: str) -> Student | None:
        """Match par le numéro LU sur la grille, quelle que soit sa largeur.

        ⚠ La version précédente exigeait exactement 4 chiffres alors que la
        grille est réglable de 1 à 9 : une grille à 5 chiffres — le choix
        naturel quand les numéros en font 5 — ne rattachait plus **aucune**
        copie, alors que le numéro complet était lu correctement.
        """
        sid = (id_read or "").strip()
        if not sid or not sid.isdigit():
            return None
        for cand in (sid, sid.lstrip("0")):
            if not cand:
                continue
            s = self._by_id.get(cand)
            if s is not None:
                return s
            if len(cand) >= MIN_ID_WIDTH:
                s = self._suffix_index(len(cand)).get(cand)
                if s is not None:
                    return s
        return None

    def by_name(self, raw_name: str, cutoff: float = 0.7) -> tuple[Student | None, float]:
        """Fuzzy match d'un nom manuscrit contre la liste. Retourne (match, score)."""
        q = _norm(raw_name)
        if not q:
            return None, 0.0
        matches = difflib.get_close_matches(q, self._norm_keys, n=1, cutoff=cutoff)
        if not matches:
            return None, 0.0
        m = matches[0]
        score = difflib.SequenceMatcher(None, q, m).ratio()
        return self._norm_to_student[m], score

    def candidates(self, raw_name: str, n: int = 5,
                   cutoff: float = 0.4) -> list[tuple[Student, float]]:
        """Top-n étudiants les plus proches d'un nom (pour résolution manuelle)."""
        q = _norm(raw_name)
        if not q:
            return []
        out = []
        for m in difflib.get_close_matches(q, self._norm_keys, n=n, cutoff=cutoff):
            score = difflib.SequenceMatcher(None, q, m).ratio()
            out.append((self._norm_to_student[m], score))
        return out

    def resolve(self, id_read: str, raw_name: str) -> dict:
        """Numéro lu d'abord, nom manuscrit ensuite, sinon rien.

        `method` distingue les deux réussites (`id` / `name_fuzzy`) : elles
        n'ont pas la même force, et l'interface doit le dire.
        """
        s = self.by_id(id_read)
        if s is not None:
            return {"matched": s, "method": "id", "score": 1.0, "flag": ""}
        s, score = self.by_name(raw_name)
        if s is not None:
            return {"matched": s, "method": "name_fuzzy", "score": score,
                    "flag": f"numéro '{id_read}' non rattaché, repli sur le nom (score={score:.2f})"}
        return {"matched": None, "method": "none", "score": 0.0,
                "flag": f"AUCUN MATCH (id={id_read!r}, name={raw_name!r})"}
