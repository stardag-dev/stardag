// Authentication API functions
import { API_V1 } from "./config";

const API_BASE = API_V1;

export interface TokenExchangeResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

/**
 * Exchange an OIDC token for a workspace-scoped internal token.
 *
 * @param oidcToken - The OIDC access token from the identity provider
 * @param workspaceId - The workspace ID to scope the token to
 * @returns The workspace-scoped internal access token
 */
export async function exchangeToken(
  oidcToken: string,
  workspaceId: string,
): Promise<TokenExchangeResponse> {
  const response = await fetch(`${API_BASE}/auth/exchange`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${oidcToken}`,
    },
    body: JSON.stringify({ workspace_id: workspaceId }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Token exchange failed: ${response.statusText}`);
  }

  return response.json();
}

// --- Local auth mode (email/password) ---

export interface SessionTokenResponse {
  session_token: string;
  token_type: string;
  expires_in: number;
}

async function postLocalAuth(
  path: string,
  body: Record<string, unknown>,
): Promise<SessionTokenResponse> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Request failed: ${response.statusText}`);
  }
  return response.json();
}

/**
 * Log in with email/password (local auth mode). Returns a user-scoped
 * session token used for bootstrap endpoints and workspace token exchange.
 */
export async function loginLocal(
  email: string,
  password: string,
): Promise<SessionTokenResponse> {
  return postLocalAuth("/auth/login", { email, password });
}

/**
 * Self-service registration (local auth mode, when enabled on the server).
 * Returns a session token (auto-login).
 */
export async function registerLocal(
  email: string,
  password: string,
  displayName?: string,
): Promise<SessionTokenResponse> {
  return postLocalAuth("/auth/register", {
    email,
    password,
    display_name: displayName || null,
  });
}
