SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON_BIN ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,backend/.venv/bin/python)
FRONTEND_PYTHON ?= python3
FRONTEND_HOST ?= 127.0.0.1
FRONTEND_PORT ?= 8090
FRONTEND_BACKEND ?= http://$(LOCAL_API_HOST):$(LOCAL_API_PORT)
COMPOSE ?= docker compose
LOCAL_COMPOSE = $(COMPOSE) -f deploy/docker-compose.yml
PROD_ENV_FILE ?= deploy/.env
PROD_COMPOSE_FILE ?= deploy/docker-compose.prod.yml
PROD_DEPLOY_DIR ?= $(CURDIR)/deploy
PROD_RUNTIME_DIR ?= $(PROD_DEPLOY_DIR)/runtime
PROD_SECRETS_DIR ?= $(PROD_DEPLOY_DIR)/secrets

# UAT uses the production topology and image, but keeps its own environment
# file, Compose project, secrets, runtime data and browser-cookie policy.
UAT_DEPLOY_DIR ?= $(CURDIR)/deploy/uat
UAT_ENV_FILE ?= $(UAT_DEPLOY_DIR)/.env
UAT_COMPOSE_FILE ?= $(CURDIR)/deploy/docker-compose.prod.yml
UAT_COMPOSE_PROJECT ?= quicstudio-uat
UAT_RUNTIME_DIR ?= $(UAT_DEPLOY_DIR)/runtime
UAT_SECRETS_DIR ?= $(UAT_DEPLOY_DIR)/secrets
UAT_HEALTH_URL ?= http://127.0.0.1:18080/health

# Compose always receives a tag derived from the checkout being deployed.
# Keep local/demo targets independent of deployment inputs; production targets
# still fail early when the image tag cannot be derived from a clean checkout.
override PROD_IMAGE_TAG := $(if $(filter prod-% oss-browser-cors-%,$(MAKECMDGOALS)),$(shell bash scripts/deployment-image-tag.sh),)
override UAT_IMAGE_TAG := $(if $(filter uat-bootstrap-admin,$(MAKECMDGOALS)),$(shell bash scripts/deployment-image-tag.sh),)
PROD_COMPOSE = QUICSTUDIO_IMAGE_TAG="$(PROD_IMAGE_TAG)" DEPLOY_RUNTIME_DIR="$(PROD_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(PROD_SECRETS_DIR)" $(COMPOSE) --env-file $(PROD_ENV_FILE) -f $(PROD_COMPOSE_FILE)
UAT_COMPOSE = QUICSTUDIO_IMAGE_TAG="$(UAT_IMAGE_TAG)" DEPLOY_RUNTIME_DIR="$(UAT_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(UAT_SECRETS_DIR)" $(COMPOSE) --project-name "$(UAT_COMPOSE_PROJECT)" --env-file "$(UAT_ENV_FILE)" -f "$(UAT_COMPOSE_FILE)"
LOCAL_DATABASE_URL ?= postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata
LOCAL_SCRATCH_ROOT ?= $(CURDIR)/deploy/runtime/scratch
LOCAL_API_HOST ?= 127.0.0.1
LOCAL_API_PORT ?= 8000
LOCAL_STORAGE_PROVIDER ?= minio
LOCAL_STORAGE_ENDPOINT ?= http://127.0.0.1:9000
LOCAL_STORAGE_ACCESS_KEY_ID ?= quicstudio
LOCAL_STORAGE_SECRET_ACCESS_KEY ?= quicstudio-local-secret
LOCAL_STORAGE_BROWSER_ENDPOINT ?= http://127.0.0.1:9000
LOCAL_OSS_BUCKET_RAW ?= quicstudio-dev-raw
LOCAL_OSS_BUCKET_PROCESS ?= quicstudio-dev-process
LOCAL_OSS_BUCKET_EXPORT ?= quicstudio-dev-export
DEV_STORAGE_ENV = STORAGE_PROVIDER="$(LOCAL_STORAGE_PROVIDER)" STORAGE_ENDPOINT="$(LOCAL_STORAGE_ENDPOINT)" STORAGE_ACCESS_KEY_ID="$(LOCAL_STORAGE_ACCESS_KEY_ID)" STORAGE_SECRET_ACCESS_KEY="$(LOCAL_STORAGE_SECRET_ACCESS_KEY)" STORAGE_BROWSER_ENDPOINT="$(LOCAL_STORAGE_BROWSER_ENDPOINT)" OSS_BUCKET_RAW="$(LOCAL_OSS_BUCKET_RAW)" OSS_BUCKET_PROCESS="$(LOCAL_OSS_BUCKET_PROCESS)" OSS_BUCKET_EXPORT="$(LOCAL_OSS_BUCKET_EXPORT)"
DEV_ENV = ENVIRONMENT=development ALLOW_DEFAULT_USER_SEED=true REALTIME_DISPATCHER_IN_API=true DATABASE_URL="$(LOCAL_DATABASE_URL)" SCRATCH_ROOT="$(LOCAL_SCRATCH_ROOT)" REDIS_URL="redis://127.0.0.1:6379/0" API_HOST="$(LOCAL_API_HOST)" API_PORT="$(LOCAL_API_PORT)" $(DEV_STORAGE_ENV) WEB_CONCURRENCY=1

