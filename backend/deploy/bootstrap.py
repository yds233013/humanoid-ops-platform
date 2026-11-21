"""Hosted-demo bootstrap (Railway or any host with a Postgres superuser URL). Idempotent; runs on every start.

Reads ADMIN_DATABASE_URL (a superuser connection, e.g. Railway's DATABASE_URL), creates the two platform roles
and the platform database if missing (the same as docker/postgres/init/01-roles.sh), and prints shell `export`
lines for DB_HOST / DB_PORT so the Django settings connect to the same server. SYNTHETIC demo only.
"""

import os
import sys
from urllib.parse import urlparse

import psycopg
from psycopg import sql

url = urlparse(os.environ["ADMIN_DATABASE_URL"])
owner, owner_pw = os.environ["DB_OWNER_USER"], os.environ["DB_OWNER_PASSWORD"]
app, app_pw = os.environ["DB_APP_USER"], os.environ["DB_APP_PASSWORD"]
dbname = os.environ["DB_NAME"]

conn = psycopg.connect(
    host=url.hostname,
    port=url.port or 5432,
    user=url.username,
    password=url.password,
    dbname=(url.path or "/postgres").lstrip("/") or "postgres",
    autocommit=True,
)
with conn.cursor() as cur:
    for role, pw, extra in ((owner, owner_pw, "CREATEDB"), (app, app_pw, "NOCREATEDB NOCREATEROLE")):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(
            sql.SQL(f"{verb} ROLE {{}} LOGIN {extra} PASSWORD {{}}").format(
                sql.Identifier(role), sql.Literal(pw)
            )
        )
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [dbname])
    if not cur.fetchone():
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(owner))
        )
conn.close()
print(f"export DB_HOST={url.hostname}")
print(f"export DB_PORT={url.port or 5432}")
print("bootstrap: roles and database ready", file=sys.stderr)
