"""Le **dossier de travail** : l'arborescence sous laquelle vivent les projets.

Un dossier de travail contient les projets d'une même année, d'un même cours,
d'une même promo, et ce qui les accompagne (listes d'étudiants, scans en
attente, comptes rendus). L'onglet *Fichiers* le parcourt et le remanie.

⚠ **Deux mots, deux niveaux** — et le code porte les noms historiques :

| interface     | sur le disque   | dans le code                      |
|---------------|-----------------|-----------------------------------|
| une **évaluation** | `sujet/exam.tex` | `project` (`project_state`, `/api/projects`) |
| un **projet** | `cohorte.json`  | `cohort` (`cohort.py`, `/api/cohorte`) |

Un projet rassemble les évaluations d'une promotion ; c'est lui qui agrège les
notes. Renommer le code coûterait des centaines de points d'appel et tous les
`config.json` déjà écrits ; seuls les libellés ont bougé.

⚠ **La racine du dossier de travail EST le projet actif**, même pointeur
(`~/.config/amcx/active_cohort`). Deux racines distinctes — « mon dossier de
travail » ici, « mon projet » là — auraient fini par désigner deux endroits
différents. Le `cohorte.json` n'apparaît que le jour où l'on compose réellement
un projet : définir la racine n'écrit rien.

Sécurité — le serveur n'a **aucune authentification** et `--host` permet de
l'exposer. Ce module déplace, renomme et supprime des fichiers : c'est de loin
la route la plus dangereuse du projet. D'où, dans l'ordre :

⚠ **L'API ne parle qu'en chemins RELATIFS à la racine.** Un client ne peut même
pas *exprimer* un chemin extérieur. `resolve()` refuse tout ce qui en sort une
fois les liens symboliques suivis — sinon un lien posé dans le dossier de
travail donnerait accès au reste du disque.

⚠ **La racine elle-même reste bornée au dossier personnel**
(`project_state.check_under_browse_root`), comme le sélecteur de projet. La
définir sur `/` ouvrirait la machine entière.

⚠ **Supprimer, c'est mettre à la corbeille** (`.amcx-corbeille/` dans la
racine), jamais `rmtree`. Un dossier de projet porte des scans, des corrections
relues à la main et des notes : un clic de trop ne doit pas être définitif. Le
vidage de la corbeille est une action séparée, qui annonce ce qu'elle détruit.

⚠ **Un nom passe par `project_state.project_name_error`** : un dossier de
rangement obéit aux mêmes contraintes qu'un dossier de projet (il peut en
devenir un), donc `..`, les séparateurs, les noms réservés de Windows et le
point ou l'espace final sont refusés avant tout accès disque.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import cohort
import project_state

# Corbeille du dossier de travail. Le point de tête la range avec les fichiers
# cachés, donc hors des listings ordinaires : elle a son propre écran.
TRASH = ".amcx-corbeille"

# Au-delà, on cesse de descendre : un dossier de travail qui contient un dépôt
# git ou un `node_modules` ferait fondre le serveur sur un parcours complet.
MAX_ENTRIES = 4000


class WorkspaceError(Exception):
    """Ce qu'on refuse de faire, et pourquoi — message montré tel quel."""


# --------------------------------------------------------------------------
# Racine
# --------------------------------------------------------------------------

def root() -> Path | None:
    """Le dossier de travail actif, ou `None`. Même pointeur que `/cohorte`."""
    d = project_state.active_cohort()
    return d if (d and Path(d).is_dir()) else None


def set_root(path) -> Path:
    """Définit le dossier de travail. N'écrit **rien** dedans."""
    p = Path(str(path)).expanduser()
    try:
        resolved = project_state.check_under_browse_root(p)
    except (ValueError, PermissionError) as e:
        raise WorkspaceError(str(e)) from e
    except OSError as e:
        raise WorkspaceError(f"chemin illisible : {e}") from e
    if not resolved.is_dir():
        raise WorkspaceError(f"{resolved} n'est pas un dossier existant.")
    project_state.set_active_cohort(resolved)
    return resolved


def _need_root() -> Path:
    r = root()
    if r is None:
        raise WorkspaceError("Aucun dossier de travail défini.")
    return Path(r).resolve()


