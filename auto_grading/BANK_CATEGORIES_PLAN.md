# Catégories hiérarchiques dans la banque de questions — plan de conception

Besoin : classer les questions d'une banque dans une **hiérarchie de catégories**
(cours « Régression linéaire » → chapitres « Inférence », « Validation », … →
sous-catégories), une question pouvant appartenir à **plusieurs** catégories,
y compris dans des branches différentes.

Statut : **plan entièrement livré** (étapes 0 à 5) — 154 tests Python,
25 contrôles SQL, 54 contrôles navigateur. Seule exception assumée : la vue
SQL `category_counts`, écartée faute de gain mesurable à cette échelle (§ 5).

## 0. Décisions

| Question | Décision | Pourquoi |
|---|---|---|
| À qui appartient l'arbre ? | **À la banque** — un arbre partagé par banque, pas d'arbre perso | Une banque V2 = un thème/une communauté (`config.banks`). L'arbre *est* la structure du cours. La vue perso existe déjà : `question_personal_tags`. |
| Qui peut l'éditer (online) ? | **Wiki-style** : tout utilisateur authentifié crée/renomme/déplace ; suppression d'un nœud non vide refusée. Audit `created_by`/`added_by`. Rôle admin réservé, hors scope. | Mode invite-only recommandé → petite communauté de confiance. ⚠ voir § 6.14 : c'est la décision la plus discutable. |
| Identité d'une catégorie | **UUID v4 stable**, dans les deux backends. Nom et chemin sont dérivés. | Renommer/déplacer ne touche jamais aux affectations. Un id local se réimporte tel quel en online sans table de correspondance. |
| Forme de l'arbre | **Liste plate `{id, parent_id, name, position}`**, pas de JSON imbriqué | Même forme que la table Postgres ; déplacer = 1 champ ; cycles détectables ; diff git lisible. |
| Stockage local de l'arbre | `<bank>/categories.json`, **à la racine**, jamais dans `questions/` | `_read_or_rebuild_index()` compte `questions/*.json` : un intrus force un rebuild à chaque lecture. |
| Affectations | Local : `categories: [uuid]` sur la question. Online : jonction `question_categories`. | Local : fichier autoportant. Online : `on delete cascade` — la RLS empêche A de nettoyer un `uuid[]` sur les questions de B. |
| Tags publics | **Conservés**, orthogonaux (facettes : `facile`, `L2`, `calcul`). Conversion opt-in seulement. | Une conversion automatique polluerait l'arbre avec des tags de difficulté. |
| Tags persos | **Inchangés** | C'est la réponse au besoin de vue perso, sans second arbre. |
| Filtre par sous-arbre | **Descendants inclus par défaut**, toggle `descendants=0`. Pseudo-filtre `uncategorized=1`. | Cliquer « Inférence » doit montrer tout le chapitre. « Sans catégorie » est indispensable pour trier l'existant. |
| Profondeur max | **4** (constante serveur) | Le besoin décrit 2-3 niveaux ; le panneau gauche (280 px) ne supporte pas plus d'indentation. |
| Suppression d'un nœud non vide | 409 par défaut ; `mode=reparent` remonte enfants et affectations au parent. **Jamais** de suppression de question. | Cohérent avec `DELETE /api/bank/<id>` qui n'affecte pas les sujets. |
| Ordre des enfants | Champ `position`, tri `(position, name)` | Un PATCH pour réordonner. |

## 1. Modèle de données

### 1.1 Local — `<bank>/categories.json`

```jsonc
{
  "version": 1,
  "modified_at": "2026-09-02T10:00:00",
  "nodes": [
    {"id": "6f1c…", "parent_id": null,    "name": "Inférence",   "position": 0, "created_at": "…", "modified_at": "…"},
    {"id": "a9b2…", "parent_id": "6f1c…", "name": "Tests",       "position": 0, "…": "…"},
    {"id": "c3d4…", "parent_id": "6f1c…", "name": "Intervalles", "position": 1, "…": "…"}
  ]
}
```

