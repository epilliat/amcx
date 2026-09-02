-- Comportements de la section 8 : trigger d'arbre, unicité entre frères,
-- FK, cascade, RLS. Chaque ligne affiche ✔ ou ✘.
--
-- UUID littéraux (pas de \set : psql ne conserve pas les quotes) et
-- dollar-quoting pour embarquer du SQL contenant des apostrophes.
--   A   = aaaaaaaa-…01     B   = bbbbbbbb-…02
--   INF = c0000000-…01     TST = …02     STU = …03     VAL = …04
\pset tuples_only on
\pset format unaligned

create or replace function must_fail(sql text, label text) returns text
language plpgsql as $fn$
begin execute sql; return '  ✘ ÉCHEC (accepté à tort) : ' || label;
exception when others then
  return '  ✔ refusé : ' || label || '  [' || left(sqlerrm, 55) || ']'; end; $fn$;
create or replace function must_pass(sql text, label text) returns text
language plpgsql as $fn$
begin execute sql; return '  ✔ accepté : ' || label;
exception when others then
  return '  ✘ ÉCHEC (refusé à tort) : ' || label || '  [' || left(sqlerrm, 55) || ']'; end; $fn$;

insert into bank_categories(id, parent_id, name, position, created_by) values
 ('c0000000-0000-0000-0000-000000000001', null, 'Inférence', 0, 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000001', 'Tests', 0, 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-000000000003', 'c0000000-0000-0000-0000-000000000002', 'Student', 0, 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-000000000004', null, 'Validation', 1, 'aaaaaaaa-0000-0000-0000-000000000001');

select '--- trigger : cycles et profondeur ---';
select must_fail($$update bank_categories set parent_id = id
                   where id = 'c0000000-0000-0000-0000-000000000001'$$,
                 'nœud son propre parent');
select must_fail($$update bank_categories
                   set parent_id = 'c0000000-0000-0000-0000-000000000003'
                   where id = 'c0000000-0000-0000-0000-000000000001'$$,
                 'cycle (parent = son petit-enfant)');
select must_pass($$insert into bank_categories(parent_id, name, created_by) values
                   ('c0000000-0000-0000-0000-000000000003', 'N4',
                    'aaaaaaaa-0000-0000-0000-000000000001')$$, '4e niveau');
select must_fail($$insert into bank_categories(parent_id, name, created_by)
                   select id, 'N5', 'aaaaaaaa-0000-0000-0000-000000000001'
                   from bank_categories where name = 'N4'$$, '5e niveau');
delete from bank_categories where name = 'N4';

select '--- déplacement : la profondeur du SOUS-ARBRE compte ---';
-- A2[B2[C2]] = hauteur 3 ; Y est à la profondeur 2.
insert into bank_categories(id, parent_id, name, created_by) values
 ('c0000000-0000-0000-0000-0000000000a1', null, 'A2', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-0000000000a2', 'c0000000-0000-0000-0000-0000000000a1', 'B2', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-0000000000a3', 'c0000000-0000-0000-0000-0000000000a2', 'C2', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-0000000000b1', null, 'X', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('c0000000-0000-0000-0000-0000000000b2', 'c0000000-0000-0000-0000-0000000000b1', 'Y', 'aaaaaaaa-0000-0000-0000-000000000001');
select must_fail($$update bank_categories set parent_id = 'c0000000-0000-0000-0000-0000000000b2'
                   where name = 'A2'$$, 'A2[B2[C2]] sous Y (profondeur 2) → 5 niveaux');
select must_pass($$update bank_categories set parent_id = 'c0000000-0000-0000-0000-0000000000b1'
                   where name = 'A2'$$, 'A2[B2[C2]] sous X (profondeur 1) → 4 niveaux pile');
update bank_categories set parent_id = null where name = 'A2';

select '--- unicité des noms entre frères ---';
select must_fail($$insert into bank_categories(parent_id, name, created_by) values
                   ('c0000000-0000-0000-0000-000000000002', '  student ',
                    'aaaaaaaa-0000-0000-0000-000000000001')$$,
                 'doublon sous le même parent (casse et espaces)');
select must_fail($$insert into bank_categories(parent_id, name, created_by) values
                   (null, 'inférence', 'aaaaaaaa-0000-0000-0000-000000000001')$$,
                 'doublon À LA RACINE (le piège du NULL)');
select must_pass($$insert into bank_categories(parent_id, name, created_by) values
                   ('c0000000-0000-0000-0000-000000000004', 'Student',
                    'aaaaaaaa-0000-0000-0000-000000000001')$$,
                 'même nom sous un autre parent');
delete from bank_categories where name = 'Student'
   and parent_id = 'c0000000-0000-0000-0000-000000000004';
select must_fail($$insert into bank_categories(name, created_by) values
                   ('   ', 'aaaaaaaa-0000-0000-0000-000000000001')$$, 'nom vide');

select '--- suppression ---';
select must_fail($$delete from bank_categories
                   where id = 'c0000000-0000-0000-0000-000000000001'$$,
                 'supprimer un nœud qui a des enfants (FK restrict)');

select '--- cascade : les affectations partent, jamais les questions ---';
insert into bank_questions(id, author_id, title, status) values
 ('11111111-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001', 'Publique de A', 'public'),
 ('11111111-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000001', 'Brouillon de A', 'draft');
insert into question_categories(question_id, category_id, added_by) values
 ('11111111-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('11111111-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000001');
delete from bank_categories where id = 'c0000000-0000-0000-0000-000000000003';
select case when (select count(*) from question_categories) = 0
             and (select count(*) from bank_questions) = 2
       then '  ✔ affectations effacées, questions intactes'
       else '  ✘ ÉCHEC cascade' end;

select '--- RLS : ce qu''un autre prof voit et peut faire ---';
-- ⚠ Un refus RLS sur DELETE/UPDATE ne lève PAS d'erreur : il filtre les
-- lignes. On compte donc les lignes touchées, on n'attend pas d'exception.
insert into question_categories(question_id, category_id, added_by) values
 ('11111111-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001'),
 ('11111111-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001');
grant usage on schema public, auth to app;
grant select, insert, update, delete on all tables in schema public to app;
grant execute on all functions in schema auth to app;

set role app;
select set_config('test.uid', 'bbbbbbbb-0000-0000-0000-000000000002', false) \g /dev/null
select case when count(*) = 1 then '  ✔ B ne voit que le classement de la question publique'
            else '  ✘ ÉCHEC : B voit ' || count(*) || ' affectation(s)' end
  from question_categories;
select case when count(*) = 0 then '  ✔ B ne voit pas le brouillon de A'
            else '  ✘ ÉCHEC' end from bank_questions where status = 'draft';
select case when count(*) = (select count(*) from bank_categories)
       then '  ✔ B voit tout l''arbre (partagé, ' || count(*) || ' nœuds)'
       else '  ✘ ÉCHEC' end from bank_categories;
select must_pass($$insert into bank_categories(name, created_by) values
                   ('Ajouté par B', 'bbbbbbbb-0000-0000-0000-000000000002')$$,
                 'B crée un nœud (mode wiki)');
select must_fail($$insert into bank_categories(name, created_by) values
                   ('Usurpation', 'aaaaaaaa-0000-0000-0000-000000000001')$$,
                 'B crée un nœud au nom de A');
with d as (delete from question_categories
           where added_by = 'aaaaaaaa-0000-0000-0000-000000000001' returning 1)
select case when count(*) = 0
       then '  ✔ B ne retire aucun classement de A (refus silencieux)'
       else '  ✘ ÉCHEC : ' || count(*) || ' ligne(s) supprimée(s)' end from d;
with u as (update question_categories
           set added_by = 'bbbbbbbb-0000-0000-0000-000000000002' where true returning 1)
select case when count(*) = 0
       then '  ✔ B ne modifie aucune affectation (pas de policy UPDATE)'
       else '  ✘ ÉCHEC : ' || count(*) end from u;
with i as (insert into question_categories(question_id, category_id, added_by)
           values ('11111111-0000-0000-0000-000000000001',
                   'c0000000-0000-0000-0000-0000000000b1',
                   'bbbbbbbb-0000-0000-0000-000000000002') returning 1)
select case when count(*) = 1 then '  ✔ B classe la question publique de A'
            else '  ✘ ÉCHEC' end from i;
with d as (delete from question_categories
           where added_by = 'bbbbbbbb-0000-0000-0000-000000000002' returning 1)
select case when count(*) = 1 then '  ✔ B retire son propre classement'
            else '  ✘ ÉCHEC : ' || count(*) end from d;
reset role;

select set_config('test.uid', 'aaaaaaaa-0000-0000-0000-000000000001', false) \g /dev/null
set role app;
with d as (delete from question_categories
           where question_id = '11111111-0000-0000-0000-000000000001' returning 1)
select case when count(*) = 1 then '  ✔ A retire le classement de sa propre question'
            else '  ✘ ÉCHEC : ' || count(*) end from d;
reset role;
