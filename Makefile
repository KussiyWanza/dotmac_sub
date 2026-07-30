.PHONY: help test test-v test-cov test-ci test-ci-shard test-fast test-integration test-architecture test-architecture-serial test-e2e lint type-check format security check lint-file type-check-file check-file migrate dev docker-up docker-down docker-logs worker beat coverage clean prod-build prod-pin prod-deploy prod-up prod-down prod-logs prod-restart prod-smtp-inbound-up prod-smtp-inbound-probe prod-migrate prod-check bump-version prod-ghcr-pin prod-ghcr-deploy deploy

# Production runs IMMUTABLE images: the base docker-compose.yml has no source
# bind-mounts and pulls code only from the baked image (built by `prod-build`).
# The dev overlay (docker-compose.dev.yml) that re-adds build:/bind-mounts is
# NEVER auto-loaded — it must be named explicitly. So both prod (PROD_COMPOSE)
# and any bare `docker compose` on a prod host run the immutable image only;
# only the dev targets below (DEV_COMPOSE) opt into the working-tree mounts.
PROD_COMPOSE = docker compose -f docker-compose.yml
DEV_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.dev.yml
# Image tag baked/run by the prod stack. Override per-deploy, e.g.
#   make prod-build APP_IMAGE=dotmac_sub:$(git rev-parse --short HEAD)
APP_IMAGE ?= dotmac_sub:latest

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Quality ──────────────────────────────────────────────

lint: lint-imports ## Run ruff linter + import boundary checks
	poetry run ruff check app/

lint-imports: ## Check import boundaries (import-linter contracts)
	poetry run lint-imports

format: ## Format code with ruff
	poetry run ruff format app/
	poetry run ruff check --fix app/

type-check: ## Run mypy type checker
	poetry run mypy app/ --ignore-missing-imports

security: ## Run bandit security scan
	poetry run bandit -r app/ -c pyproject.toml -q

check: lint type-check security ## Run all quality checks (lint + type-check + security)

lint-file: ## Lint a single file (usage: make lint-file FILE=app/services/nas.py)
	poetry run ruff check $(FILE)
	poetry run ruff format --check $(FILE)

type-check-file: ## Type-check a single file (usage: make type-check-file FILE=app/services/nas.py)
	poetry run mypy $(FILE) --ignore-missing-imports

check-file: lint-file type-check-file ## Lint + type-check a single file (usage: make check-file FILE=app/services/nas.py)

# ─── Release ──────────────────────────────────────────────

bump-version: ## Bump app version (usage: make bump-version BUMP=patch or VERSION=1.2.3)
	@if [ -n "$(VERSION)" ]; then \
		python3 scripts/bump_version.py --set "$(VERSION)"; \
	else \
		python3 scripts/bump_version.py "$(BUMP)"; \
	fi

# ─── Testing ──────────────────────────────────────────────

UNIT_TEST_PATHS := tests/ --ignore=tests/integration --ignore=tests/e2e
UNIT_TEST_WORKERS ?= auto
UNIT_TEST_ARGS = $(UNIT_TEST_PATHS) -n $(UNIT_TEST_WORKERS) --durations=25

test: ## Run the parallel non-integration suite (override UNIT_TEST_WORKERS as needed)
	poetry run pytest $(UNIT_TEST_ARGS) -q

test-v: ## Run the parallel non-integration suite (verbose)
	poetry run pytest $(UNIT_TEST_ARGS) -v

test-cov: ## Run the parallel non-integration suite with terminal coverage
	poetry run pytest $(UNIT_TEST_ARGS) --cov=app --cov-report=term-missing -q

test-ci: ## Run the canonical CI unit suite with XML coverage
	poetry run pytest $(UNIT_TEST_ARGS) --cov=app --cov-report=xml -q

CI_SHARD ?=
CI_SHARDS ?= 4
CI_DURATIONS_FILE ?= .ci-cache/test-durations.json
CI_DURATIONS_OUTPUT ?= .ci-cache/current-test-durations.json