- Fichier absent = arbre vide, **sans écriture** : une banque locale sur un partage en lecture seule reste consultable.
- Écriture par `config.write_json_atomic`, sous `bank._lock`.
- Invariants validés à chaque écriture (module pur, § 3.1) : UUID valides, `parent_id` existant ou `null`, pas de cycle, profondeur ≤ 4, nom non vide ≤ 80, unicité insensible à la casse entre frères.

Question locale : ajout de `"categories": ["uuid", …]`. Absent = `[]`. Un id absent de l'arbre est **ignoré à la lecture** (jamais d'erreur) et purgé au prochain `save()`.

`index.json` : chaque entrée gagne `categories`, plus un `"index_version": 2` — rebuild automatique si la version lue diffère. C'est ce qui rend la migration d'index idempotente et sans intervention.

### 1.2 Online — `supabase/schema.sql`, section 8

```sql
create table if not exists bank_categories (
  id          uuid primary key default gen_random_uuid(),
  parent_id   uuid references bank_categories(id) on delete restrict,
  name        text not null check (char_length(btrim(name)) between 1 and 80),
  position    int  not null default 0,
  created_by  uuid references profiles(user_id) on delete set null,
  created_at  timestamptz default now(),
  modified_at timestamptz default now()
);
-- Unicité entre frères, racine incluse (NULL n'est pas distinct sous coalesce)
create unique index if not exists bank_categories_sibling_name_idx
  on bank_categories (coalesce(parent_id, '00000000-0000-0000-0000-000000000000'), lower(btrim(name)));
create index if not exists bank_categories_parent_idx on bank_categories (parent_id);

create table if not exists question_categories (
  question_id uuid references bank_questions(id)  on delete cascade,
  category_id uuid references bank_categories(id) on delete cascade,
  added_by    uuid references profiles(user_id)   on delete set null,
  added_at    timestamptz default now(),
  primary key (question_id, category_id)
);
create index if not exists question_categories_category_idx on question_categories (category_id);
```

**Trigger `bank_categories_check_tree()`** (before insert or update) : refuse `parent_id = id`, refuse un cycle (CTE récursive remontant depuis `new.parent_id`), refuse une profondeur > 4. Seule garantie contre un client qui contournerait le serveur AMCx.

**RLS**
- `bank_categories` : select/insert/update/delete `using (auth.uid() is not null)`. Pas de `using (true)` — l'arbre est la table des matières du cours, inutile de l'exposer au rôle `anon`.
- `question_categories` :
  - select : `exists (select 1 from bank_questions q where q.id = question_id)` — la RLS de `bank_questions` s'applique dans la sous-requête, donc on ne voit une affectation que si on voit la question.
  - insert : `added_by = auth.uid()` + le même `exists`.
  - delete : `added_by = auth.uid() or exists (… and q.author_id = auth.uid())`.

**Comptages par nœud** : phase 1, `GET /rest/v1/question_categories?select=category_id` agrégé côté client (même pattern que `get_global_stats`). Optimisation ultérieure : vue `category_counts` en `security_invoker`. Ne pas utiliser les agrégats PostgREST (désactivés par défaut sur Supabase).

### 1.3 Opérations sensibles

| Opération | Local | Online |
|---|---|---|
| Renommer | PATCH `name` ; affectations intactes (id) | idem |
| Déplacer | PATCH `parent_id` après vérif cycle/profondeur | vérif client (message clair) **et** trigger (garantie) |
| Supprimer vide | retire le nœud | DELETE (FK `restrict` protège des enfants) |
| Supprimer non vide `mode=reparent` | sous `_lock` : enfants → grand-parent, balayage des questions portant l'id, **un seul** `rebuild_index()` | PATCH enfants, puis insert vers parent en `ignore-duplicates` + DELETE (cascade). ⚠ ne réaffecte que les lignes visibles/supprimables par l'utilisateur : les affectations d'autrui sont perdues par le cascade — à écrire dans la confirmation UI. |

## 2. Migration

**Local** — rien à réécrire : `categories.json` absent → arbre vide ; question sans clé → `[]` ; `index_version` absent → rebuild une fois. Rejouable.

**Online** — section 8 en `create table if not exists` / `drop policy if exists`, comme le reste du schéma. Aucune ligne existante touchée.

