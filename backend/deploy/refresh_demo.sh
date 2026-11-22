#!/bin/sh
# Daily demo refresh (v2 01D): destroy and rebuild the demo workspace so its visits stay relative to today.
# Run as a Railway cron service in the DEMO environment only (see docs/runbook_deploy_backup_rollback.md,
# "Demo workspace"). build_demo_workspace --reset refuses unless the database is a demo workspace.
set -e
if [ "${DEMO_WORKSPACE:-}" != "1" ]; then
  echo "refresh_demo.sh: DEMO_WORKSPACE is not 1 in this environment; nothing done" >&2
  exit 1
fi
if [ -z "${DEMO_DATABASE_FINGERPRINT:-}" ]; then
  echo "refresh_demo.sh: DEMO_DATABASE_FINGERPRINT is not set; nothing done" >&2
  exit 1
fi
eval "$(python deploy/bootstrap.py)"
exec env DJANGO_DB_IDENTITY=owner python manage.py build_demo_workspace --reset