test-ci-shard: ## Run one duration-balanced CI unit shard
	@test -n "$(CI_SHARD)" || (echo "CI_SHARD is required" >&2; exit 2)
	@paths="$$(poetry run python scripts/ci/select_test_shard.py --shard "$(CI_SHARD)" --shards "$(CI_SHARDS)" --durations-file "$(CI_DURATIONS_FILE)")"; \
	PYTHONPATH="$(CURDIR)" poetry run pytest $$paths -n auto --durations=25 --cov=app --cov-report= -q \
		-p scripts.ci.pytest_durations \
		--ci-durations-output="$(CI_DURATIONS_OUTPUT)"

test-fast: ## Run the parallel non-integration suite, stopping on first failure
	poetry run pytest $(UNIT_TEST_ARGS) -x --tb=short -q

test-integration: ## Run the PostgreSQL integration gate
	poetry run pytest tests/integration/ -v --tb=short -o "addopts="

test-architecture: ## Run architecture guards with the measured four-worker default
	poetry run pytest tests/architecture -q -n 4 --durations=50

test-architecture-serial: ## Run architecture guards serially for isolation/debugging
	poetry run pytest tests/architecture -q

test-e2e: ## Run end-to-end browser tests
	poetry run pytest tests/e2e/ -v --headed

# ─── Database ─────────────────────────────────────────────

migrate: ## Apply all pending migrations
	poetry run alembic upgrade head

migrate-new: ## Create a new migration (usage: make migrate-new msg="add users table")
	poetry run alembic revision --autogenerate -m "$(msg)"

migrate-down: ## Rollback last migration
	poetry run alembic downgrade -1

migrate-history: ## Show migration history
	poetry run alembic history --verbose

# ─── Development ──────────────────────────────────────────

dev: ## Run dev server with hot reload
	python -m uvicorn app.main:app --reload --port 8000

worker: ## Run Celery worker
	celery -A app.celery_app worker --loglevel=info

beat: ## Run Celery beat scheduler
	celery -A app.celery_app beat --loglevel=info

# ─── Docker ───────────────────────────────────────────────

docker-up: ## Start all Docker containers (dev overlay: working-tree bind-mounts)
	$(DEV_COMPOSE) up -d

docker-down: ## Stop all Docker containers
	$(DEV_COMPOSE) down

docker-logs: ## Tail Docker container logs
	$(DEV_COMPOSE) logs -f --tail=100

docker-rebuild: ## Rebuild and restart app container (dev)
	$(DEV_COMPOSE) build app && $(DEV_COMPOSE) up -d app

docker-shell: ## Open shell in app container
	docker exec -it dotmac_sub_app bash

docker-migrate: ## Run migrations inside Docker
	docker exec dotmac_sub_app alembic upgrade heads

# ─── Host-build fallback guard ─────────────────────────────────────────────
#
# Building the prod image ON the prod host is the fallback path, not the
# supported one: it bakes whatever that box's git tree happens to contain,
# which is how prod drifted away from main repeatedly. `make deploy TAG=...`
# (GHCR) is the supported path. These targets now refuse to run unless the
# operator states the intent explicitly, so nobody reaches for them under
# incident pressure without noticing what they are choosing.
define require-host-build
	if [ "$${ALLOW_HOST_BUILD:-0}" != "1" ]; then \
		echo "REFUSING TO BUILD ON HOST: this bakes the host's git tree, not a CI-tested commit." >&2; \
		echo "Supported path:  make deploy TAG=sha-<shortsha>   (pulls the CI-built GHCR image)" >&2; \
		echo "List tags:       bash scripts/deploy.sh --status" >&2; \
		echo "" >&2; \
		echo "If the registry is genuinely unreachable and you accept the drift risk:" >&2; \
		echo "  ALLOW_HOST_BUILD=1 make $@" >&2; \
		exit 1; \
	fi
