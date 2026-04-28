// API client with authentication support

interface GetAccessTokenOptions {
  /**
   * Skip the localStorage token cache and always re-exchange via Cognito.
   * Used by ``fetchWithAuth`` after an unexpected 401 to recover from a
   * cached internal token that has gone stale before its client-side
   * ``expiresAt`` (e.g. signed by a prior API container's keypair, or the
   * server has rotated the JWT signing key).
   */
  forceRefresh?: boolean;
}

type GetAccessToken = (
  workspaceId: string | null,
  opts?: GetAccessTokenOptions,
) => Promise<string | null>;

type SessionExpiredHandler = () => void;

let getAccessTokenFn: GetAccessToken | null = null;
let currentWorkspaceId: string | null = null;
let sessionExpiredHandler: SessionExpiredHandler | null = null;

// Set the access token getter (called by AuthConnector)
export function setAccessTokenGetter(fn: GetAccessToken): void {
  getAccessTokenFn = fn;
}

// Set the session-expired handler (called by AuthConnector). Invoked by
// the fetch wrappers when a 401 cannot be recovered via re-exchange — at
// that point the user's Cognito session has likely expired and they need
// to log in again.
export function setSessionExpiredHandler(handler: SessionExpiredHandler | null): void {
  sessionExpiredHandler = handler;
}

// Set the current workspace ID (called by EnvironmentContext when workspace changes)
export function setCurrentWorkspaceId(workspaceId: string | null): void {
  currentWorkspaceId = workspaceId;
}

// Get the current workspace ID
export function getCurrentWorkspaceId(): string | null {
  return currentWorkspaceId;
}

/**
 * Internal helper that runs the actual fetch and handles 401 once.
 *
 * Flow:
 *  1. Pull a token from ``getTokenFor()`` (the caller decides whether to
 *     resolve a workspace token, the bootstrap OIDC token, etc.).
 *  2. Send the request.
 *  3. If status is 401 *and* we got a token on attempt 1, force a refresh
 *     and retry exactly once. We don't retry when there was no token on
 *     attempt 1 (an unauthenticated caller can't usefully retry).
 *  4. If the retry also returns 401, signal the session-expired handler
 *     (the user needs to log in again) and return the response — the
 *     caller's ``response.ok`` check still gets to throw its own error.
 */
async function fetchWithRetry(
  url: string,
  options: RequestInit,
  getTokenFor: (forceRefresh: boolean) => Promise<string | null>,
): Promise<Response> {
  const headers = new Headers(options.headers);

  let token: string | null = null;
  try {
    token = await getTokenFor(false);
  } catch (error) {
    console.warn("Failed to get access token:", error);
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(url, { ...options, headers });

  if (response.status !== 401 || !token) {
    return response;
  }

  // 401 with a token attached → either the token is stale (server rotated
  // its signing key, or the cached token outlived its TTL despite the
  // client's belief that it was still valid) or the token has been
  // explicitly invalidated server-side. Force a refresh and retry once.
  let refreshedToken: string | null = null;
  try {
    refreshedToken = await getTokenFor(true);
  } catch (error) {
    console.warn("Token refresh after 401 failed:", error);
  }
  if (!refreshedToken || refreshedToken === token) {
    sessionExpiredHandler?.();
    return response;
  }

  const retryHeaders = new Headers(options.headers);
  retryHeaders.set("Authorization", `Bearer ${refreshedToken}`);
  const retryResponse = await fetch(url, { ...options, headers: retryHeaders });
  if (retryResponse.status === 401) {
    sessionExpiredHandler?.();
  }
  return retryResponse;
}

// Fetch wrapper that includes auth headers when available
export async function fetchWithAuth(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  return fetchWithRetry(url, options, async (forceRefresh) => {
    if (!getAccessTokenFn) return null;
    return getAccessTokenFn(currentWorkspaceId, { forceRefresh });
  });
}

// Fetch wrapper for bootstrap/user-level endpoints that always use OIDC ID token
// Use this for endpoints like /me, /me/invites that don't require workspace-scoped tokens
export async function fetchWithBootstrapAuth(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  return fetchWithRetry(url, options, async (forceRefresh) => {
    if (!getAccessTokenFn) return null;
    return getAccessTokenFn(null, { forceRefresh });
  });
}

// Fetch with explicit workspace ID (for cases where you need a specific workspace's token)
export async function fetchWithWorkspaceAuth(
  url: string,
  workspaceId: string,
  options: RequestInit = {},
): Promise<Response> {
  return fetchWithRetry(url, options, async (forceRefresh) => {
    if (!getAccessTokenFn) return null;
    return getAccessTokenFn(workspaceId, { forceRefresh });
  });
}
