// API client with authentication support

type GetAccessToken = (workspaceId: string | null) => Promise<string | null>;

let getAccessTokenFn: GetAccessToken | null = null;
let currentWorkspaceId: string | null = null;

// Set the access token getter (called by AuthConnector)
export function setAccessTokenGetter(fn: GetAccessToken): void {
  getAccessTokenFn = fn;
}

// Set the current workspace ID (called by EnvironmentContext when workspace changes)
export function setCurrentWorkspaceId(workspaceId: string | null): void {
  currentWorkspaceId = workspaceId;
}

// Get the current workspace ID
export function getCurrentWorkspaceId(): string | null {
  return currentWorkspaceId;
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
      // Use workspace-scoped token if workspace is set, otherwise use OIDC ID token
      const token = await getAccessTokenFn(currentWorkspaceId);
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

// Fetch with explicit workspace ID (for cases where you need a specific workspace's token)
export async function fetchWithWorkspaceAuth(
  url: string,
  workspaceId: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);

  if (getAccessTokenFn) {
    try {
      const token = await getAccessTokenFn(workspaceId);
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
