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
