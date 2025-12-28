import { fetchWithAuth } from "./client";
import { API_V1_UI } from "./config";

const API_BASE = API_V1_UI;

// --- Types ---

export interface Organization {
  id: string;
  name: string;
  slug: string;
  description: string | null;
}

export interface OrganizationDetail extends Organization {
  member_count: number;
  workspace_count: number;
}

export interface OrganizationSummary {
  id: string;
  name: string;
  slug: string;
  role: "owner" | "admin" | "member";
}

export interface UserProfile {
  user: {
    id: string;
    external_id: string;
    email: string;
    display_name: string | null;
  };
  organizations: OrganizationSummary[];
}

export interface Workspace {
  id: string;
  organization_id: string;
  name: string;
  slug: string;
  description: string | null;
  owner_id: string | null; // Non-null for personal workspaces
}

export interface Member {
  id: string;
  user_id: string;
  email: string;
  display_name: string | null;
  role: "owner" | "admin" | "member";
}

export interface Invite {
  id: string;
  email: string;
  role: "owner" | "admin" | "member";
  status: "pending" | "accepted" | "declined" | "cancelled";
  invited_by_email: string | null;
}

export interface PendingInvite {
  id: string;
  organization_id: string;
  organization_name: string;
  role: "owner" | "admin" | "member";
  invited_by_email: string | null;
}

// --- User Profile ---

export async function fetchUserProfile(): Promise<UserProfile> {
  const response = await fetchWithAuth(`${API_BASE}/me`);
  if (!response.ok) {
    throw new Error(`Failed to fetch profile: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchPendingInvites(): Promise<PendingInvite[]> {
  const response = await fetchWithAuth(`${API_BASE}/me/invites`);
  if (!response.ok) {
    throw new Error(`Failed to fetch invites: ${response.statusText}`);
  }
  return response.json();
}

// --- Organizations ---

export async function createOrganization(data: {
  name: string;
  slug: string;
  description?: string;
}): Promise<Organization> {
  const response = await fetchWithAuth(`${API_BASE}/organizations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create organization`);
  }
  return response.json();
}

export async function fetchOrganization(orgId: string): Promise<OrganizationDetail> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch organization: ${response.statusText}`);
  }
  return response.json();
}

export async function updateOrganization(
  orgId: string,
  data: { name?: string; description?: string },
): Promise<Organization> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`Failed to update organization: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteOrganization(orgId: string): Promise<void> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new Error(`Failed to delete organization: ${response.statusText}`);
  }
}

// --- Workspaces ---

export async function fetchWorkspaces(orgId: string): Promise<Workspace[]> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}/workspaces`);
  if (!response.ok) {
    throw new Error(`Failed to fetch workspaces: ${response.statusText}`);
  }
  return response.json();
}

export async function createWorkspace(
  orgId: string,
  data: { name: string; slug: string; description?: string },
): Promise<Workspace> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create workspace`);
  }
  return response.json();
}

export async function updateWorkspace(
  orgId: string,
  workspaceId: string,
  data: { name?: string; description?: string },
): Promise<Workspace> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to update workspace: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteWorkspace(
  orgId: string,
  workspaceId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to delete workspace`);
  }
}

// --- Members ---

export async function fetchMembers(orgId: string): Promise<Member[]> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}/members`);
  if (!response.ok) {
    throw new Error(`Failed to fetch members: ${response.statusText}`);
  }
  return response.json();
}

export async function updateMemberRole(
  orgId: string,
  memberId: string,
  role: "owner" | "admin" | "member",
): Promise<Member> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/members/${memberId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to update member role`);
  }
  return response.json();
}

export async function removeMember(orgId: string, memberId: string): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/members/${memberId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to remove member`);
  }
}

// --- Invites ---

export async function fetchInvites(orgId: string): Promise<Invite[]> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}/invites`);
  if (!response.ok) {
    throw new Error(`Failed to fetch invites: ${response.statusText}`);
  }
  return response.json();
}

export async function createInvite(
  orgId: string,
  data: { email: string; role: "admin" | "member" },
): Promise<Invite> {
  const response = await fetchWithAuth(`${API_BASE}/organizations/${orgId}/invites`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create invite`);
  }
  return response.json();
}

export async function cancelInvite(orgId: string, inviteId: string): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/invites/${inviteId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(`Failed to cancel invite: ${response.statusText}`);
  }
}

export async function acceptInvite(inviteId: string): Promise<Organization> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/invites/${inviteId}/accept`,
    {
      method: "POST",
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to accept invite: ${response.statusText}`);
  }
  return response.json();
}

export async function declineInvite(inviteId: string): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/invites/${inviteId}/decline`,
    {
      method: "POST",
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to decline invite: ${response.statusText}`);
  }
}

// --- API Keys ---

export interface ApiKey {
  id: string;
  workspace_id: string;
  name: string;
  key_prefix: string;
  created_by_id: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiKeyCreateResponse extends ApiKey {
  key: string; // The full key, only returned once on creation
}

export async function fetchApiKeys(
  orgId: string,
  workspaceId: string,
  includeRevoked = false,
): Promise<ApiKey[]> {
  const params = new URLSearchParams();
  if (includeRevoked) {
    params.append("include_revoked", "true");
  }
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/api-keys?${params}`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch API keys: ${response.statusText}`);
  }
  return response.json();
}

export async function createApiKey(
  orgId: string,
  workspaceId: string,
  name: string,
): Promise<ApiKeyCreateResponse> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/api-keys`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create API key`);
  }
  return response.json();
}

export async function revokeApiKey(
  orgId: string,
  workspaceId: string,
  keyId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/api-keys/${keyId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to revoke API key`);
  }
}

// --- Target Roots ---

export interface TargetRoot {
  id: string;
  workspace_id: string;
  name: string;
  uri_prefix: string;
  created_at: string;
}

export async function fetchTargetRoots(
  orgId: string,
  workspaceId: string,
): Promise<TargetRoot[]> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/target-roots`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch target roots: ${response.statusText}`);
  }
  return response.json();
}

export async function createTargetRoot(
  orgId: string,
  workspaceId: string,
  data: { name: string; uri_prefix: string },
): Promise<TargetRoot> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/target-roots`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create target root`);
  }
  return response.json();
}

export async function updateTargetRoot(
  orgId: string,
  workspaceId: string,
  rootId: string,
  data: { name?: string; uri_prefix?: string },
): Promise<TargetRoot> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/target-roots/${rootId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to update target root`);
  }
  return response.json();
}

export async function deleteTargetRoot(
  orgId: string,
  workspaceId: string,
  rootId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/organizations/${orgId}/workspaces/${workspaceId}/target-roots/${rootId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to delete target root`);
  }
}