endef

prod-build: ## [FALLBACK] Build the prod image on this host. Requires ALLOW_HOST_BUILD=1 — prefer `make deploy TAG=...`
	@set -eu; \
	$(require-host-build); \
	if [ -n "$$(git status --porcelain)" ]; then \
		echo "WARNING: working tree has uncommitted changes — building committed HEAD only; they will NOT be in the image."; \
	fi; \
	sha=$$(git rev-parse --short HEAD); \
	wt=$$(mktemp -d "$${TMPDIR:-/tmp}/dotmac-prod-build.XXXXXX"); \
	trap 'git worktree remove --force "$$wt" >/dev/null 2>&1 || rm -rf "$$wt"' EXIT INT TERM; \
	git worktree add --detach --quiet "$$wt" HEAD; \
	echo "Building $(APP_IMAGE) (+ dotmac_sub:latest, dotmac_sub:$$sha) from clean HEAD $$sha"; \
	docker build -t $(APP_IMAGE) -t dotmac_sub:latest -t "dotmac_sub:$$sha" "$$wt"

prod-deploy: ## [FALLBACK] Host-build deploy. Requires ALLOW_HOST_BUILD=1 — prefer `make deploy TAG=sha-<shortsha>`
	@set -eu; $(require-host-build)
	$(MAKE) prod-build
	$(MAKE) prod-pin
	$(MAKE) prod-migrate
	$(MAKE) prod-restart
	IMAGE_REPO=dotmac_sub RETAIN_IMAGES=5 TAG_REGEX='^[0-9a-f]+$$' bash scripts/docker_image_retention.sh || true

prod-pin: ## Point .env APP_IMAGE at the freshly-built HEAD image (compose's source of truth)
	@sha=$$(git rev-parse --short HEAD); \
	img="dotmac_sub:$$sha"; \
	if grep -q '^APP_IMAGE=' .env 2>/dev/null; then \
		sed -i.bak "s#^APP_IMAGE=.*#APP_IMAGE=$$img#" .env && rm -f .env.bak; \
	else \
		printf 'APP_IMAGE=%s\n' "$$img" >> .env; \
	fi; \
	echo "Pinned APP_IMAGE=$$img in .env (compose now runs this image)"

prod-up: ## Start the production (immutable-image) Docker stack
	$(PROD_COMPOSE) up -d

prod-down: ## Stop the production Docker stack
	$(PROD_COMPOSE) down

prod-logs: ## Tail production Docker logs
	$(PROD_COMPOSE) logs -f --tail=100

prod-restart: ## Recreate prod app + worker services from the current image (APP_IMAGE)
	$(PROD_COMPOSE) up -d app celery-worker celery-worker-bandwidth celery-worker-ingestion celery-worker-monitoring celery-worker-billing celery-worker-tr069 celery-beat bandwidth-poller syslog-listener

prod-smtp-inbound-up: ## Start/recreate the opt-in, single-instance SMTP intake
	$(PROD_COMPOSE) --profile smtp-inbound up -d --no-scale team-inbox-smtp

prod-smtp-inbound-probe: ## Prove SMTP intake creates a marked team-inbox message
	$(PROD_COMPOSE) --profile smtp-inbound exec -T team-inbox-smtp python -m app.team_inbox_smtp e2e-probe

