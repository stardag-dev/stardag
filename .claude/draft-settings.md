# Draft Settings - Session 2025-12-30

This file tracks new permissions granted during this session for later review.

## New Permissions to Add

- [ ] Web Search
- [ ] npx tsc commands
- [ ] npm run build
- [ ] ./node_modules/.bin/tsc

## Session Notes

- Investigating Cognito logout issue (missing client_id parameter)

## Changes Made This Session

### Cognito Logout Fix

**Problem**: Logout from app.stardag.com failed with "Required String parameter 'client_id' is not present"

**Root Cause**: Amazon Cognito's logout endpoint doesn't follow standard OIDC - it requires `client_id` and `logout_uri` instead of standard parameters.

**Solution**: Added Cognito-specific logout handling:

1. `app/stardag-ui/src/auth/config.ts` - Added:
   - `COGNITO_DOMAIN` env var
   - `isCognitoIssuer()` - detect Cognito vs Keycloak
   - `getCognitoLogoutUrl()` - construct Cognito logout URL

2. `app/stardag-ui/src/context/AuthContext.tsx` - Updated `logout()` to:
   - Use Cognito-specific URL when `isCognitoIssuer()` returns true
   - Fall back to standard OIDC logout for Keycloak

3. `infra/aws-cdk/scripts/deploy-ui.sh` - Added `VITE_COGNITO_DOMAIN` env var

4. `.claude/tasks/registry-service/aws-deployment.md` - Documented the issue and solution

**To Deploy**: Run `./infra/aws-cdk/scripts/deploy-ui.sh` (requires `.env.deploy` file)

### deploy-ui.sh Hardening

Added validation to require `.env.deploy` file and `DOMAIN_NAME` variable, with clear error messages if missing.
