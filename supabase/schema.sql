-- AMCx — Banque de questions en ligne (Supabase)
-- Phase A : tables + RLS pour parité fonctionnelle avec bank.py local.
-- Phase B (ratings, favoris, tags persos, vue agrégée) en bas, commentée.
--
-- À copier-coller dans : Supabase dashboard → SQL editor → "+ New query" → Run.
-- Idempotent : peut être re-run, ne perd aucune donnée existante.

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
-- Phase B (à activer plus tard) — ratings, favoris, tags persos, vue agrégée
-- ============================================================================
--
-- create table if not exists question_ratings (
--   question_id  uuid references bank_questions(id) on delete cascade,
--   user_id      uuid references profiles(user_id)  on delete cascade,
--   stars        int check (stars between 1 and 5),
--   favorite     bool default false,
--   comment      text,
--   created_at   timestamptz default now(),
--   modified_at  timestamptz default now(),
--   primary key (question_id, user_id)
-- );
-- alter table question_ratings enable row level security;
-- create policy "ratings lecture publique" on question_ratings for select using (true);
-- create policy "ratings user own insert"  on question_ratings for insert with check (user_id = auth.uid());
-- create policy "ratings user own update"  on question_ratings for update using (user_id = auth.uid());
-- create policy "ratings user own delete"  on question_ratings for delete using (user_id = auth.uid());
--
-- create table if not exists question_personal_tags (
--   question_id uuid references bank_questions(id) on delete cascade,
--   user_id     uuid references profiles(user_id)  on delete cascade,
--   tags        text[] not null default '{}',
--   primary key (question_id, user_id)
-- );
-- alter table question_personal_tags enable row level security;
-- create policy "perso tags user own" on question_personal_tags for all
--   using (user_id = auth.uid()) with check (user_id = auth.uid());
--
-- create materialized view question_stats_global as
-- select q.id as question_id,
--        count(distinct e.user_id) as n_users,
--        count(distinct e.project_name) as n_projects,
--        coalesce(sum(e.n_eval), 0) as total_n_eval,
--        coalesce(sum(e.n_perfect), 0) as total_n_perfect,
--        case when sum(e.n_eval) > 0 then sum(e.sum_normalized)/sum(e.n_eval) else null end as avg_normalized,
--        avg(r.stars) as avg_stars,
--        count(distinct r.user_id) filter (where r.favorite) as n_favorites
-- from bank_questions q
-- left join question_evals e   on e.question_id = q.id
-- left join question_ratings r on r.question_id = q.id
-- group by q.id;
-- create unique index on question_stats_global (question_id);