prod-migrate: ## Apply migrations, retry lock timeouts, then verify schema contracts
	@n=0; until [ $$n -ge 4 ]; do \
	  out=$$($(PROD_COMPOSE) run --rm --no-deps app alembic upgrade heads 2>&1); rc=$$?; \
	  echo "$$out"; \
	  [ $$rc -eq 0 ] && break; \
	  if echo "$$out" | grep -qiE "lock timeout|canceling statement due to lock"; then \
	    n=$$((n+1)); echo ">> prod-migrate hit lock_timeout (attempt $$n/4) — a schema-locking migration could not grab its ACCESS EXCLUSIVE lock; retrying in 10s"; sleep 10; \
	  else \
	    echo ">> prod-migrate failed (not a lock_timeout) — aborting; app is untouched (migrate runs before recreate)"; exit $$rc; \
	  fi; \
	done; \
	[ $$n -lt 4 ] || { echo ">> prod-migrate still lock-blocked after retries — quiesce the app (stop app+workers), run migrate, then recreate. See seabone-staging-deploy-quirks."; exit 1; }; \
	$(PROD_COMPOSE) run --rm --no-deps app python -m scripts.migration.verify_schema_contracts

# ─── GHCR deploy (RECOMMENDED) ─────────────────────────────────────────────
# Pull the exact CI-built, CI-tested image instead of building on the host —
# decoupled from the box's git tree (which drifts). `make prod-deploy` above is
# a host-build fallback for air-gapped / registry-down situations only.
#
# `make deploy TAG=sha-<shortsha>` runs the hardened scripts/deploy.sh:
#   verify image on GHCR -> DB backup -> pin APP_IMAGE -> pull ->
#   migrate + verify -> warm candidate -> recreate app+workers -> health gate.
# CI (.github/workflows/ghcr.yml) pushes ghcr.io/<owner>/dotmac_sub per main push;
# the host must `docker login ghcr.io` (PAT with read:packages) once.
deploy: ## Hardened GHCR deploy. Usage: make deploy TAG=sha-abc1234
	@test -n "$(TAG)" || { echo "usage: make deploy TAG=sha-<shortsha> (see: scripts/deploy.sh --status)"; exit 1; }
	bash scripts/deploy.sh "$(TAG)"

genieacs-build: ## Rebuild the GenieACS image locally (CI publishes it; this is for local dev only)
	@set -eu; \
	version="$$(grep -oE 'genieacs@[0-9]+\.[0-9]+\.[0-9]+' docker/genieacs/Dockerfile | head -1 | cut -d@ -f2)"; \
	test -n "$$version" || { echo "could not parse genieacs version from docker/genieacs/Dockerfile" >&2; exit 1; }; \
	img="$(GHCR_IMAGE)-genieacs:$$version"; \
	echo "Building $$img from docker/genieacs"; \
	docker build -t "$$img" docker/genieacs

GHCR_IMAGE ?= ghcr.io/michaelayoade/dotmac_sub
GHCR_TAG ?= latest

# prod-ghcr-pin / prod-ghcr-deploy predate scripts/deploy.sh and skip its DB
# backup, OCI revision validation, warm-candidate handoff, health gates and
# rollback — and GHCR_TAG defaults to the moving `latest` tag. They are kept
# only as a manual escape hatch and refuse to run unless the operator states
# the intent explicitly, same contract as require-host-build above.
define require-legacy-ghcr-deploy
	if [ "$${ALLOW_LEGACY_GHCR_DEPLOY:-0}" != "1" ]; then \
		echo "REFUSING LEGACY GHCR PATH: this skips scripts/deploy.sh's backup, revision validation, health gates and rollback." >&2; \
		echo "Supported path:  make deploy TAG=sha-<shortsha>   (runs the hardened scripts/deploy.sh)" >&2; \
		echo "" >&2; \
		echo "If you accept an unguarded pin/deploy:" >&2; \
		echo "  ALLOW_LEGACY_GHCR_DEPLOY=1 make $@" >&2; \
		exit 1; \
	fi
endef

