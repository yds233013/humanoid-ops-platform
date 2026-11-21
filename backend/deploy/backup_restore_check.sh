#!/bin/sh
# Back up a platform database and PROVE the backup restores (v2 contract 12.4; runbook docs/runbook_deploy_backup_rollback.md).
#
#   backup_restore_check.sh SOURCE_DATABASE_URL TARGET_DATABASE_URL [BACKUP_FILE]
#
# SOURCE is dumped with pg_dump (custom format). TARGET must be an EMPTY database that is NOT the source
# (never a production database); it is restored with pg_restore (privileges kept, so the roles must exist) and a few row counts are compared with the
# source. Exit 0 only when every check matches. Needs pg_dump, pg_restore and psql (PostgreSQL client 16+).
# URLs are read from the arguments only, never echoed (they contain passwords).
set -eu

SRC="${1:?usage: $0 SOURCE_DATABASE_URL TARGET_DATABASE_URL [BACKUP_FILE]}"
DST="${2:?usage: $0 SOURCE_DATABASE_URL TARGET_DATABASE_URL [BACKUP_FILE]}"
OUT="${3:-./backup-$(date -u +%Y%m%dT%H%M%SZ).dump}"

[ "$SRC" != "$DST" ] || { echo "REFUSED: source and target are the same database" >&2; exit 2; }

q() { psql "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"; }

n_tables="$(q "$DST" "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
if [ "$n_tables" != "0" ]; then
  echo "REFUSED: target database is not empty ($n_tables tables in schema public)" >&2
  exit 2
fi

echo "1/4 dumping source to $OUT"
pg_dump "$SRC" --format=custom --no-owner --file "$OUT"

echo "2/4 restoring into the empty target"
pg_restore --dbname "$DST" --no-owner --exit-on-error "$OUT"

echo "3/4 row-count checks (source vs restored)"
fail=0
check() {
  s="$(q "$SRC" "SELECT count(*) FROM $1")"
  d="$(q "$DST" "SELECT count(*) FROM $1")"
  if [ "$s" = "$d" ]; then echo "  ok   $1: $s"; else echo "  FAIL $1: source=$s restored=$d"; fail=1; fi
}
for t in django_migrations auth_user accounts_rolegrant platform_core_auditevent platform_core_workspace \
         fleet_robot planning_visit; do
  # a table that does not exist in this build is skipped, not failed
  if [ "$(q "$SRC" "SELECT to_regclass('public.$t') IS NOT NULL")" = "t" ]; then check "$t"; fi
done

echo "4/4 integrity: audit trigger and singleton constraint came across"
trg="$(q "$DST" "SELECT count(*) FROM pg_trigger WHERE tgname IN ('audit_no_update_delete','audit_no_truncate')")"
if [ "$trg" = "2" ]; then echo "  ok   audit append-only triggers present"; else echo "  FAIL audit triggers: $trg of 2"; fail=1; fi
con="$(q "$DST" "SELECT count(*) FROM pg_constraint WHERE conname = 'workspace_singleton_id_is_1'")"
if [ "$con" = "1" ]; then echo "  ok   workspace singleton constraint present"; else echo "  FAIL workspace constraint missing"; fail=1; fi

if [ "$fail" = "0" ]; then echo "RESULT: restore check PASSED (backup file: $OUT)"; else echo "RESULT: restore check FAILED" >&2; exit 1; fi
