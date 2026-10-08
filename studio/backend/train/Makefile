.PHONY: api scheduler test lint web build

api:
	.venv/bin/uvicorn quictrain_api.main:app --reload --port 8000

scheduler:
	.venv/bin/python -m quictrain_scheduler.main

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .

web:
	pnpm --dir apps/web dev

build:
	pnpm --dir apps/web build