# --------------------------------------------------------------------------
# Résolution d'un chemin relatif
# --------------------------------------------------------------------------

def resolve(rel: str, *, must_exist: bool = True) -> Path:
    """Chemin absolu d'un `rel` **relatif à la racine** (`""` = la racine).

    ⚠ La vérification porte sur le chemin RÉSOLU, liens symboliques suivis :
    un lien déposé dans le dossier de travail donnerait sinon accès au reste
    du disque avec les droits de l'utilisateur.
    """
    r = _need_root()
    rel = str(rel or "").replace("\\", "/").strip("/")
    if rel in ("", "."):
        return r
    target = (r / rel)
    try:
        resolved = target.resolve()
    except OSError as e:
        raise WorkspaceError(f"chemin illisible : {e}") from e
    if resolved != r and r not in resolved.parents:
        raise WorkspaceError(f"hors du dossier de travail : {rel}")
    if must_exist and not resolved.exists():
        raise WorkspaceError(f"{rel} n'existe pas.")
    return resolved


def rel_of(path: Path) -> str:
    """Chemin relatif à la racine, en séparateurs `/` (`""` pour la racine)."""
    r = _need_root()
    try:
        out = Path(path).resolve().relative_to(r)
    except ValueError:
        raise WorkspaceError(f"hors du dossier de travail : {path}")
    return "" if str(out) == "." else out.as_posix()


def _check_name(name: str, what: str = "dossier") -> str:
    name = str(name or "").strip()
    err = project_state.project_name_error(name, what)
    if err:
        raise WorkspaceError(err)
    return name


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------

def is_project(d: Path) -> bool:
    """Un dossier de projet AMCx — lui-même ou via son `auto_grading/`.

    ⚠ `new_project.create_project()` rend `<dest>/auto_grading` : c'est LUI le
    projet, alors qu'on nomme « projet » le dossier parent. Les deux formes
    existent sur disque, on reconnaît les deux (même règle que
    `exam_results.resolve_project`).

    ⚠ Le `auto_grading/` d'un projet n'est PAS un projet de plus. Sans cette
    règle l'arbre affichait deux pastilles « projet actif » imbriquées, et il
    fallait deviner laquelle ouvrir.
    """
    d = Path(d)
    if d.name == "auto_grading" and project_state.is_valid_project(d):
        return False
    return (project_state.is_valid_project(d)
            or project_state.is_valid_project(d / "auto_grading"))


def project_root_of(d: Path) -> Path | None:
    """Le dossier à ouvrir pour ce projet (celui qui porte `sujet/exam.tex`)."""
    d = Path(d)
    for cand in (d / "auto_grading", d):
        if project_state.is_valid_project(cand):
            return cand
    return None


def is_cohort(d: Path) -> bool:
    """Un dossier de **projet** : il rassemble des évaluations (`cohorte.json`).

    ⚠ Ce n'est pas l'inverse de `is_project` — un dossier peut n'être ni l'un
    ni l'autre (un dossier de rangement), et la racine du dossier de travail se
    comporte en projet même sans le fichier : `cohort.load` traite un
    `cohorte.json` absent comme un projet vide. Le fichier n'est écrit qu'au
    premier réglage, c'est ce qui permet de poser la racine sur un dossier
    existant sans le transformer.
    """
    return cohort.is_cohort(Path(d))


def new_cohort(parent_rel: str, name: str) -> str:
    """Crée un sous-dossier et en fait un **projet**. Rend son chemin relatif.

    ⚠ Ne bascule PAS dessus. Ouvrir un projet re-enracine l'arbre : le faire
    d'office au moment de la création planterait l'utilisateur dans un dossier
    vide, alors qu'il vient le plus souvent de créer un rangement où déplacer
    des évaluations existantes. « Ouvrir ce projet » est une action à part.
    """
    rel = mkdir(parent_rel, name)
    d = resolve(rel)
    cohort.save(d, dict(cohort.DEFAULTS, name=d.name))
    return rel


