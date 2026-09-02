#!/bin/sh
# Vérifie la section 8 de supabase/schema.sql sur un vrai Postgres.
#
# Ce que la suite Python NE PEUT PAS couvrir : le trigger anti-cycle et
# anti-profondeur, l'index d'unicité entre frères (avec le piège du NULL à la
# racine), les FK `restrict`/`cascade`, et les policies RLS. Le faux PostgREST
# de tests/fake_postgrest.py ne simule aucun de ces mécanismes.
#
#   docker requis.   ./tests/sql/run.sh
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/../.." && pwd)
CT=amcx-pg-test

docker rm -f $CT >/dev/null 2>&1 || true
docker run -d --name $CT -e POSTGRES_PASSWORD=x postgres:16-alpine >/dev/null
trap 'docker rm -f $CT >/dev/null 2>&1 || true' EXIT
i=0
while ! docker exec $CT pg_isready -U postgres >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 30 ] && { echo "postgres n'a pas démarré"; exit 1; }
  sleep 1
done

# Section 8 seule : le reste du schéma dépend de auth.users / Supabase.
awk '/^-- 8\) Catégories hiérarchiques/{f=1} f' "$ROOT/supabase/schema.sql" > /tmp/section8.sql
docker exec $CT psql -U postgres -q -c "create role app login noinherit" >/dev/null 2>&1
docker exec $CT psql -U postgres -q -c "create database t" >/dev/null
docker cp "$DIR/00_stub.sql"        $CT:/tmp/ >/dev/null
docker cp /tmp/section8.sql         $CT:/tmp/ >/dev/null
docker cp "$DIR/01_tree_and_rls.sql" $CT:/tmp/ >/dev/null

echo "== application du schéma =="
docker exec $CT psql -U postgres -d t -v ON_ERROR_STOP=1 -q \
  -f /tmp/00_stub.sql -f /tmp/section8.sql 2>&1 | grep -i error && exit 1
echo "== idempotence (2e passage) =="
docker exec $CT psql -U postgres -d t -v ON_ERROR_STOP=1 -q \
  -f /tmp/section8.sql 2>&1 | grep -i error && exit 1
echo "   ok"
echo "== comportements =="
OUT=$(docker exec $CT psql -U postgres -d t -q -f /tmp/01_tree_and_rls.sql 2>&1 \
      | grep -v '^$\|^SET\|^RESET\|^NOTICE\|^GRANT\|^INSERT\|^UPDATE\|^DELETE\|^bbbb\|^aaaa\|^set_config')
echo "$OUT"
echo "$OUT" | grep -q '✘' && { echo; echo "ÉCHEC"; exit 1; }
echo
echo "TOUT PASSE"