prod-ghcr-pin: ## [FALLBACK] Point .env APP_IMAGE at GHCR_IMAGE:GHCR_TAG. Requires ALLOW_LEGACY_GHCR_DEPLOY=1 — prefer `make deploy TAG=...`
	@set -eu; \
	$(require-legacy-ghcr-deploy); \
	img="$(GHCR_IMAGE):$(GHCR_TAG)"; \
	if grep -q '^APP_IMAGE=' .env 2>/dev/null; then \
		sed -i.bak "s#^APP_IMAGE=.*#APP_IMAGE=$$img#" .env && rm -f .env.bak; \
	else \
		printf 'APP_IMAGE=%s\n' "$$img" >> .env; \
	fi; \
	echo "Pinned APP_IMAGE=$$img in .env (compose now runs the CI-built image)"

prod-ghcr-deploy: ## [FALLBACK] Unguarded GHCR deploy. Requires ALLOW_LEGACY_GHCR_DEPLOY=1 — prefer `make deploy TAG=...`
	@set -eu; $(require-legacy-ghcr-deploy)
	$(MAKE) prod-ghcr-pin
	$(PROD_COMPOSE) pull app
	$(MAKE) prod-migrate
	$(MAKE) prod-restart
	IMAGE_REPO=$(GHCR_IMAGE) RETAIN_IMAGES=5 bash scripts/docker_image_retention.sh || true

prod-check: ## Run deployment reconciliation checks in the production stack
	$(PROD_COMPOSE) run --rm app python scripts/setup/deploy_reconcile.py

# ─── Credentials ──────────────────────────────────────────

encrypt-credentials: ## Audit all credential-at-rest values (dry run)
	poetry run python -m scripts.one_off.remediate_credential_encryption --dry-run

encrypt-credentials-execute: ## Encrypt plaintext values in all credential stores
	poetry run python -m scripts.one_off.remediate_credential_encryption --execute

cleanup-unrecoverable-credentials: ## Plan lifecycle-safe lost-key cleanup
	poetry run python -m scripts.one_off.cleanup_unrecoverable_credentials

cleanup-unrecoverable-credentials-execute: ## Execute the exact reviewed cleanup plan
	@test -n "$(PLAN_DIGEST)" || (echo "PLAN_DIGEST is required" && exit 2)
	poetry run python -m scripts.one_off.cleanup_unrecoverable_credentials \
		--execute --confirm-plan-digest "$(PLAN_DIGEST)"

reconcile-nas-lifecycle: ## Plan NAS lifecycle and subscription access-path repairs
	poetry run python -m scripts.one_off.reconcile_nas_lifecycle

reconcile-nas-lifecycle-details: ## Show bounded per-NAS review evidence
	poetry run python -m scripts.one_off.reconcile_nas_lifecycle --details

report-nas-access-path-evidence: ## Summarize recent history for blocked NAS rows
	poetry run python -m scripts.one_off.report_nas_access_path_evidence

report-nas-access-path-evidence-details: ## Show redacted per-NAS history evidence
	poetry run python -m scripts.one_off.report_nas_access_path_evidence --details

reconcile-nas-lifecycle-execute: ## Execute the exact reviewed NAS lifecycle plan
	@test -n "$(PLAN_DIGEST)" || (echo "PLAN_DIGEST is required" && exit 2)
	poetry run python -m scripts.one_off.reconcile_nas_lifecycle \
		--execute --confirm-plan-digest "$(PLAN_DIGEST)"

generate-encryption-key: ## Generate a new credential encryption key
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ─── GenieACS Setup ──────────────────────────────────────

setup-genieacs: ## Deploy provisions, virtual params, presets to GenieACS
	poetry run python scripts/setup_genieacs.py

setup-genieacs-dry-run: ## Preview GenieACS setup without making changes
	poetry run python scripts/setup_genieacs.py --dry-run

setup-genieacs-list: ## List current GenieACS provisions and presets
	poetry run python scripts/setup_genieacs.py --list

# ─── Pre-commit ───────────────────────────────────────────

pre-commit-install: ## Install pre-commit hooks
	poetry run pre-commit install

pre-commit-run: ## Run pre-commit on all files
	poetry run pre-commit run --all-files

# ─── Cleanup ──────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage
