-- Development seed data for local testing
-- This runs AFTER migrations, only in local dev environment
-- Production uses OIDC login to create users/orgs dynamically
--
-- Matches Keycloak test users in docker/keycloak/realm-export.json:
--   - testuser@localhost (primary test user)
--   - admin@localhost

-- Only insert if not already present (idempotent)
-- Note: external_id will be updated on first OIDC login with actual Keycloak sub claim
INSERT INTO users (id, external_id, email, display_name, created_at)
VALUES ('testuser', 'keycloak-testuser', 'testuser@localhost', 'Test User', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO organizations (id, name, slug, created_by_id, created_at)
VALUES ('default', 'Default Organization', 'default', 'testuser', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO organization_members (id, organization_id, user_id, role, created_at)
VALUES ('default-member', 'default', 'testuser', 'owner', NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO workspaces (id, organization_id, name, slug, owner_id, created_at)
VALUES ('default', 'default', 'Default Workspace', 'default', 'testuser', NOW())
ON CONFLICT (id) DO NOTHING;
