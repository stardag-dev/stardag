-- Postgres initialization script for stardag
-- Creates separate admin and service roles following principle of least privilege

-- Create admin role (used by Alembic migrations and maintainers)
-- Can CREATE, ALTER, DROP tables
CREATE ROLE stardag_admin WITH LOGIN PASSWORD 'stardag_admin';

-- Create service role (used by FastAPI at runtime)
-- Can only SELECT, INSERT, UPDATE, DELETE
CREATE ROLE stardag_service WITH LOGIN PASSWORD 'stardag_service';

-- Grant admin full access to the database
GRANT ALL PRIVILEGES ON DATABASE stardag TO stardag_admin;

-- Allow admin to create schemas and grant to others
GRANT CREATE ON DATABASE stardag TO stardag_admin;

-- Connect as superuser to set up schema permissions
\connect stardag

-- Grant admin full control of public schema
GRANT ALL ON SCHEMA public TO stardag_admin;

-- Grant service usage on public schema (can access objects, but not create)
GRANT USAGE ON SCHEMA public TO stardag_service;

-- Set default privileges: when admin creates tables, service gets DML access
ALTER DEFAULT PRIVILEGES FOR ROLE stardag_admin IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stardag_service;

-- Set default privileges for sequences (needed for auto-increment PKs)
ALTER DEFAULT PRIVILEGES FOR ROLE stardag_admin IN SCHEMA public
GRANT USAGE, SELECT ON SEQUENCES TO stardag_service;

-- Note: These default privileges apply to future tables created by stardag_admin.
-- Existing tables (if any) would need explicit grants.
