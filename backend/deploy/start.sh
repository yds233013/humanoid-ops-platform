#!/bin/sh
# Hosted demo start: roles + database, migrations (owner), demo seed once, then gunicorn as the runtime role.
set -e
eval "$(python deploy/bootstrap.py)"
DJANGO_DB_IDENTITY=owner python manage.py migrate --noinput
# Owner provisioning of operating settings from PROVISION_* env vars (harmless when none are present).
if env | grep -q '^PROVISION_[A-Z_]*=.'; then
  DJANGO_DB_IDENTITY=owner python manage.py provision_settings
fi
if DJANGO_DB_IDENTITY=owner python deploy/needs_seed.py; then
  for c in seed_dev seed_fleet seed_planning seed_guardrails seed_readiness seed_execution seed_operations seed_guardrails; do
    DJANGO_DB_IDENTITY=owner python manage.py "$c"
  done
fi
exec env DJANGO_DB_IDENTITY=app gunicorn config.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 60
