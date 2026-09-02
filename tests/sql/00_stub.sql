create schema if not exists auth;
create table auth.users (id uuid primary key);
create or replace function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('test.uid', true), '')::uuid $$;
create table profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null);
create table bank_questions (
  id uuid primary key default gen_random_uuid(),
  author_id uuid not null references profiles(user_id) on delete cascade,
  title text not null,
  status text not null default 'draft',
  modified_at timestamptz default now());
alter table bank_questions enable row level security;
create policy "q lecture" on bank_questions for select
  using (status = 'public' or author_id = auth.uid());
create or replace function touch_modified_at() returns trigger language plpgsql as
  $$ begin new.modified_at = now(); return new; end; $$;
-- deux utilisateurs
insert into auth.users(id) values
  ('aaaaaaaa-0000-0000-0000-000000000001'),
  ('bbbbbbbb-0000-0000-0000-000000000002');
insert into profiles(user_id, display_name) values
  ('aaaaaaaa-0000-0000-0000-000000000001','A'),
  ('bbbbbbbb-0000-0000-0000-000000000002','B');
