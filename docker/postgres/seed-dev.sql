-- Development seed data for local testing
-- This runs AFTER migrations, only in local dev environment
-- Production uses OIDC login to create users/orgs dynamically

-- Only insert if not already present (idempotent)
INSERT INTO users (id, external_id, email, display_name, created_at)
VALUES ('default', 'default-local-user', 'default@localhost', 'Default User', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO organizations (id, name, slug, created_by_id, created_at)
VALUES ('default', 'Default Organization', 'default', 'default', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO organization_members (id, organization_id, user_id, role, created_at)
VALUES ('default', 'default', 'default', 'owner', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO workspaces (id, organization_id, name, slug, owner_id, created_at)
VALUES ('default', 'default', 'Default Workspace', 'default', 'default', NOW())
ON CONFLICT (id) DO NOTHING;