.PHONY: help dev-frontend dev-mock dev-frontend-up dev-mock-up dev-frontend-down dev-frontend-logs db-upgrade dev-seed infra-up infra-down minio-up minio-down dev-pg dev-worker dev-api dev-worker-control dev-worker-ingest dev-worker-media dev-worker-publish dev-worker-export dev-worker-analytics dev-worker-governance dev-worker-ai dev-beat dev-run dev-migrate dev-seed-local dev-screen-up dev-up dev-status dev-attach dev-logs dev-restart dev-down dev-infra-down dev-wipe vendor-frontend test-vendor-qrdf supply-check prod-image-tag prod-init prod-migrate prod-up prod-bootstrap-admin prod-status prod-logs prod-down prod-wipe uat-init uat-up uat-bootstrap-admin uat-status uat-logs uat-down uat-wipe uat-cors-check uat-cors-apply oss-browser-cors-check oss-browser-cors-apply

help:
	@printf '%s\n' \
		'QuicStudio commands:' \
		'  make dev-mock                         Frontend-only mock/demo' \
		'  make dev-frontend                     Frontend preview with API proxy' \
		'  make dev-frontend-up|dev-mock-up      Managed frontend preview/mock' \
		'  make dev-frontend-down|dev-frontend-logs  Stop/inspect managed frontend' \
		'  make demo-check                       Verify every demo view renders' \
		'  make dev-up                           Full local development stack' \
		'  make dev-status|dev-logs SERVICE=frontend  Inspect local services' \
		'  make dev-down                         Stop local application services' \
		'  make prod-init && make prod-up        Initialize/start production' \
		'  make prod-status|prod-logs             Inspect production' \
		'  make uat-init && make uat-up           Initialize/start UAT' \
		'  make uat-status|uat-logs                Inspect UAT' \
		'  make uat-cors-check                    Check UAT OSS CORS (allows HTTP)' \
		'  make prod-wipe / make uat-wipe         Destructive; requires confirmation'

dev-frontend:
	@echo "QuicStudio Web: http://$(FRONTEND_HOST):$(FRONTEND_PORT)/"
	$(FRONTEND_PYTHON) frontend/serve_preview.py --host "$(FRONTEND_HOST)" --port "$(FRONTEND_PORT)" --backend "$(FRONTEND_BACKEND)"

dev-mock:
	@echo "QuicStudio Mock: http://$(FRONTEND_HOST):$(FRONTEND_PORT)/?demo=1"
	$(FRONTEND_PYTHON) frontend/serve_preview.py --host "$(FRONTEND_HOST)" --port "$(FRONTEND_PORT)" --backend "$(FRONTEND_BACKEND)" --mock

dev-frontend-up:
	@FRONTEND_PYTHON="$(FRONTEND_PYTHON)" FRONTEND_HOST="$(FRONTEND_HOST)" FRONTEND_PORT="$(FRONTEND_PORT)" FRONTEND_BACKEND="$(FRONTEND_BACKEND)" FRONTEND_MOCK=false bash scripts/dev-screen.sh restart frontend

