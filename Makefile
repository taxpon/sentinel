COMPOSE ?= docker compose
UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-cov ci \
        db up down logs migrate shell \
        adr-index adr-check seed-issues bootstrap bootstrap-devin bootstrap-github

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the virtualenv from uv.lock
	$(UV) sync

lint:  ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:  ## Apply ruff formatting and safe fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:  ## mypy over src/sentinel
	$(UV) run mypy

test:  ## Run the test suite (tests touching the database need `make db` first)
	$(UV) run pytest

test-cov:  ## Run the test suite with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing

ci: lint typecheck test  ## Everything CI runs, locally

# Compose refuses to load the project at all when `env_file` is missing, so a fresh clone or a new
# worktree cannot even start Postgres for the tests. Seed it from the example instead of failing.
.env:
	@cp .env.example $@
	@echo 'Created .env from .env.example. Required values are blank — fill them in before "make up".'

db: | .env  ## Start only Postgres, and wait for it to accept connections
	$(COMPOSE) up -d --wait db

up: | .env  ## Start the full stack
	$(COMPOSE) up -d --build

down: | .env  ## Stop the stack (add CLEAN=1 to drop the database volume)
	$(COMPOSE) down $(if $(CLEAN),-v,)

logs: | .env  ## Follow the logs of every service
	$(COMPOSE) logs -f

migrate: | .env  ## Apply database migrations
	$(COMPOSE) run --rm api alembic upgrade head

shell: | .env  ## psql into the database
	$(COMPOSE) exec db psql -U sentinel -d sentinel

adr-index:  ## Regenerate docs/adr/index.md and .claude/rules/adr-pointers.md
	$(UV) run scripts/gen_adr_index.py

adr-check:  ## Fail if the generated ADR index is stale
	$(UV) run scripts/gen_adr_index.py --check

seed-issues:  ## Create or update the GitHub task issues from docs/tasks.yaml
	$(UV) run scripts/seed_issues.py

bootstrap: bootstrap-github bootstrap-devin  ## One-time setup of the target repo and Devin org

bootstrap-devin:  ## Register tags, knowledge and the nightly schedule in the Devin org
	$(UV) run scripts/bootstrap_devin.py

bootstrap-github:  ## Configure the fork's labels, issues and webhook
	$(UV) run scripts/bootstrap_github.py