def evaluations() -> list[dict]:
    """Les évaluations du dossier de travail, sur **deux** niveaux.

    Un dossier de travail est soit plat (une évaluation par sous-dossier), soit
    groupé (des projets, chacun portant ses évaluations) : n'en regarder qu'un
    seul viderait le menu de la topbar dans le second cas. On ne descend pas
    dans une évaluation — son `auto_grading/` n'en est pas une seconde.

    Rend `[{name, path, group}]`, `group` étant le dossier intermédiaire (`""`
    à la racine) : sans lui, deux évaluations homonymes de deux promotions
    s'affichent pareil.
    """
    r = root()
    if r is None:
        return []

    def scan(d: Path, group: str, depth: int) -> list[dict]:
        out = []
        try:
            kids = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return out
        for sub in kids[:MAX_ENTRIES]:
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if is_project(sub):
                out.append({"name": sub.name, "path": str(sub), "group": group})
            elif depth > 0:
                out.extend(scan(sub, sub.name, depth - 1))
        return out

    return scan(Path(r), "", 1)


def _entry(p: Path, rel_parent: str) -> dict:
    try:
        st = p.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    is_dir = p.is_dir()
    rel = f"{rel_parent}/{p.name}" if rel_parent else p.name
    return {
        "name":    p.name,
        "rel":     rel,
        "is_dir":  is_dir,
        "is_link": p.is_symlink(),
        "size":    0 if is_dir else size,
        "mtime":   mtime,
        "ext":     "" if is_dir else p.suffix.lower().lstrip("."),
        "project": bool(is_dir and is_project(p)),
        "cohort":  bool(is_dir and is_cohort(p)),
    }


def listdir(rel: str = "", *, hidden: bool = False) -> list[dict]:
    """Entrées directes d'un dossier : dossiers d'abord, puis par nom.

    Une entrée illisible est **ignorée** plutôt que de faire échouer le
    listing — un dossier sans droit de lecture ne doit pas rendre tout
    l'arbre inutilisable.
    """
    d = resolve(rel)
    if not d.is_dir():
        raise WorkspaceError(f"{rel or '.'} n'est pas un dossier.")
    out, n = [], 0
    try:
        entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        raise WorkspaceError(f"{rel or '.'} illisible : {e}") from e
    for p in entries:
        if p.name == TRASH:
            continue                      # la corbeille a son propre écran
        if p.name.startswith(".") and not hidden:
            continue
        n += 1
        if n > MAX_ENTRIES:
            break
        try:
            out.append(_entry(p, rel.strip("/")))
        except OSError:
            continue
    out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return out


def info(rel: str = "") -> dict:
    """Détail d'une entrée, enrichi pour un dossier de projet AMCx."""
    p = resolve(rel)
    rel = rel.strip("/")
    parent = Path(rel).parent.as_posix() if rel else ""
    d = _entry(p, "" if parent == "." else parent)
    d["rel"] = rel
    if not rel:
        d["name"] = p.name
    if p.is_dir():
        try:
            kids = [k for k in p.iterdir() if not k.name.startswith(".")]
            d["n_items"] = len(kids)
            d["n_dirs"] = sum(1 for k in kids if k.is_dir())
        except OSError:
            d["n_items"] = d["n_dirs"] = 0
        pr = project_root_of(p)
        if pr:
            d["project_root"] = str(pr)
        if d["cohort"]:
            d["cohort_root"] = str(p)
    return d


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------

def mkdir(parent_rel: str, name: str) -> str:
    d = resolve(parent_rel)
    name = _check_name(name)
    if not d.is_dir():
        raise WorkspaceError(f"{parent_rel or '.'} n'est pas un dossier.")
    target = d / name
    if target.exists():
        raise WorkspaceError(f"« {name} » existe déjà ici.")
    target.mkdir()
    return rel_of(target)


def rename(rel: str, name: str) -> str:
    """Renomme une entrée **sur place** (le nom seul, jamais un chemin)."""
    p = resolve(rel)
    name = _check_name(name, "dossier" if p.is_dir() else "fichier")
    if not rel.strip("/"):
        raise WorkspaceError("On ne renomme pas la racine depuis ici.")
    target = p.parent / name
    if target == p:
        return rel_of(p)
    if target.exists():
        raise WorkspaceError(f"« {name} » existe déjà ici.")
    p.rename(target)
    return rel_of(target)