dev-mock-up:
	@FRONTEND_PYTHON="$(FRONTEND_PYTHON)" FRONTEND_HOST="$(FRONTEND_HOST)" FRONTEND_PORT="$(FRONTEND_PORT)" FRONTEND_BACKEND="$(FRONTEND_BACKEND)" FRONTEND_MOCK=true bash scripts/dev-screen.sh restart frontend

dev-frontend-down:
	@bash scripts/dev-screen.sh down frontend

dev-frontend-logs:
	@bash scripts/dev-screen.sh logs frontend

.PHONY: demo-check
demo-check:
	@node --test frontend/tests/demo-mode-flows.test.mjs
	@node scripts/demo-smoke.mjs

db-upgrade:
	@$(DEV_ENV) PYTHONPATH=backend $(PYTHON_BIN) -m scripts.migrate

dev-seed:
	@$(DEV_ENV) PYTHONPATH=backend $(PYTHON_BIN) -m scripts.seed_dev

infra-up:
	$(LOCAL_COMPOSE) up -d postgres redis minio minio-init

infra-down:
	$(LOCAL_COMPOSE) down

minio-up:
	$(LOCAL_COMPOSE) up -d minio minio-init

minio-down:
	$(LOCAL_COMPOSE) stop minio-init minio

dev-pg: infra-up
	@bash scripts/dev-screen.sh wait-infra
	@$(DEV_ENV) $(MAKE) db-upgrade
	@$(DEV_ENV) bash scripts/start.sh

dev-api:
	@$(DEV_ENV) bash scripts/start.sh

dev-worker: dev-worker-control

dev-worker-control dev-worker-ingest dev-worker-media dev-worker-publish dev-worker-export dev-worker-analytics dev-worker-governance dev-worker-ai:
	@$(DEV_ENV) bash scripts/dev-screen-service.sh "$(patsubst dev-worker-%,%,$@)"

dev-beat:
	@$(DEV_ENV) bash scripts/dev-screen-service.sh beat

dev-run:
	@$(DEV_ENV) bash scripts/dev-screen-service.sh "$(SERVICE)"

dev-migrate:
	@$(DEV_ENV) $(MAKE) db-upgrade

dev-seed-local:
	@$(DEV_ENV) $(MAKE) dev-seed

dev-screen-up:
	@LOCAL_API_HOST="$(LOCAL_API_HOST)" LOCAL_API_PORT="$(LOCAL_API_PORT)" FRONTEND_PYTHON="$(FRONTEND_PYTHON)" FRONTEND_HOST="$(FRONTEND_HOST)" FRONTEND_PORT="$(FRONTEND_PORT)" FRONTEND_BACKEND="$(FRONTEND_BACKEND)" FRONTEND_MOCK=false bash scripts/dev-screen.sh up

dev-up:
	@$(MAKE) infra-up
	@bash scripts/dev-screen.sh wait-infra
	@$(MAKE) dev-migrate
	@$(MAKE) dev-seed-local
	@$(MAKE) dev-screen-up

dev-status:
	@bash scripts/dev-screen.sh status

dev-attach:
	@bash scripts/dev-screen.sh attach "$(SERVICE)"

dev-logs:
	@bash scripts/dev-screen.sh logs "$(SERVICE)"

dev-restart:
	@$(MAKE) dev-down
	@$(MAKE) dev-migrate
	@$(MAKE) dev-screen-up

dev-down:
	@bash scripts/dev-screen.sh down

dev-infra-down:
	@$(MAKE) dev-down
	@$(LOCAL_COMPOSE) down

dev-wipe:
	@CONFIRM_DEV_WIPE="$(CONFIRM_DEV_WIPE)" bash scripts/dev-screen.sh wipe

vendor-frontend:
	@test -f "$(PROD_ENV_FILE)" || { echo "Missing $(PROD_ENV_FILE); copy deploy/.env.example first" >&2; exit 2; }
	@set -a; source "$(PROD_ENV_FILE)"; set +a; bash scripts/vendor-frontend.sh

