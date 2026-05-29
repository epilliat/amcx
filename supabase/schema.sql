-- AMCx — Banque de questions en ligne (Supabase)
-- Phase A : tables + RLS pour parité fonctionnelle avec bank.py local.
-- Phase B (ratings, favoris, tags persos, vue agrégée) en bas, commentée.
--
-- À copier-coller dans : Supabase dashboard → SQL editor → "+ New query" → Run.
-- Idempotent : peut être re-run, ne perd aucune donnée existante.

-- ============================================================================
-- 0) (Optionnel) Whitelist email académique au signup
-- ============================================================================
-- DÉCOMMENTER + ÉDITER la regex pour restreindre l'inscription aux emails
-- académiques. Bloque les signups Gmail/Yahoo/mailinator/etc. Le filtre est
-- déclenché AVANT que la ligne n'arrive dans auth.users → l'user reçoit
-- l'erreur dans son client (AMCx affichera "Email académique requis").
--
-- create or replace function check_academic_email()
-- returns trigger language plpgsql security definer as $$
-- begin
--   -- À adapter : ajouter ou retirer des domaines selon ta communauté cible.
--   if new.email !~* '@(ensai\.fr|univ-rennes.+\.fr|inrae\.fr|cnrs\.fr|inria\.fr|.+\.ac-.+\.fr|.+\.edu)$' then
--     raise exception 'Email académique requis (.fr universitaire, .ac-XXX.fr, ou .edu).'
--                     using errcode = 'P0001';
--   end if;
--   return new;
-- end; $$;
--
-- drop trigger if exists enforce_academic_email on auth.users;
-- create trigger enforce_academic_email
--   before insert on auth.users
--   for each row execute function check_academic_email();

-- ============================================================================
-- 1) Profils utilisateurs (extension de auth.users)
-- ============================================================================

create table if not exists profiles (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  display_name text not null,
  institution  text,
  created_at   timestamptz default now()
);

-- Auto-création d'un profil au signup (trigger sur auth.users insert).
-- Le display_name part de la partie locale de l'email (avant @), modifiable
-- ensuite par l'user.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into profiles (user_id, display_name)
  values (new.id, split_part(new.email, '@', 1))
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ============================================================================
-- 2) Questions de la banque
-- ============================================================================

create table if not exists bank_questions (
  id             uuid primary key default gen_random_uuid(),
  author_id      uuid not null references profiles(user_id) on delete cascade,
  kind           text not null check (kind in ('question_qcm','question_open','question_freeform','text','answerbox')),
  data           jsonb not null,            -- Block.data verbatim (sans `bid` ni `_bank_id`)
  title          text not null,
  tags           text[] not null default '{}', -- tags PUBLICS définis par l'auteur
  status         text not null default 'draft' check (status in ('draft','public','archived')),
  version        int not null default 1,
  source_project text,
  created_at     timestamptz default now(),
  modified_at    timestamptz default now()
);

create index if not exists bank_questions_status_idx  on bank_questions (status);
create index if not exists bank_questions_author_idx  on bank_questions (author_id);
create index if not exists bank_questions_tags_idx    on bank_questions using gin (tags);
create index if not exists bank_questions_search_idx  on bank_questions
  using gin (to_tsvector('french', title || ' ' || coalesce(data->>'statement','')));

-- Trigger update modified_at automatiquement
create or replace function touch_modified_at()
returns trigger
language plpgsql
as $$
begin
  new.modified_at = now();
  return new;
end;
$$;

drop trigger if exists bank_questions_touch on bank_questions;
create trigger bank_questions_touch
  before update on bank_questions
  for each row execute function touch_modified_at();

-- ============================================================================
-- 3) Évaluations : 1 ligne par (question, user, projet)
-- ============================================================================

create table if not exists question_evals (
  id                uuid primary key default gen_random_uuid(),
  question_id       uuid not null references bank_questions(id) on delete cascade,
  user_id           uuid not null references profiles(user_id) on delete cascade,
  project_name      text not null,
  n_eval            int not null default 0,
  sum_normalized    float not null default 0,
  n_perfect         int not null default 0,
  max_score_at_sync float,
  last_sync         timestamptz default now(),
  unique (question_id, user_id, project_name)
);
create index if not exists question_evals_question_idx on question_evals (question_id);
create index if not exists question_evals_user_idx     on question_evals (user_id);

-- ============================================================================
-- 4) Row-Level Security (RLS)
-- ============================================================================

-- profiles : lecture publique du display_name, écriture seulement par soi-même
alter table profiles enable row level security;
drop policy if exists "profiles lecture publique" on profiles;
create policy "profiles lecture publique" on profiles for select using (true);
drop policy if exists "profiles auteur insert" on profiles;
create policy "profiles auteur insert" on profiles for insert with check (user_id = auth.uid());
drop policy if exists "profiles auteur update" on profiles;
create policy "profiles auteur update" on profiles for update using (user_id = auth.uid());

