"""Exit 0 when the demo data has not been loaded yet (no robots), 1 otherwise. Run as the schema owner."""

import os
import sys

import psycopg

with (
    psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_OWNER_USER"],
        password=os.environ["DB_OWNER_PASSWORD"],
    ) as c,
    c.cursor() as cur,
):
    cur.execute("SELECT count(*) FROM fleet_robot")
    row = cur.fetchone()
    sys.exit(0 if row is not None and row[0] == 0 else 1)
