"""Migration de la banque locale (`~/Documents/AMCx-banque/`) vers Supabase.

CLI :
    python -m auto_grading.bank_migrate [--dry-run] [--also-patch-projects]

Étapes :
0. Monte l'arbre de catégories (`categories.json`) **en conservant les ids** :
   ils sont déjà des UUID v4, donc les affectations des questions migrées
   pointent sur les bons nœuds sans table de correspondance. Parents avant
   enfants (la FK l'exige).
1. Lit chaque `<bank_id>-<slug>.json` du dossier local.
2. Pour chaque question : POST sur Supabase (statut `draft`), puis ses
   affectations de catégories dans `question_categories`.
3. Pour chaque `stats.by_project.<proj>.*` : upsert dans `question_evals`.
4. Enregistre le mapping `{old_8hex_id: new_uuid}` dans
   `~/.config/amcx/bank_migration.json` (idempotent : déjà migré = skip).
5. Optionnellement (--also-patch-projects) : parcourt les projets connus via
   `project_state.recent_projects()`, recharge leur `sujet/exam.tex`, et pour
   chaque bloc dont `data._bank_id` est un ancien 8-hex présent dans le
   mapping, le remplace par le nouveau UUID.

Prérequis : l'user doit être connecté (bank_user_token posé dans config.json
du projet actif). Lancer après s'être connecté via Réglages → Banque.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import bank
import bank_online
import bank_taxonomy as tx
import config
import project_state


MAPPING_FILE = Path.home() / ".config" / "amcx" / "bank_migration.json"


def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        try:
            return json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_mapping(m: dict) -> None:
    MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def migrate_categories(dry_run: bool = False) -> dict:
    """Monte l'arbre local sur Supabase en conservant les identifiants.

    L'ordre préfixe d'`annotate` place chaque parent avant ses enfants, ce que
    la clé étrangère `parent_id` exige. Idempotent : un nœud déjà présent (même
    id) est ignoré côté serveur.

    Un conflit de nom entre sœurs (l'arbre en ligne contient déjà un chapitre
    du même nom, créé indépendamment) n'est PAS fusionné : il est signalé, à
    l'humain de trancher.
    """
    if not dry_run and not bank_online.is_logged_in():
        raise SystemExit("✘ Pas connecté.")
    nodes = bank.load_categories()
    if not nodes:
        return {"created": 0, "errors": []}
    created, errors = 0, []
    for n in tx.annotate(nodes):          # ordre préfixe : parents d'abord
        label = " › ".join(n["path"])
        print(f"  → catégorie {label}", end=" ")
        if dry_run:
            print("(dry-run)")
            continue
        try:
            bank_online.import_category(n)
            created += 1
            print("✓")
        except Exception as e:
            errors.append(f"{label} : {e}")
            print(f"✘ {e}")
    return {"created": created, "errors": errors}


def migrate_questions(dry_run: bool = False) -> dict:
    """Upload chaque question locale → Supabase. Retourne un récap."""
    if not bank_online.is_logged_in():
        raise SystemExit("✘ Pas connecté. Lance le serveur, va dans Réglages → Banque → Se connecter.")

    qdir = bank.question_dir()
    if not qdir.exists():
        return {"uploaded": 0, "skipped": 0, "evals": 0, "errors": []}

    mapping = load_mapping()
    uploaded, skipped, evals_total, cats_total = 0, 0, 0, 0
    errors: list[str] = []

    for jp in sorted(qdir.glob("*.json")):
        try:
            q = json.loads(jp.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{jp.name} : lecture {e}")
            continue
        old_id = q.get("bank_id", "")
        if not old_id:
            errors.append(f"{jp.name} : pas de bank_id")
            continue
        if old_id in mapping:
            skipped += 1
            continue

        # Strip bank_id pour forcer INSERT (online assigne un UUID frais)
        payload = dict(q)
        payload.pop("bank_id", None)
        payload.pop("stats", None)
        payload.setdefault("status", "draft")

        print(f"  → upload {old_id} ({q.get('title','')[:50]})", end=" ")
        if dry_run:
            print("(dry-run)")
            continue
        try:
            saved = bank_online.save(payload)
            new_id = saved["bank_id"]
            mapping[old_id] = new_id
            uploaded += 1
            print(f"→ {new_id}")
        except Exception as e:
            errors.append(f"{jp.name} : upload {e}")
            print(f"✘ {e}")
            continue

        # Affectations de catégories : les ids locaux valent en ligne (l'arbre
        # a été monté à l'identique par migrate_categories).
        cats = q.get("categories") or []
        if cats:
            try:
                cats_total += bank_online.import_assignments(new_id, cats)
            except Exception as e:
                errors.append(f"{old_id} : catégories {e}")

        # Évals : 1 ligne par projet
        by_p = ((q.get("stats") or {}).get("by_project") or {})
        for proj_name, ev in by_p.items():
            try:
                bank_online.update_project_stats(
                    new_id, proj_name,
                    n_eval=int(ev.get("n_eval", 0)),
                    sum_normalized=float(ev.get("sum_normalized", 0)),
                    n_perfect=int(ev.get("n_perfect", 0)),
                    max_score_at_sync=float(ev.get("max_score_at_sync") or 0),
                )
                evals_total += 1
            except Exception as e:
                errors.append(f"{old_id}/{proj_name} : eval {e}")

        if not dry_run:
            save_mapping(mapping)  # incremental save (interruption-safe)

    return {
        "uploaded": uploaded,
        "skipped":  skipped,
        "evals":    evals_total,
        "cats":     cats_total,
        "errors":   errors,
        "mapping_file": str(MAPPING_FILE) if not dry_run else "(dry-run)",
    }


def patch_projects(mapping: dict, dry_run: bool = False) -> dict:
    """Pour chaque projet connu, recharge sujet/exam.tex et remplace les
    `data._bank_id` qui sont d'anciens 8-hex présents dans `mapping`.
    """
    if not mapping:
        return {"projects_scanned": 0, "blocks_patched": 0}

    # On importe ici car sujet_store est lourd (parse complet).
    import sujet_store  # noqa

    projects = project_state.recent_projects() or []
    n_proj, n_blocks = 0, 0
    for proj in projects:
        proj_path = Path(proj.get("path") or "").expanduser()
        if not (proj_path / "sujet" / "exam.tex").exists():
            continue
        n_proj += 1
        # Force sujet_store sur ce projet via env var (rapide).
        import os
        old = os.environ.get("AMCX_PROJECT_DIR")
        os.environ["AMCX_PROJECT_DIR"] = str(proj_path)
        try:
            sub = sujet_store.parse_subject()
            if sub.get("mode") != "canonical":
                continue
            patched_here = 0
            for b in sub.get("blocks", []):
                old_id = (b.data or {}).get("_bank_id")
                if old_id and old_id in mapping:
                    if not dry_run:
                        b.data["_bank_id"] = mapping[old_id]
                        sujet_store.update_block(b.bid, b.data)
                    patched_here += 1
                    n_blocks += 1
            if patched_here:
                print(f"  ✓ {proj_path.name} : {patched_here} bloc(s) patchés")
        finally:
            if old is not None:
                os.environ["AMCX_PROJECT_DIR"] = old
            else:
                os.environ.pop("AMCX_PROJECT_DIR", None)

    return {"projects_scanned": n_proj, "blocks_patched": n_blocks}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Liste seulement, n'upload rien.")
    ap.add_argument("--also-patch-projects", action="store_true",
                    help="Après upload, patche les sujets locaux qui référencent "
                         "d'anciens bank_id (les remplace par le nouvel UUID).")
    args = ap.parse_args()

    print(f"AMCx — migration banque locale → online")
    print(f"  source : {bank.question_dir()}")
    print(f"  cible  : {config.active_bank_cfg().get('supabase_url')}")
    print()

    print("1/3 Montage de l'arbre de catégories…")
    r0 = migrate_categories(dry_run=args.dry_run)
    print(f"  → {r0['created']} catégorie(s)")
    if r0["errors"]:
        print(f"  ⚠ {len(r0['errors'])} conflit(s) — à trancher à la main :")
        for e in r0["errors"][:10]:
            print(f"    - {e}")

    print("\n2/3 Upload des questions…")
    r1 = migrate_questions(dry_run=args.dry_run)
    print(f"\n  → {r1['uploaded']} uploadée(s), {r1['skipped']} déjà migrée(s), "
          f"{r1['evals']} eval(s), {r1.get('cats', 0)} affectation(s) de catégorie")
    if r1["errors"]:
        print(f"  ⚠ {len(r1['errors'])} erreur(s) :")
        for e in r1["errors"][:10]:
            print(f"    - {e}")

    if args.also_patch_projects:
        print("\n3/3 Patch des sujets locaux…")
        mapping = load_mapping()
        r2 = patch_projects(mapping, dry_run=args.dry_run)
        print(f"  → {r2['projects_scanned']} projet(s) scanné(s), "
              f"{r2['blocks_patched']} bloc(s) patchés")

    print(f"\n✓ Mapping persisté : {r1['mapping_file']}")


if __name__ == "__main__":
    main()