def move(rel: str, dest_rel: str) -> str:
    """Déplace une entrée dans le dossier `dest_rel`.

    ⚠ Refuse de déplacer un dossier **dans lui-même ou dans l'un de ses
    descendants** : `shutil.move` y construirait une arborescence dont le
    parent est son propre enfant, sans message utilisable. Et refuse d'écraser
    une entrée existante : un déplacement ne doit jamais détruire en silence.
    """
    src = resolve(rel)
    dst_dir = resolve(dest_rel)
    if not rel.strip("/"):
        raise WorkspaceError("On ne déplace pas la racine.")
    if not dst_dir.is_dir():
        raise WorkspaceError("La destination n'est pas un dossier.")
    if src == dst_dir or src in dst_dir.parents:
        raise WorkspaceError(
            f"« {src.name} » ne peut pas être déplacé dans lui-même.")
    if src.parent == dst_dir:
        return rel_of(src)                        # déjà là : pas une erreur
    target = dst_dir / src.name
    if target.exists():
        raise WorkspaceError(f"« {src.name} » existe déjà dans la destination.")
    shutil.move(str(src), str(target))
    return rel_of(target)


def trash_dir() -> Path:
    return _need_root() / TRASH


def trash(rel: str) -> dict:
    """Met une entrée à la corbeille. **Rien n'est supprimé.**

    Le nom d'origine est préservé dans un sous-dossier horodaté : deux
    suppressions successives de `notes.csv` doivent pouvoir coexister, et il
    faut savoir d'où chaque chose vient pour la restaurer.
    """
    p = resolve(rel)
    if not rel.strip("/"):
        raise WorkspaceError("On ne supprime pas le dossier de travail lui-même.")
    box = trash_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slot = box / f"{stamp}-{os.urandom(2).hex()}"
    slot.mkdir(parents=True)
    (slot / "origine.txt").write_text(rel_of(p) + "\n", encoding="utf-8")
    shutil.move(str(p), str(slot / p.name))
    return {"slot": slot.name, "name": p.name, "from": rel_of(p.parent)}