**`bank_migrate.py`** — étape « 1b/ catégories » : POST des nœuds **avec leur id local** (`ignore-duplicates`, parents avant enfants par tri topologique), puis `question_categories` via le mapping `{old_8hex: uuid}` déjà persisté. Idempotent par PK.

**Tags → catégories** : aucun automatisme. Route opt-in `POST /api/bank/categories/<id>/assign {tag: "proba"}` — affecte toutes les questions portant ce tag, les tags restent. Rejouable (union d'ensembles).

## 3. Serveur

### 3.1 Module pur `auto_grading/bank_taxonomy.py` (nouveau)

Pas d'I/O. `validate_nodes`, `children_of`, `descendants(id)`, `depth`, `path(id) -> [names]`, `would_create_cycle`, `sort_siblings`, `is_valid_cat_id` (regex UUID **stricte** — `bank.is_valid_bank_id` accepte 36 caractères quelconques de `[0-9a-fA-F-]`), `annotate(nodes, counts)` → ajoute `depth`, `path`, `n_direct`, `n_total`. Les deux backends s'appuient dessus ; les UIs reçoivent des nœuds annotés et restent bêtes.

### 3.2 API commune aux deux backends (contrat `_bank()`)

Mêmes signatures dans `bank.py` et `bank_online.py` :

- `list_categories() -> list[dict]` (annotés)
- `create_category(name, parent_id=None, position=None)`
- `update_category(cat_id, *, name=None, parent_id=…, position=None)` — sentinelle pour distinguer « ne pas toucher » de « mettre à la racine »
- `delete_category(cat_id, mode="refuse") -> {removed, reparented_children, reassigned_questions}`
- `get_question_categories(bank_id)` / `set_question_categories(bank_id, cat_ids)` (remplacement, comme `set_personal_tags`)
- `assign_category(cat_id, bank_ids, remove=False) -> int` (bulk)
- `list_questions(filters)` : nouveaux `category`, `descendants` (défaut True), `uncategorized` ; chaque item porte `categories`
- `from_block(..., categories=None)`

Online : ajouter `question_categories(category_id)` au `select` embarqué (`_SELECT_WITH_AUTHOR`) pour les affectations en une requête ; pour le **filtre**, réutiliser le pattern `restrict_ids` existant avec l'ensemble des descendants. `uncategorized` = complémentaire côté client (la liste est plafonnée à 500 de toute façon).

### 3.3 Routes (`server.py`, à côté de `/api/bank*`)

| Route | Corps / query | Réponse |
|---|---|---|
| `GET /api/bank/categories` | — | `{ok, nodes:[{id,parent_id,name,position,depth,path,n_direct,n_total}], max_depth, can_edit}` |
| `POST /api/bank/categories` | `{name, parent_id?, position?}` | `{ok, node}` |
| `PATCH /api/bank/categories/<id>` | `{name?, parent_id?, position?}` | `{ok, node}` ; 409 si cycle/profondeur/doublon |
| `DELETE /api/bank/categories/<id>?mode=refuse\|reparent` | — | 409 si non vide et `refuse` |
| `GET /api/bank/<bank_id>/categories` | — | `{ok, categories:[ids]}` |
| `PUT /api/bank/<bank_id>/categories` | `{categories:[ids]}` | `{ok, categories}` |
| `POST /api/bank/categories/<id>/assign` | `{bank_ids:[…]}` ou `{tag:"…"}`, `remove?` | `{ok, n}` |
| `GET /api/bank` | `+ category=&descendants=0|1&uncategorized=1` | inchangé + `categories` par item |
| `POST /api/bank` | `+ categories:[ids]` | inchangé |

Transverse :
- Valider **tous** les ids avant toute interpolation dans une URL PostgREST ou tout accès disque — même famille que le bug du `*` documenté dans `bank.py`.
- `can_edit` : local → `True` ; online → `logged_in`. Les catégories marchent dans les **deux** backends ; seul l'online non connecté renvoie 401.
- Ne **pas** router les affectations par `/api/bank/<id>/save-data` : cette route incrémente `version`. Classer n'est pas éditer.
- Anti-CSRF déjà couvert par `_same_origin_only`.

**Remplacer le double appel** de `api_bank_list` (`list_questions` appelé deux fois, dont une entière juste pour `all_tags` — en online, deux fetchs complets à chaque frappe) par `GET /api/bank/facets` → `{all_tags, nodes}`, chargé à l'ouverture et après chaque mutation d'arbre. Sinon l'arbre ajoute un troisième aller-retour par frappe.

## 4. UI

Composant partagé **`static/bank_tree.js`** (global `AMCxBankTree`, sur le modèle de `AMCxRender` / `AMCxBlockEditor`), utilisé par `/banque` et par les deux modales de `/sujet`. DOM par `createElement` + `textContent` exclusivement : les noms de catégories viennent de la banque partagée, donc entrée non fiable. Deux modes : `filter` (sélection unique + édition) et `pick` (cases à cocher).

**`/banque`, panneau gauche** — bloc « Catégories » repliable entre les filtres et le compteur : racine « Toutes », pseudo-nœud « Sans catégorie », puis l'arbre ; par ligne chevron, nom, `n_direct (n_total)`. Case « inclure les sous-catégories » (cochée par défaut). État déplié en `localStorage` par slug de banque. Boutons au survol (renommer inline, ajouter enfant, supprimer, monter/descendre), visibles seulement si `can_edit`.

**Liste** — sous le titre, première catégorie en chemin abrégé (`Inférence › Tests`) + `+2` si plusieurs ; hors filtre seulement (inutile quand on est déjà dans le nœud).

**Fiche** — rangée « Catégories » sous les tags publics : chips-chemin avec `×`, plus « + Ajouter » ouvrant l'arbre en mode `pick` → `PUT /api/bank/<id>/categories`. C'est aussi le **déplacement** : retirer une chip, en ajouter une. Le glisser-déposer liste → nœud est une phase ultérieure.

**Modale « Banque » de `/sujet`** — section « Catégories » au-dessus de « Tags publics », mode `filter` **sans** boutons d'édition : on n'édite pas l'arbre depuis un sujet.

**Modale « Sauver dans la banque »** — arbre compact en mode `pick`, replié sauf les nœuds pré-cochés ; pré-sélection = dernières catégories utilisées pour cette banque (`localStorage`), gain réel quand on sauve 10 questions du même chapitre. Bouton « + nouvelle sous-catégorie » inline pour ne pas devoir sortir vers `/banque`.

**Une question dans 3 catégories** — une chip-chemin par appartenance, triées par chemin, tooltip = chemin complet, libellé tronqué au dernier segment. Dans l'arbre, `n_total` compte une question **une seule fois** par nœud même si elle est affectée à deux descendants (ensemble, pas somme).

Ajouter au passage la facette « Tags publics » à `/banque` (elle n'existe que dans la modale de `/sujet`).

## 5. Ordre d'implémentation

**0 — Module pur** `bank_taxonomy.py`. ✅ **fait** (29 tests). Vérifiable par tests unitaires (cycle, profondeur, doublons, `descendants`, `annotate`). Le dépôt n'a **aucune** suite de tests : c'est l'occasion de poser `tests/`, isolable via `AMCX_BANK_DIR`.

**1 — Backend local + routes.** ✅ **fait** (45 + 27 tests). Vérifiable en `curl`, sans compte ni réseau : 3 nœuds, un déplacement, un cycle (409), une question dans 2 nœuds, filtre par chapitre avec/sans descendants, `uncategorized=1`, suppression non vide (409 puis `reparent`), et contrôle de `categories.json` + fichier question + un seul rebuild d'index.

**2 — Backend online.** ✅ **fait** (25 tests SQL + 40 tests client). Rejouer le schéma deux fois sans erreur ; mêmes scénarios curl ; avec deux comptes : B voit l'arbre de A, voit les affectations des questions `public` de A mais pas de ses brouillons, ne peut pas retirer une affectation posée par A sur la question de A ; `update bank_categories set parent_id = <descendant>` échoue en SQL direct.

**3 — UI `/banque`.** ✅ **fait** (24 contrôles navigateur). Navigation, comptages cohérents avec la liste, renommage sans perte d'affectation, chips + picker, état déplié persistant. Vérifier qu'un nom contenant `<script>` s'affiche en texte.

**4 — UI `/sujet`.** ✅ **fait** (21 contrôles navigateur). Sauver un bloc avec 2 catégories → visible sous les deux nœuds ; pré-sélection mémorisée au second enregistrement.

**5 — Outillage.** ✅ **fait**, sauf la vue `category_counts` (écartée).  `bank_migrate.py`, « catégorie depuis un tag », `GET /api/bank/facets`, puis glisser-déposer et vue `category_counts` si les volumes le justifient. Migrer deux fois → second passage 0 upload, ids identiques local/online.

## 6. Pièges et dettes relevés dans le code existant

1. **`bank_online._question_to_row`** : liste `skip` de clés non-colonnes. Ajouter `categories` **avant tout le reste**, sinon `save()` part en PGRST204.
2. **Filtres PostgREST non échappés** (`tags_quoted`, `mon_tag`) : interpolation de valeurs utilisateur sans échapper `"`, `,`, `)`. Pas une injection SQL, mais un filtre cassé ou élargi. Pour les catégories : n'interpoler que des UUID validés par regex stricte.
3. **`bank.save()` reconstruit l'index à chaque appel** : un bulk-assign de N questions = N scans complets. Ajouter `reindex=True` (ou un context manager de batch) ; `delete_category(mode=reparent)` en a besoin.
4. **Heuristique de resync de l'index** : compte `questions/*.json` — d'où l'obligation de mettre `categories.json` à la racine.
5. **Double `list_questions` dans `api_bank_list`** : ne pas empiler l'arbre dessus → route `facets`.
6. **`/api/bank/<id>/save-data` incrémente `version`** : ne pas y router les affectations.
7. **`banque.html` lit `cfg.bank_mode`**, clé V1 obsolète depuis le multi-banques : corrigé à l'exécution par `refreshAuthStatus()`, mais le premier rendu est faux.
8. **`question_freeform` absent des sélecteurs de type** des deux UIs alors que `from_block` l'accepte (par ailleurs désactivé à la création, cf. `DISABLED_KINDS`).
9. **Widgets Phase B dupliqués** entre `banque.html` et `sujet.html` (~150 lignes copiées). Ne pas ajouter une troisième copie : l'arbre vit dans `bank_tree.js` dès le départ.
10. **`banque.html` : `innerHTML` avec l'email non échappé** (vient de la config, risque faible) — contraire à la règle « pas d'innerHTML brut ».
11. **`bank.is_valid_bank_id`** accepte `[0-9a-fA-F-]{36}` : suffisant contre le glob, pas comme validation d'UUID.
12. **Aucun test dans le dépôt.**
13. **Tags persos inexistants en local** (routes Phase B → 400) : si un jour on veut un arbre perso, la brique de base manque en local.
15. **`AMCX_BANK_DIR` était sans effet** (découvert à l'étape 1) : le repli de
    `active_bank_cfg()` porte toujours un `path`, donc `bank_root()` n'atteignait
    jamais la branche env — la variable documentée ne servait à rien, et aucun
    test ne pouvait isoler une banque jetable. **Corrigé.**
16. **`new_project.py` place le sujet dans `<projet>/auto_grading/sujet/`** alors
    que `sujet_store.SUJET_DIR` vaut `<project_root>/sujet` : la racine de projet
    attendue est donc le sous-dossier `auto_grading/`, pas le dossier créé. Pas
    touché (hors scope), mais déroutant quand on monte un projet de test.
17. **Un refus RLS sur DELETE/UPDATE ne lève pas** (vérifié sur PG 16) : il
    filtre les lignes. Un test qui attend une exception passe donc à tort. Les
    contrôles RLS doivent compter les lignes touchées. C'est aussi ce qui rend
    `set_question_categories` fusionnant plutôt que remplaçant en ligne —
    corrigé en relisant l'état réel après écriture.
14. ⚠ **Wiki-style + cascade** : la décision « tout utilisateur authentifié édite l'arbre » et la perte des affectations d'autrui au `reparent` sont les deux points à retrancher si la banque online devient large. À reconsidérer avant d'ouvrir une banque au-delà d'une équipe de confiance.
