// Authentication API functions
import { API_V1 } from "./config";

const API_BASE = API_V1;

export interface TokenExchangeResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

/**
 * Exchange a Keycloak token for an org-scoped internal token.
 *
 * @param keycloakToken - The Keycloak access token
 * @param orgId - The organization ID to scope the token to
 * @returns The org-scoped internal access token
 */
export async function exchangeToken(
  keycloakToken: string,
  orgId: string,
): Promise<TokenExchangeResponse> {
  const response = await fetch(`${API_BASE}/auth/exchange`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${keycloakToken}`,
    },
    body: JSON.stringify({ org_id: orgId }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Token exchange failed: ${response.statusText}`);
  }

  return response.json();
}