-- bank_questions : tous lisent les 'public' ; l'auteur lit/modifie ses propres lignes
alter table bank_questions enable row level security;
drop policy if exists "bank_questions lecture" on bank_questions;
create policy "bank_questions lecture" on bank_questions for select
  using (status = 'public' or author_id = auth.uid());
drop policy if exists "bank_questions insert" on bank_questions;
create policy "bank_questions insert" on bank_questions for insert
  with check (author_id = auth.uid());
drop policy if exists "bank_questions update" on bank_questions;
create policy "bank_questions update" on bank_questions for update
  using (author_id = auth.uid());
drop policy if exists "bank_questions delete" on bank_questions;
create policy "bank_questions delete" on bank_questions for delete
  using (author_id = auth.uid());

-- question_evals : chaque user ne voit/écrit que ses propres lignes
alter table question_evals enable row level security;
drop policy if exists "question_evals user own" on question_evals;
create policy "question_evals user own" on question_evals for all
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- ============================================================================
-- Phase B — ratings, favoris, commentaires, tags persos, stats agrégées
-- ============================================================================

-- 5) Ratings : 1 ligne par (question, user). Stars + favori + commentaire.
create table if not exists question_ratings (
  question_id  uuid references bank_questions(id) on delete cascade,
  user_id      uuid references profiles(user_id)  on delete cascade,
  stars        int check (stars between 1 and 5),
  favorite     bool not null default false,
  comment      text,
  created_at   timestamptz default now(),
  modified_at  timestamptz default now(),
  primary key (question_id, user_id)
);
create index if not exists question_ratings_question_idx on question_ratings (question_id);
create index if not exists question_ratings_user_idx     on question_ratings (user_id);
create index if not exists question_ratings_favorite_idx on question_ratings (user_id) where favorite;

drop trigger if exists question_ratings_touch on question_ratings;
create trigger question_ratings_touch
  before update on question_ratings
  for each row execute function touch_modified_at();

alter table question_ratings enable row level security;
-- TOUT le monde lit les ratings (pour aggréger avg_stars + n_favorites côté client)
drop policy if exists "ratings lecture publique" on question_ratings;
create policy "ratings lecture publique" on question_ratings for select using (true);
drop policy if exists "ratings user own insert" on question_ratings;
create policy "ratings user own insert" on question_ratings for insert with check (user_id = auth.uid());
drop policy if exists "ratings user own update" on question_ratings;
create policy "ratings user own update" on question_ratings for update using (user_id = auth.uid());
drop policy if exists "ratings user own delete" on question_ratings;
create policy "ratings user own delete" on question_ratings for delete using (user_id = auth.uid());

-- 6) Tags personnels : 1 ligne par (question, user). En plus des tags publics.
create table if not exists question_personal_tags (
  question_id uuid references bank_questions(id) on delete cascade,
  user_id     uuid references profiles(user_id)  on delete cascade,
  tags        text[] not null default '{}',
  primary key (question_id, user_id)
);
create index if not exists question_personal_tags_user_idx on question_personal_tags (user_id);
create index if not exists question_personal_tags_tags_idx on question_personal_tags using gin (tags);

alter table question_personal_tags enable row level security;
drop policy if exists "perso tags user own" on question_personal_tags;
create policy "perso tags user own" on question_personal_tags for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- 7) Fonction RPC SECURITY DEFINER : retourne les stats globales d'une question.
-- Nécessaire car question_evals est RLS-isolé per-user → un user normal ne peut
-- pas voir les évals des autres pour aggréger n_users/n_projects/total_n_eval.
-- La fonction tourne avec les privilèges du créateur (rôle owner du schéma) →
-- bypass RLS. Ne retourne que des agrégats anonymes (pas de fuite d'identité).
create or replace function get_question_eval_stats(qid uuid)
returns table (
  n_users         int,
  n_projects      int,
  total_n_eval    int,
  total_n_perfect int,
  avg_normalized  float
)
language sql security definer set search_path = public as $$
  select
    count(distinct user_id)::int                                            as n_users,
    count(distinct (user_id::text || '|' || project_name))::int             as n_projects,
    coalesce(sum(n_eval), 0)::int                                            as total_n_eval,
    coalesce(sum(n_perfect), 0)::int                                         as total_n_perfect,
    case when sum(n_eval) > 0 then sum(sum_normalized)/sum(n_eval) else null end as avg_normalized
  from question_evals
  where question_id = qid;
$$;

-- Permettre l'appel depuis PostgREST par tout user authentifié.
grant execute on function get_question_eval_stats(uuid) to authenticated, anon;
