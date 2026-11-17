#!/bin/bash
# Runs once, as the Postgres superuser, when the database volume is first created.
# Creates the two identities the platform uses:
#   ops_owner - owns the schema and runs migrations (CREATEDB so the test runner can create test DBs)
#   ops_app   - runtime role; table privileges are granted by migrations, never ownership
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
  CREATE ROLE ${DB_OWNER_USER} LOGIN CREATEDB PASSWORD '${DB_OWNER_PASSWORD}';
  CREATE ROLE ${DB_APP_USER} LOGIN PASSWORD '${DB_APP_PASSWORD}' NOCREATEDB NOCREATEROLE;
  CREATE DATABASE ${DB_NAME} OWNER ${DB_OWNER_USER};
SQL
