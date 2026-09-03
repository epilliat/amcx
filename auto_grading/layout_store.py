"""layout_store.py — géométrie canonique des cases (calage AMC), sans le logiciel AMC.

Pour corriger une feuille de réponses, le pipeline a besoin de la position de chaque
case à 300 dpi. AMC fournit ça dans `layout.sqlite` — mais ce fichier n'est que
l'import (par l'outil AMC `meptex`) du fichier **`.xy`** que `pdflatex` produit
lui-même en compilant le `.tex` au format AMC.

Ce module reconstruit la géométrie **sans dépendre du logiciel AMC** :

- `parse_xy(path)`     — réimplémente `meptex` (réf. /usr/lib/AMC/perl/AMC-meptex.pl) ;
- `parse_sqlite(path)` — lit un `layout.sqlite` existant (lecture seule) ;
- `get_layout()`       — résout la source par précédence (config `amc_dir`) + cache.

Précédence de `get_layout()` :
  1. `<amc_dir>/data/layout.sqlite`   (examen déjà préparé par AMC)
  2. `<amc_dir>/*.xy`                 (calage AMC fourni)
  3. `sujet/exam.xy`                  (produit par la compilation du sujet)
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# SUJET_DIR vit dans le projet actif (cf. config.project_root()).
# Calculé à l'import — un changement de projet implique un restart du process.
from config import project_root as _project_root  # noqa: E402

ROOT = _project_root()
SUJET_DIR = ROOT / "sujet"

DPI = 300  # résolution canonique du pipeline (= défaut de meptex)

# Unités TeX → pouces (1 pouce vaut tant d'unités). cf. AMC-meptex.pl %u_in_one_inch.
_UNITS_PER_INCH = {"in": 1.0, "cm": 2.54, "mm": 25.4, "pt": 72.27, "sp": 65536 * 72.27}

# code AMC du type de case → role (cf. AMC::DataModule::layout : ANSWER=1, QUESTIONONLY=2)
_ROLE = {"case": 1, "casequestion": 2, "scorequestion": 3, "score": 4,
         "qtext": 102, "atext": 103}

ROLE_ANSWER = 1        # cases cochées par l'étudiant (feuille de réponses)
ROLE_QUESTIONONLY = 2  # cases imprimées avec l'énoncé (questionnaire)


# ==========================================================================
# Modèle de données
# ==========================================================================

@dataclass(frozen=True)
class Box:
    """Une case, en pixels 300 dpi (origine coin haut-gauche, y vers le bas)."""
    page: int
    role: int
    question: int
    answer: int
    char: str          # lettre/chiffre affiché ('A'-'L', '0'-'9') ; '' si inconnu
    xmin: float
    xmax: float
    ymin: float
    ymax: float


@dataclass(frozen=True)
class CodeBox:
    """Un bit du code d'identification imprimé en haut de chaque page.

    AMC dessine ce code avec `\\AMC@binaryCode` : chaque bit est une vraie case,
    **noircie à l'impression** si le bit vaut 1, vide sinon. Trois codes se
    suivent : `kind=1` numéro de copie, `kind=2` numéro de page, `kind=3`
    checksum. `rank` est l'ordre de dessin, de gauche à droite.

    Rien à voir avec les cases que l'étudiant coche : celles-ci sont imprimées
    et donc lisibles sans que personne ne remplisse quoi que ce soit.
    """
    page: int
    kind: int          # 1 = copie, 2 = page, 3 = checksum
    rank: int          # 1 = case la plus à gauche
    xmin: float
    xmax: float
    ymin: float
    ymax: float


@dataclass(frozen=True)
class Zone:
    """Une zone nommée (ex. champ nom manuscrit `__n`)."""
    page: int
    zone: str
    xmin: float
    xmax: float
    ymin: float
    ymax: float


@dataclass(frozen=True)
class PageInfo:
    page: int
    width: float           # px
    height: float          # px
    mark_diameter: float   # px
    mires: tuple           # 4 (x,y) [TL, TR, BR, BL] ; () si absentes
    checksum: int = 0      # 3e champ de `\\page{copie/page/checksum}`


@dataclass
class Layout:
    """Géométrie complète d'un sujet (toutes pages) pour **une copie donnée**.

    Pour les sujets randomisés multi-copies (`\\exemplaire{N>1}`), chaque copie
    a son propre `Layout` (mêmes positions de cases, mais `Box.char` permuté
    selon le seed). Le numéro de copie est dans `copy` (1 pour les sujets
    non randomisés ou la copie #1).
    """
    dpi: int
    pages: dict                       # n° de page → PageInfo
    boxes: list                       # toutes les Box
    zones: list                       # toutes les Zone
    answer_sheet_page: int            # page de la feuille de réponses (cases role=1)
    question_names: dict = field(default_factory=dict)
    source: str = ""
    copy: int = 1                     # numéro de copie (1..N)
    code_boxes: list = field(default_factory=list)   # bits du code imprimé
    # Triplets (copie, page, checksum) de TOUTES les copies du sujet. Sert à
    # valider un code décodé : un triplet absent de cette liste est une lecture
    # fausse, pas une copie inconnue.
    page_ids: tuple = ()

    # ---- accès à la feuille de réponses ----------------------------------
    def sheet_boxes(self, role: int | None = ROLE_ANSWER) -> list:
        """Cases de la feuille de réponses, triées (question, answer)."""
        bs = [b for b in self.boxes
              if b.page == self.answer_sheet_page and (role is None or b.role == role)]
        return sorted(bs, key=lambda b: (b.question, b.answer))

    def code_boxes_on_page(self, page: int) -> list:
        """Bits du code imprimé de cette page, triés (kind, rang)."""
        return sorted([c for c in self.code_boxes if c.page == page],
                      key=lambda c: (c.kind, c.rank))

    def boxes_on_page(self, page: int, role: int | None = None) -> list:
        bs = [b for b in self.boxes
              if b.page == page and (role is None or b.role == role)]
        return sorted(bs, key=lambda b: (b.question, b.answer))

    @property
    def sheet(self) -> PageInfo:
        return self.pages[self.answer_sheet_page]

    @property
    def page_w(self) -> float:
        return self.sheet.width

    @property
    def page_h(self) -> float:
        return self.sheet.height

    @property
    def mark_diameter(self) -> float:
        return self.sheet.mark_diameter

    @property
    def mires(self) -> tuple:
        return self.sheet.mires

    @property
    def name_zone(self):
        """(xmin, xmax, ymin, ymax) du champ nom `__n` de la feuille, ou None."""
        for z in self.zones:
            if z.page == self.answer_sheet_page and z.zone == "__n":
                return (z.xmin, z.xmax, z.ymin, z.ymax)
        return None

    def _question_name(self, q: int) -> str:
        return (self.question_names.get(q) or "").strip()

    def copy_id_columns(self) -> list[int]:
        """Numéros des colonnes (au sens `question` AMC) formant la grille
        `\\AMCcode{copie}{N}` — vide si le sujet est `\\exemplaire{1}`."""
        return sorted(q for q in self.question_names
                      if self._question_name(q).startswith("copie["))

    def student_id_columns(self) -> list[int]:
        """Colonnes du code étudiant `\\AMCcodeGridInt{etu}{N}`."""
        return sorted(q for q in self.question_names
                      if self._question_name(q).startswith("etu["))

    def qcm_questions(self) -> list[int]:
        """Numéros des questions QCM (= ni grille copie, ni grille étudiant)."""
        copy = set(self.copy_id_columns())
        etu = set(self.student_id_columns())
        # union des questions présentes dans question_names et dans les boxes
        all_qs = set(self.question_names.keys()) | {b.question for b in self.boxes}
        return sorted(q for q in all_qs if q not in copy and q not in etu)


# ==========================================================================
# Parseur .xy — port fidèle de AMC-meptex.pl
# ==========================================================================

_DIM_RE = re.compile(r"^\s*([+-]?[0-9]*\.?[0-9]*)\s*([a-zA-Z]+)\s*$")
_PAGE_RE = re.compile(
    r"\\page\{([^}]+)\}\{([^}]+)\}\{([^}]+)\}(?:\{([^}]+)\}\{([^}]+)\})?")
_TRACEPOS_RE = re.compile(
    r"\\tracepos\{(.+?)\}\{([+-]?[0-9.]+[a-z]*)\}"
    r"\{([+-]?[0-9.]+[a-z]*)\}(?:\{([a-zA-Z]*)\})?\s*$")
_BOXCHAR_RE = re.compile(r"\\boxchar\{(.+)\}\{(.*)\}\s*$")
_QUESTION_RE = re.compile(r"\\question\{([0-9]+)\}\{(.*)\}\s*$")
_PREFIX_RE = re.compile(r"^[0-9]+/[0-9]+:")          # préfixe `élève/page:`
_MIRE_RE = re.compile(r"position[HB][GD]$")
_ZONE_RE = re.compile(r"^__zone:(.*):(.*)$")
_BOX_RE = re.compile(
    r"^(casequestion|case|scorequestion|score|qtext|atext)"
    r":(.*):([0-9]+),(-?[0-9]+)$")
_DIGIT_RE = re.compile(r"[1-9]")
# Bits du code d'identification imprimé : `chiffre:<kind>,<rang>`.
_CODE_RE = re.compile(r"^chiffre:([0-9]+),([0-9]+)$")


def _read_inches(dim: str) -> float:
    """Convertit une dimension TeX ('597.5pt', '0sp', …) en pouces."""
    m = _DIM_RE.match(dim or "")
    if not m:
        raise ValueError(f"dimension illisible: {dim!r}")
    num, unit = m.group(1), m.group(2)
    per = _UNITS_PER_INCH.get(unit)
    if per is None:
        raise ValueError(f"unité inconnue: {unit!r} ({dim!r})")
    return float(num or "0") / per


def _ajoute(arr: list, val: float) -> None:
    """Reproduit la fonction `ajoute` de meptex : accumule [min, max] dans `arr`
    en **ignorant les valeurs nulles** (mires/zones tracent 4 points dont chacun
    ne porte qu'une coordonnée non nulle)."""
    if not val:
        return
    if arr:
        if val < arr[0]:
            arr[0] = val
        if val > arr[1]:
            arr[1] = val
    else:
        arr.append(val)
        arr.append(val)


def _epc(page_id: str) -> tuple:
    """`'1/12/49'` → (élève=1, page=12, checksum=49)."""
    parts = (page_id or "").split("/")

    def _int(s, default):
        s = s.strip() if s else ""
        return int(s) if s.lstrip("-").isdigit() else default

    student = _int(parts[0] if len(parts) > 0 else "", 1)
    page = _int(parts[1] if len(parts) > 1 else "", 1)
    checksum = _int(parts[2] if len(parts) > 2 else "", 0)
    return student, page, checksum


def _bbox(box: dict):
    """(xmin, xmax, ymin, ymax) à partir des corners convertis, ou None."""
    bx, by = box["bx"], box["by"]
    if len(bx) < 2 or len(by) < 2:
        return None
    # après le retournement Y : by[0] = ymax (grand), by[1] = ymin (petit)
    return (bx[0], bx[1], by[1], by[0])


def parse_xy_all_copies(path) -> dict[int, Layout]:
    """Parse `.xy` → {copy_id: Layout} pour toutes les copies présentes.

    Pour un sujet `\\exemplaire{N}`, le `.xy` contient une entrée `\\page{c/p/cs}`
    par (copie, page) — on les regroupe par `c` et on construit un `Layout` par
    copie. Pour un sujet `\\exemplaire{1}` ou non-AMC : un seul Layout copie #1.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    raw_pages = []        # ordre du fichier (toutes copies confondues)
    cur = None
    question_names = {}

    for line in text.splitlines():
        m = _PAGE_RE.search(line)
        if m:
            pid, dx, dy, px, py = m.groups()
            if px is None or not _DIGIT_RE.search(px):
                px = dx
            if py is None or not _DIGIT_RE.search(py):
                py = dy
            cur = {"id": pid,
                   "dim_x": _read_inches(dx), "dim_y": _read_inches(dy),
                   "page_x": _read_inches(px), "page_y": _read_inches(py),
                   "cases": {}}
            raw_pages.append(cur)
            continue
        m = _TRACEPOS_RE.search(line)
        if m:
            if cur is None:
                continue
            key = _PREFIX_RE.sub("", m.group(1))
            box = cur["cases"].setdefault(key, {"bx": [], "by": [], "char": None})
            _ajoute(box["bx"], _read_inches(m.group(2)))
            _ajoute(box["by"], _read_inches(m.group(3)))
            continue
        m = _BOXCHAR_RE.search(line)
        if m:
            if cur is None:
                continue
            key = _PREFIX_RE.sub("", m.group(1))
            box = cur["cases"].setdefault(key, {"bx": [], "by": [], "char": None})
            box["char"] = m.group(2)
            continue
        m = _QUESTION_RE.match(line)
        if m:
            question_names[int(m.group(1))] = m.group(2)

    # Regroupement par copie (premier composant de `\page{c/p/cs}`)
    by_copy: dict[int, list] = {}
    for p in raw_pages:
        student, _, _ = _epc(p["id"])
        by_copy.setdefault(student, []).append(p)
    if not by_copy:
        return {}
    # Triplets de toutes les copies : c'est l'ensemble de validation d'un code
    # décodé, et on ne sait pas encore quelle copie on regarde.
    all_ids = tuple(sorted({_epc(p["id"]) for p in raw_pages}))
    return {c: _build_from_pages(pages, question_names, str(path), copy=c,
                                 page_ids=all_ids)
            for c, pages in by_copy.items()}


def parse_xy(path) -> Layout:
    """Parse un `.xy` et retourne le `Layout` de la copie #1 (rétrocompat).

    Pour accéder à d'autres copies, utiliser `parse_xy_all_copies(path)`.
    """
    layouts = parse_xy_all_copies(path)
    if not layouts:
        # fichier vide / sans `\page` : on retourne un Layout vide pour ne pas
        # casser le contrat des appelants (même comportement que l'historique).
        return _assemble(DPI, {}, [], [], {}, str(path), copy=1)
    return layouts.get(1) or next(iter(layouts.values()))


def _build_from_pages(raw_pages, question_names, source, copy=1,
                      page_ids=()) -> Layout:
    pages, boxes, zones, code_boxes = {}, [], [], []

    for p in raw_pages:
        _student, pageno, _checksum = _epc(p["id"])
        cases, page_y = p["cases"], p["page_y"]

        # pouces → pixels + retournement de l'axe Y (origine en haut)
        for box in cases.values():
            box["bx"] = [v * DPI for v in box["bx"]]
            box["by"] = [DPI * (page_y - v) for v in box["by"]]

        # diamètre des mires + positions
        diam, dn = 0.0, 0
        for key, box in cases.items():
            if _MIRE_RE.search(key):
                if len(box["bx"]) == 2:
                    diam += abs(box["bx"][1] - box["bx"][0]); dn += 1
                if len(box["by"]) == 2:
                    diam += abs(box["by"][1] - box["by"][0]); dn += 1
        mark_diameter = diam / dn if dn else 0.0

        mires = []
        for pos in ("HG", "HD", "BD", "BG"):       # TL, TR, BR, BL
            b = cases.get("position" + pos)
            if b and len(b["bx"]) == 2 and len(b["by"]) == 2:
                mires.append(((b["bx"][0] + b["bx"][1]) / 2,
                              (b["by"][0] + b["by"][1]) / 2))
        mires = tuple(mires) if len(mires) == 4 else ()

        pages[pageno] = PageInfo(page=pageno,
                                 width=DPI * p["dim_x"], height=DPI * p["dim_y"],
                                 mark_diameter=mark_diameter, mires=mires,
                                 checksum=_checksum)

        # meptex : une page sans aucune mire est ignorée pour les cases/zones
        if dn == 0:
            continue

        for key, box in cases.items():
            bb = _bbox(box)
            if bb is None:
                continue
            mz = _ZONE_RE.match(key)
            if mz:
                zones.append(Zone(page=pageno, zone=mz.group(2),
                                  xmin=bb[0], xmax=bb[1], ymin=bb[2], ymax=bb[3]))
                continue
            mb = _BOX_RE.match(key)
            if mb:
                boxes.append(Box(page=pageno, role=_ROLE.get(mb.group(1), 1),
                                 question=int(mb.group(3)), answer=int(mb.group(4)),
                                 char=box["char"] or "",
                                 xmin=bb[0], xmax=bb[1], ymin=bb[2], ymax=bb[3]))
                continue
            mc = _CODE_RE.match(key)
            if mc:
                code_boxes.append(CodeBox(page=pageno, kind=int(mc.group(1)),
                                          rank=int(mc.group(2)),
                                          xmin=bb[0], xmax=bb[1],
                                          ymin=bb[2], ymax=bb[3]))

    lay = _assemble(DPI, pages, boxes, zones, question_names, source, copy=copy)
    lay.code_boxes = code_boxes
    lay.page_ids = tuple(page_ids)
    return lay


# ==========================================================================
# Lecteur layout.sqlite
# ==========================================================================

def parse_sqlite_all_copies(path) -> dict[int, Layout]:
    """Lit un `layout.sqlite` AMC (lecture seule) → {copy_id: Layout}.

    AMC stocke une ligne par (student, page, ...) dans chaque table ; on les
    regroupe par `student` pour produire un Layout par copie.
    """
    path = Path(path)
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        pages_raw = list(con.execute(
            "SELECT student, page, dpi, width, height, markdiameter "
            "FROM layout_page"))
        boxes_raw = list(con.execute(
            "SELECT student, page, role, question, answer, xmin, xmax, ymin, ymax, "
            "char FROM layout_box"))
        zones_raw = list(con.execute(
            "SELECT student, page, zone, xmin, xmax, ymin, ymax FROM layout_zone"))
        marks_raw = list(con.execute(
            "SELECT student, page, corner, x, y FROM layout_mark"))
        try:
            question_names = {q: n for q, n in con.execute(
                "SELECT question, name FROM layout_question")}
        except sqlite3.Error:
            question_names = {}
        # Bits du code imprimé en haut de page : AMC les range dans
        # `layout_digit` (numberid = kind, digitid = rang) et le checksum de
        # chaque page dans `layout_page`. Sans eux, `decode_page_code` ne
        # peut rien lire sur un examen préparé par AMC.
        try:
            digits_raw = list(con.execute(
                "SELECT student, page, numberid, digitid, xmin, xmax, ymin, ymax "
                "FROM layout_digit"))
        except sqlite3.Error:
            digits_raw = []
        try:
            checksums = {(s, pg): int(c or 0) for s, pg, c in con.execute(
                "SELECT student, page, checksum FROM layout_page")}
        except sqlite3.Error:
            checksums = {}
    finally:
        con.close()

    by_copy: dict[int, dict] = {}
    for s, pg, pdpi, w, h, md in pages_raw:
        by_copy.setdefault(s, {"pages_raw": [], "boxes": [], "zones": [],
                               "marks": {}})["pages_raw"].append(
            (pg, pdpi, w, h, md))
    for s, pg, role, q, a, xn, xx, yn, yx, ch in boxes_raw:
        by_copy.setdefault(s, {"pages_raw": [], "boxes": [], "zones": [],
                               "marks": {}})["boxes"].append(
            Box(page=pg, role=role, question=q, answer=a, char=ch or "",
                xmin=xn, xmax=xx, ymin=yn, ymax=yx))
    for s, pg, zn, xn, xx, yn, yx in zones_raw:
        by_copy.setdefault(s, {"pages_raw": [], "boxes": [], "zones": [],
                               "marks": {}})["zones"].append(
            Zone(page=pg, zone=zn, xmin=xn, xmax=xx, ymin=yn, ymax=yx))
    for s, pg, corner, x, y in marks_raw:
        d = by_copy.setdefault(s, {"pages_raw": [], "boxes": [], "zones": [],
                                   "marks": {}})
        d["marks"].setdefault(pg, {})[corner] = (x, y)

    if not by_copy:
        return {}

    code_by_copy: dict[int, list] = {}
    for s, pg, kind, rank, xn, xx, yn, yx in digits_raw:
        code_by_copy.setdefault(s, []).append(
            CodeBox(page=pg, kind=int(kind), rank=int(rank),
                    xmin=xn, xmax=xx, ymin=yn, ymax=yx))
    # Triplets valides de TOUTES les copies : c'est contre eux qu'un code lu
    # est validé (cf. cv_grade.decode_page_code).
    page_ids = tuple(sorted((s, pg, c) for (s, pg), c in checksums.items()))

    out: dict[int, Layout] = {}
    for s, d in by_copy.items():
        dpi = DPI
        pages = {}
        for pg, pdpi, w, h, md in d["pages_raw"]:
            dpi = int(pdpi or DPI)
            cm = d["marks"].get(pg, {})
            mires = (tuple(cm[c] for c in (1, 2, 3, 4))
                     if all(c in cm for c in (1, 2, 3, 4)) else ())
            pages[pg] = PageInfo(page=pg, width=w, height=h,
                                 mark_diameter=md or 0.0, mires=mires,
                                 checksum=checksums.get((s, pg), 0))
        lay = _assemble(dpi, pages, d["boxes"], d["zones"],
                        question_names, str(path), copy=s)
        lay.code_boxes = code_by_copy.get(s, [])
        lay.page_ids = page_ids
        out[s] = lay
    return out


def parse_sqlite(path) -> Layout:
    """Lit un `layout.sqlite` AMC (rétrocompat : retourne la copie #1)."""
    layouts = parse_sqlite_all_copies(path)
    if not layouts:
        return _assemble(DPI, {}, [], [], {}, str(path), copy=1)
    return layouts.get(1) or next(iter(layouts.values()))


# ==========================================================================
# Assemblage + résolution de la source
# ==========================================================================

def _assemble(dpi, pages, boxes, zones, question_names, source, copy=1) -> Layout:
    # feuille de réponses = page portant les cases role=1 (la plus fournie)
    role1 = {}
    for b in boxes:
        if b.role == ROLE_ANSWER:
            role1[b.page] = role1.get(b.page, 0) + 1
    if role1:
        answer_sheet_page = max(role1, key=role1.get)
    elif pages:
        answer_sheet_page = max(pages)
    else:
        answer_sheet_page = 0
    return Layout(dpi=dpi, pages=pages, boxes=boxes, zones=zones,
                  answer_sheet_page=answer_sheet_page,
                  question_names=question_names, source=source, copy=copy)


# Cache à deux niveaux :
#   _cache["all_key"] : (kind, str(path), mtime) → cache de la dernière source résolue
#   _cache["by_copy"][copy] : Layout par copie
_cache = {"all_key": None, "by_copy": {}, "available": ()}


def _resolve_source():
    """(kind, path) de la meilleure source de layout disponible, ou None."""
    import config
    amc = config.amc_dir()
    sqlite_path = amc / "data" / "layout.sqlite"
    if sqlite_path.is_file():
        return ("sqlite", sqlite_path)
    if amc.is_dir():
        xys = sorted(amc.glob("*.xy"))
        calage = [p for p in xys if "calage" in p.name.lower()]
        if calage:
            return ("xy", calage[0])
        if xys:
            return ("xy", xys[0])
    exam_xy = SUJET_DIR / "exam.xy"
    if exam_xy.is_file():
        return ("xy", exam_xy)
    return None


def _load_all_copies():
    """Recharge toutes les copies de la source résolue (avec cache mtime)."""
    src = _resolve_source()
    if src is None:
        raise FileNotFoundError(
            "Aucune source de calage trouvée : ni <amc_dir>/data/layout.sqlite, "
            "ni <amc_dir>/*.xy, ni sujet/exam.xy. "
            "Compile le sujet (onglet Sujet) pour produire sujet/exam.xy.")
    kind, path = src
    key = (kind, str(path), path.stat().st_mtime)
    if _cache["all_key"] != key:
        layouts = (parse_sqlite_all_copies(path) if kind == "sqlite"
                   else parse_xy_all_copies(path))
        if not layouts:
            # source vide → on construit un Layout copie #1 vide pour tomber
            # sur le même message d'erreur explicite plus tard.
            layouts = {1: _assemble(DPI, {}, [], [], {}, str(path), copy=1)}
        try:
            import config
            forced = int(config.load_config().get("answer_sheet_page") or 0)
        except Exception:
            forced = 0
        if forced:
            for lay in layouts.values():
                lay.answer_sheet_page = forced
        _cache["all_key"] = key
        _cache["by_copy"] = layouts
        _cache["available"] = tuple(sorted(layouts.keys()))
    return _cache["by_copy"]


def get_layout(copy: int | None = None) -> Layout:
    """Layout d'une copie (défaut copie #1, rétrocompat).

    Pour les sujets non randomisés ou un projet legacy, `copy=1` est la seule
    copie présente — appelée même avec `copy=2`, on retombe sur la copie #1.
    """
    layouts = _load_all_copies()
    if copy is None:
        copy = 1
    lay = layouts.get(copy)
    if lay is not None:
        return lay
    # copie demandée absente → repli silencieux sur copie #1 (puis n'importe
    # laquelle), pour rester rétrocompat avec les appelants existants.
    return layouts.get(1) or next(iter(layouts.values()))


def get_available_copies() -> tuple[int, ...]:
    """Tuple trié des numéros de copies présentes dans la source courante."""
    _load_all_copies()
    return _cache["available"]


def invalidate_cache() -> None:
    """Force la relecture de la source au prochain `get_layout()`."""
    _cache["all_key"] = None
    _cache["by_copy"] = {}
    _cache["available"] = ()


if __name__ == "__main__":
    lay = get_layout()
    print(f"Source       : {lay.source}")
    print(f"Pages        : {sorted(lay.pages)}")
    print(f"Feuille rép. : page {lay.answer_sheet_page} "
          f"({lay.page_w:.0f}×{lay.page_h:.0f} px, {lay.dpi} dpi)")
    print(f"Mires        : {len(lay.mires)}  diamètre {lay.mark_diameter:.1f}")
    print(f"Champ nom    : {lay.name_zone}")
    sb = lay.sheet_boxes()
    print(f"Cases role=1 : {len(sb)}  (questions "
          f"{min((b.question for b in sb), default=0)}–"
          f"{max((b.question for b in sb), default=0)})")
