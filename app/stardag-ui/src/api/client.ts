// API client with authentication support

type GetAccessToken = () => Promise<string | null>;

let getAccessTokenFn: GetAccessToken | null = null;

// Set the access token getter (called by AuthProvider)
export function setAccessTokenGetter(fn: GetAccessToken): void {
  getAccessTokenFn = fn;
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
      const token = await getAccessTokenFn();
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
