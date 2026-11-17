# Local development. Requires Docker and uv. See README.md.
SHELL := /bin/bash
ENV := set -a; source .env.example; [ -f .env ] && source .env; set +a;
BE := cd backend &&

.PHONY: up down db migrate seed run test lint typecheck migcheck ci reset

up:            ## start db + migrate + app in containers
	docker compose up --build

db:            ## start only the database
	docker compose up -d db

migrate: db    ## run migrations as the schema owner
	$(ENV) $(BE) DJANGO_DB_IDENTITY=owner uv run python manage.py migrate --noinput

seed: migrate  ## create synthetic dev users
	$(ENV) $(BE) DJANGO_DB_IDENTITY=owner uv run python manage.py seed_dev && DJANGO_DB_IDENTITY=owner uv run python manage.py seed_fleet && DJANGO_DB_IDENTITY=owner uv run python manage.py seed_planning && DJANGO_DB_IDENTITY=owner uv run python manage.py seed_readiness

run: migrate   ## run the app on the host as the runtime role
	$(ENV) $(BE) DJANGO_DB_IDENTITY=app uv run python manage.py runserver

test: db       ## tests against real Postgres (test DB created by the owner role)
	$(ENV) $(BE) DJANGO_DB_IDENTITY=owner uv run pytest

lint:
	$(BE) uv run ruff check . && uv run ruff format --check .

typecheck:
	$(ENV) $(BE) DJANGO_DB_IDENTITY=owner uv run mypy .

migcheck: db
	$(ENV) $(BE) DJANGO_DB_IDENTITY=owner uv run python manage.py makemigrations --check --dry-run

ci: lint typecheck migcheck test   ## the same steps CI runs

down:
	docker compose down

reset:         ## destroy the local database volume
	docker compose down -v