test-vendor-qrdf:
	$(PYTHON_BIN) -m pip install -e "backend/vendor/qrdf[dev]"
	$(PYTHON_BIN) -m pytest backend/vendor/qrdf/tests -q

supply-check:
	bash scripts/check-domestic-sources.sh

prod-image-tag:
	@bash scripts/deployment-image-tag.sh

# Production defaults remain deploy/runtime and deploy/secrets, including
# deploy/secrets/storage_access_key_id and deploy/secrets/storage_secret_access_key.
prod-init: prod-image-tag
	@bash scripts/init-deployment.sh \
		--env-file "$(PROD_ENV_FILE)" \
		--runtime-dir "$(PROD_RUNTIME_DIR)" \
		--secrets-dir "$(PROD_SECRETS_DIR)" \
		--label production

prod-migrate: prod-init
	$(PROD_COMPOSE) up --build migrate

oss-browser-cors-check: prod-init
	@DEPLOY_RUNTIME_DIR="$(PROD_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(PROD_SECRETS_DIR)" QUICSTUDIO_IMAGE_TAG="$(PROD_IMAGE_TAG)" bash scripts/oss-browser-cors.sh --env-file "$(PROD_ENV_FILE)" --compose-file "$(PROD_COMPOSE_FILE)"

oss-browser-cors-apply: prod-init
	@DEPLOY_RUNTIME_DIR="$(PROD_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(PROD_SECRETS_DIR)" QUICSTUDIO_IMAGE_TAG="$(PROD_IMAGE_TAG)" bash scripts/oss-browser-cors.sh --env-file "$(PROD_ENV_FILE)" --compose-file "$(PROD_COMPOSE_FILE)" --apply

prod-up: prod-init
	$(PROD_COMPOSE) build migrate
	$(PROD_COMPOSE) up -d --wait redis-state
	$(PROD_COMPOSE) up migrate
	$(PROD_COMPOSE) up -d --build api realtime-dispatcher worker-control worker-ingest worker-media worker-publish worker-export worker-analytics celery-beat

prod-bootstrap-admin: prod-init
	@test -n "$$ADMIN_EMAIL" || { echo "Set ADMIN_EMAIL before bootstrap, for example: make prod-bootstrap-admin ADMIN_EMAIL=admin@example.com"; exit 2; }
	@test -f "$(PROD_SECRETS_DIR)/admin_bootstrap_password" || { echo "Missing $(PROD_SECRETS_DIR)/admin_bootstrap_password; run make prod-init first"; exit 2; }
	$(PROD_COMPOSE) run --rm bootstrap-admin python -m scripts.bootstrap_admin --email "$$ADMIN_EMAIL" --password-file /run/secrets/admin_bootstrap_password --leave-password-file
	@echo "Initial administrator password (shown once): $$(cat "$(PROD_SECRETS_DIR)/admin_bootstrap_password")"
	@rm -f "$(PROD_SECRETS_DIR)/admin_bootstrap_password"
	@touch "$(PROD_SECRETS_DIR)/admin_bootstrap_consumed"
	@chmod 600 "$(PROD_SECRETS_DIR)/admin_bootstrap_consumed"

prod-status:
	$(PROD_COMPOSE) ps

prod-logs:
	$(PROD_COMPOSE) logs -f --tail=200

prod-down:
	$(PROD_COMPOSE) down
	@echo "Containers stopped. deploy/runtime/ remains intact."

prod-wipe:
	@test "$$CONFIRM_WIPE" = "DELETE_DEPLOY_RUNTIME" || { echo "Refusing to remove deploy/runtime. Re-run with CONFIRM_WIPE=DELETE_DEPLOY_RUNTIME after taking a backup."; exit 2; }
	@find "$(PROD_RUNTIME_DIR)" -mindepth 1 -maxdepth 1 ! -name .gitignore -exec rm -rf {} +
	@echo "Removed persisted deployment data from $(PROD_RUNTIME_DIR)."

