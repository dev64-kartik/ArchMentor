#!/usr/bin/env bash
# Creates secondary databases and required schemas on first boot of the
# shared Postgres instance. Runs automatically via
# /docker-entrypoint-initdb.d on initial container creation only.
set -euo pipefail

psql_admin() {
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" "$@"
}

create_db_if_missing() {
  local db_name="$1"
  echo "Ensuring database exists: ${db_name}"
  psql_admin --dbname "${POSTGRES_DB}" <<-EOSQL
    SELECT 'CREATE DATABASE "${db_name}"'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db_name}')
    \gexec
EOSQL
}

create_schema_if_missing() {
  local db_name="$1"
  local schema_name="$2"
  echo "Ensuring schema '${schema_name}' exists in database '${db_name}'"
  psql_admin --dbname "${db_name}" <<-EOSQL
    CREATE SCHEMA IF NOT EXISTS "${schema_name}";
EOSQL
}

auth_db="${AUTH_DB:-auth}"
langfuse_db="${LANGFUSE_DB:-langfuse}"

create_db_if_missing "${auth_db}"
create_db_if_missing "${langfuse_db}"

# GoTrue keeps its tables in the `auth` schema, not `public`.
create_schema_if_missing "${auth_db}" "auth"

echo "Postgres init complete."