def list_trash() -> list[dict]:
    box = trash_dir()
    if not box.is_dir():
        return []
    out = []
    for slot in sorted(box.iterdir(), reverse=True):
        if not slot.is_dir():
            continue
        origin = ""
        try:
            origin = (slot / "origine.txt").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        items = [k for k in slot.iterdir() if k.name != "origine.txt"]
        if not items:
            continue
        p = items[0]
        out.append({"slot": slot.name, "name": p.name, "origin": origin,
                    "is_dir": p.is_dir(),
                    "deleted_at": datetime.fromtimestamp(
                        slot.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def restore(slot: str) -> str:
    """Remet une entrée de la corbeille à sa place d'origine.

    Si la place d'origine est reprise, on restaure **à la racine** plutôt que
    d'écraser : une restauration ne doit jamais détruire ce qui est en place.
    """
    box = trash_dir()
    name = _check_name(slot)
    s = (box / name)
    if not s.is_dir() or s.resolve().parent != box.resolve():
        raise WorkspaceError("Entrée de corbeille inconnue.")
    items = [k for k in s.iterdir() if k.name != "origine.txt"]
    if not items:
        raise WorkspaceError("Entrée de corbeille vide.")
    p = items[0]
    origin = ""
    try:
        origin = (s / "origine.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    dest_dir = _need_root()
    if origin:
        parent_rel = str(Path(origin).parent)
        if parent_rel not in (".", ""):
            try:
                cand = resolve(parent_rel)
                if cand.is_dir():
                    dest_dir = cand
            except WorkspaceError:
                pass
    target = dest_dir / p.name
    if target.exists():
        target = _need_root() / p.name
    if target.exists():
        raise WorkspaceError(
            f"« {p.name} » existe déjà : renommez-le avant de restaurer.")
    shutil.move(str(p), str(target))
    shutil.rmtree(s, ignore_errors=True)
    return rel_of(target)


def trash_size() -> dict:
    """Ce que le vidage détruirait — annoncé avant de le faire."""
    box = trash_dir()
    n_files = n_bytes = 0
    if box.is_dir():
        for dirpath, _dirnames, filenames in os.walk(box):
            for f in filenames:
                n_files += 1
                try:
                    n_bytes += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return {"n_entries": len(list_trash()), "n_files": n_files,
            "n_bytes": n_bytes}


def empty_trash() -> dict:
    """Vide la corbeille. **C'est la seule fonction qui détruit vraiment.**

    Le dossier lui-même est retiré : une corbeille vide n'a pas à traîner dans
    le dossier de travail de quelqu'un. Elle est recréée à la suppression
    suivante.
    """
    before = trash_size()
    box = trash_dir()
    if box.is_dir():
        shutil.rmtree(box, ignore_errors=True)
    return before


# --------------------------------------------------------------------------
# Dépôt de fichiers
# --------------------------------------------------------------------------

def save_upload(dest_rel: str, filename: str, stream) -> str:
    """Écrit un fichier déposé dans `dest_rel`.

    ⚠ Le nom vient du navigateur : il passe par la **même** validation que
    tout autre nom (`project_name_error`), après avoir été réduit à son
    dernier segment. Un `../../.ssh/authorized_keys` n'a aucune chance
    d'arriver jusqu'ici — la racine est déjà bornée —, mais on ne se repose
    pas sur une seule barrière pour une écriture.

    ⚠ **On n'écrase jamais** : un fichier de même nom se voit suffixer `-2`,
    `-3`… Déposer un scan par-dessus un scan déjà corrigé serait irréparable.
    """
    d = resolve(dest_rel)
    if not d.is_dir():
        raise WorkspaceError("La destination n'est pas un dossier.")
    name = _check_name(Path(str(filename or "").replace("\\", "/")).name,
                       "fichier")
    target = d / name
    stem, suffix, k = target.stem, target.suffix, 2
    while target.exists():
        target = d / f"{stem}-{k}{suffix}"
        k += 1
    with open(target, "wb") as f:
        shutil.copyfileobj(stream, f)
    return rel_of(target)


# --------------------------------------------------------------------------
# Aperçu
# --------------------------------------------------------------------------

# Extensions dont le contenu se lit comme du texte. Tout le reste n'est pas
# deviné : afficher un binaire en « texte » remplit l'écran de caractères de
# contrôle et fait croire à un fichier corrompu.
TEXT_EXT = {"tex", "txt", "csv", "json", "md", "log", "xy", "py", "r", "sty",
            "cfg", "ini", "yml", "yaml", "tsv", "bib", "aux", "bak", "sql"}

PREVIEW_MAX = 200_000          # octets lus au plus — au-delà on tronque

# ⚠ **Liste blanche stricte des types servis EN LIGNE** (`inline`). Servir un
# fichier de l'utilisateur sur l'origine du serveur, c'est lui donner les
# droits de l'interface : un `.html` ou un `.svg` déposé dans le dossier de
# travail deviendrait du script exécuté sur `localhost:5050`. Ces trois-là ne
# portent pas de script exécutable par le navigateur, et la réponse part avec
# `X-Content-Type-Options: nosniff` pour que le type annoncé fasse foi. Tout
# le reste passe par le téléchargement, qui n'exécute rien.
INLINE_TYPES = {"pdf": "application/pdf", "png": "image/png",
                "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def preview(rel: str) -> dict:
    """Aperçu d'un fichier : texte tronqué, ou le type d'affichage en ligne."""
    p = resolve(rel)
    if not p.is_file():
        raise WorkspaceError("Ce n'est pas un fichier.")
    ext = p.suffix.lower().lstrip(".")
    size = p.stat().st_size
    if ext in INLINE_TYPES:
        return {"kind": "inline", "mime": INLINE_TYPES[ext], "size": size}
    if ext not in TEXT_EXT:
        return {"kind": "binary", "size": size}
    raw = p.read_bytes()[:PREVIEW_MAX]
    return {"kind": "text", "size": size,
            "truncated": size > PREVIEW_MAX,
            # `errors="replace"` : un fichier en latin-1 se lit quand même, en
            # montrant où ça coince, plutôt que de rendre une erreur opaque.
            "text": raw.decode("utf-8", errors="replace")}


def inline_type(rel: str) -> str:
    """Le type MIME sous lequel servir ce fichier en ligne, ou lève."""
    p = resolve(rel)
    ext = p.suffix.lower().lstrip(".")
    if not p.is_file() or ext not in INLINE_TYPES:
        raise WorkspaceError("Ce type de fichier ne s'affiche pas en ligne.")
    return INLINE_TYPES[ext]
