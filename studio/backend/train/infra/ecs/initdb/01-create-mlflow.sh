#!/bin/bash
# Runs only on first PostgreSQL data-dir init (empty volume).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-EOSQL
  SELECT 'CREATE DATABASE mlflow OWNER ${POSTGRES_USER}'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec
EOSQL
