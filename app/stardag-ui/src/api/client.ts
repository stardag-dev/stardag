// API client with authentication support

type GetAccessToken = (orgId: string | null) => Promise<string | null>;

let getAccessTokenFn: GetAccessToken | null = null;
let currentOrgId: string | null = null;

// Set the access token getter (called by AuthConnector)
export function setAccessTokenGetter(fn: GetAccessToken): void {
  getAccessTokenFn = fn;
}

// Set the current org ID (called by EnvironmentContext when org changes)
export function setCurrentOrgId(orgId: string | null): void {
  currentOrgId = orgId;
}

// Get the current org ID
export function getCurrentOrgId(): string | null {
  return currentOrgId;
}

// Fetch wrapper that includes auth headers when available
export async function fetchWithAuth(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);

  // Add auth header if we have a token getter
  if (getAccessTokenFn) {
    try {
      // Use org-scoped token if org is set, otherwise use OIDC ID token
      const token = await getAccessTokenFn(currentOrgId);
      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
      }
    } catch (error) {
      console.warn("Failed to get access token:", error);
    }
  }

  return fetch(url, {
    ...options,
    headers,
  });
}

// Fetch with explicit org ID (for cases where you need a specific org's token)
export async function fetchWithOrgAuth(
  url: string,
  orgId: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);

  if (getAccessTokenFn) {
    try {
      const token = await getAccessTokenFn(orgId);
      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
      }
    } catch (error) {
      console.warn("Failed to get access token:", error);
    }
  }

  return fetch(url, {
    ...options,
    headers,
  });
}
