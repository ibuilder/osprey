-- Creates the ordinary role the Osprey app connects as.
--
-- Why this exists: Postgres lets superusers — and any role with BYPASSRLS — skip
-- row-level security completely, and FORCE ROW LEVEL SECURITY does NOT override
-- that. If the app connects as the database superuser (the default in most
-- Postgres images), the tenant-isolation policies in migration 0002 are applied
-- but enforce nothing. So the app gets its own plain role.
--
-- Runs once, automatically, on first container start (docker-entrypoint-initdb.d).
-- The owner role (POSTGRES_USER) still owns the schema and runs migrations.

\set app_password `echo "${OSPREY_APP_DB_PASSWORD:-osprey_app}"`

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'osprey_app') THEN
    CREATE ROLE osprey_app LOGIN;
  END IF;
END $$;

ALTER ROLE osprey_app WITH PASSWORD :'app_password';

-- Explicitly ensure the app role can never bypass row-level security.
ALTER ROLE osprey_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE osprey TO osprey_app;
GRANT USAGE ON SCHEMA public TO osprey_app;

-- Tables do not exist yet — migrations create them later, owned by POSTGRES_USER.
-- Default privileges make those future tables readable/writable by the app role
-- without a second manual grant step after every migration.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO osprey_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO osprey_app;

-- Cover anything that already exists (e.g. re-running against a populated volume).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO osprey_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO osprey_app;