uat-init:
	@bash scripts/init-deployment.sh \
		--env-file "$(UAT_ENV_FILE)" \
		--runtime-dir "$(UAT_RUNTIME_DIR)" \
		--secrets-dir "$(UAT_SECRETS_DIR)" \
		--label UAT

uat-up: uat-init
	@QUICDATA_REPO_ROOT="$(CURDIR)" \
		QUICDATA_DEPLOY_DIR="$(UAT_DEPLOY_DIR)" \
		QUICDATA_ENV_FILE="$(UAT_ENV_FILE)" \
		QUICDATA_COMPOSE_FILE="$(UAT_COMPOSE_FILE)" \
		QUICSTUDIO_COMPOSE_PROJECT="$(UAT_COMPOSE_PROJECT)" \
		UAT_HEALTH_URL="$(UAT_HEALTH_URL)" \
		DEPLOY_RUNTIME_DIR="$(UAT_RUNTIME_DIR)" \
		DEPLOY_SECRETS_DIR="$(UAT_SECRETS_DIR)" \
		QUICDATA_SECRETS_DIR="$(UAT_SECRETS_DIR)" \
		bash scripts/uat-up.sh

uat-bootstrap-admin: uat-init
	@test -n "$$ADMIN_EMAIL" || { echo "Set ADMIN_EMAIL before UAT bootstrap, for example: make uat-bootstrap-admin ADMIN_EMAIL=admin@example.com"; exit 2; }
	@test -f "$(UAT_SECRETS_DIR)/admin_bootstrap_password" || { echo "Missing $(UAT_SECRETS_DIR)/admin_bootstrap_password; run make uat-init first"; exit 2; }
	$(UAT_COMPOSE) run --rm bootstrap-admin python -m scripts.bootstrap_admin --email "$$ADMIN_EMAIL" --password-file /run/secrets/admin_bootstrap_password --leave-password-file
	@echo "Initial UAT administrator password (shown once): $$(cat "$(UAT_SECRETS_DIR)/admin_bootstrap_password")"
	@rm -f "$(UAT_SECRETS_DIR)/admin_bootstrap_password"
	@touch "$(UAT_SECRETS_DIR)/admin_bootstrap_consumed"
	@chmod 600 "$(UAT_SECRETS_DIR)/admin_bootstrap_consumed"

uat-status:
	$(UAT_COMPOSE) ps

uat-logs:
	$(UAT_COMPOSE) logs -f --tail=200

uat-down:
	$(UAT_COMPOSE) down
	@echo "UAT containers stopped. $(UAT_RUNTIME_DIR) remains intact."

uat-wipe:
	@test "$$CONFIRM_WIPE" = "DELETE_UAT_RUNTIME" || { echo "Refusing to remove UAT runtime. Re-run with CONFIRM_WIPE=DELETE_UAT_RUNTIME after taking a backup."; exit 2; }
	@test -d "$(UAT_RUNTIME_DIR)" || { echo "UAT runtime directory does not exist: $(UAT_RUNTIME_DIR)"; exit 2; }
	@$(UAT_COMPOSE) down
	@find "$(UAT_RUNTIME_DIR)" -mindepth 1 -maxdepth 1 ! -name .gitignore -exec rm -rf {} +
	@echo "Removed persisted UAT data from $(UAT_RUNTIME_DIR)."

uat-cors-check: uat-init
	@DEPLOY_RUNTIME_DIR="$(UAT_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(UAT_SECRETS_DIR)" bash scripts/oss-browser-cors.sh --env-file "$(UAT_ENV_FILE)" --compose-file "$(UAT_COMPOSE_FILE)" --project-name "$(UAT_COMPOSE_PROJECT)" --allow-http

uat-cors-apply: uat-init
	@DEPLOY_RUNTIME_DIR="$(UAT_RUNTIME_DIR)" DEPLOY_SECRETS_DIR="$(UAT_SECRETS_DIR)" bash scripts/oss-browser-cors.sh --env-file "$(UAT_ENV_FILE)" --compose-file "$(UAT_COMPOSE_FILE)" --project-name "$(UAT_COMPOSE_PROJECT)" --allow-http --apply
