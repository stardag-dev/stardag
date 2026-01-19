import { fetchWithAuth } from "./client";
import { API_V1_UI } from "./config";

const API_BASE = API_V1_UI;

// --- Types ---

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  is_personal?: boolean;
}

export interface WorkspaceDetail extends Workspace {
  member_count: number;
  environment_count: number;
}

export interface WorkspaceSummary {
  id: string;
  name: string;
  slug: string;
  role: "owner" | "admin" | "member";
  is_personal?: boolean;
}

export interface UserProfile {
  user: {
    id: string;
    external_id: string;
    email: string;
    display_name: string | null;
  };
  workspaces: WorkspaceSummary[];
}

export interface Environment {
  id: string;
  workspace_id: string;
  name: string;
  slug: string;
  description: string | null;
  owner_id: string | null; // Deprecated: was used for personal environments
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
  workspace_id: string;
  workspace_name: string;
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

// --- Workspaces ---

export async function createWorkspace(data: {
  name: string;
  slug: string;
  description?: string;
  initial_environment_name?: string;
  initial_environment_slug?: string;
  initial_target_root_name?: string;
  initial_target_root_uri?: string;
}): Promise<Workspace> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create workspace`);
  }
  return response.json();
}

export async function fetchWorkspace(workspaceId: string): Promise<WorkspaceDetail> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces/${workspaceId}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch workspace: ${response.statusText}`);
  }
  return response.json();
}

export async function updateWorkspace(
  workspaceId: string,
  data: { name?: string; description?: string },
): Promise<Workspace> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces/${workspaceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`Failed to update workspace: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces/${workspaceId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new Error(`Failed to delete workspace: ${response.statusText}`);
  }
}

// --- Environments ---

export async function fetchEnvironments(workspaceId: string): Promise<Environment[]> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch environments: ${response.statusText}`);
  }
  return response.json();
}

export async function createEnvironment(
  workspaceId: string,
  data: { name: string; slug: string; description?: string },
): Promise<Environment> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create environment`);
  }
  return response.json();
}

export async function updateEnvironment(
  workspaceId: string,
  environmentId: string,
  data: { name?: string; description?: string },
): Promise<Environment> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to update environment: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteEnvironment(
  workspaceId: string,
  environmentId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to delete environment`);
  }
}

// --- Members ---

export async function fetchMembers(workspaceId: string): Promise<Member[]> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces/${workspaceId}/members`);
  if (!response.ok) {
    throw new Error(`Failed to fetch members: ${response.statusText}`);
  }
  return response.json();
}

export async function updateMemberRole(
  workspaceId: string,
  memberId: string,
  role: "owner" | "admin" | "member",
): Promise<Member> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/members/${memberId}`,
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

export async function removeMember(
  workspaceId: string,
  memberId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/members/${memberId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to remove member`);
  }
}

// --- Invites ---

export async function fetchInvites(workspaceId: string): Promise<Invite[]> {
  const response = await fetchWithAuth(`${API_BASE}/workspaces/${workspaceId}/invites`);
  if (!response.ok) {
    throw new Error(`Failed to fetch invites: ${response.statusText}`);
  }
  return response.json();
}

export async function createInvite(
  workspaceId: string,
  data: { email: string; role: "admin" | "member" },
): Promise<Invite> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/invites`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to create invite`);
  }
  return response.json();
}

export async function cancelInvite(
  workspaceId: string,
  inviteId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/invites/${inviteId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(`Failed to cancel invite: ${response.statusText}`);
  }
}

export async function acceptInvite(inviteId: string): Promise<Workspace> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/invites/${inviteId}/accept`,
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
    `${API_BASE}/workspaces/invites/${inviteId}/decline`,
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
  environment_id: string;
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
  workspaceId: string,
  environmentId: string,
  includeRevoked = false,
): Promise<ApiKey[]> {
  const params = new URLSearchParams();
  if (includeRevoked) {
    params.append("include_revoked", "true");
  }
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/api-keys?${params}`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch API keys: ${response.statusText}`);
  }
  return response.json();
}

export async function createApiKey(
  workspaceId: string,
  environmentId: string,
  name: string,
): Promise<ApiKeyCreateResponse> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/api-keys`,
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
  workspaceId: string,
  environmentId: string,
  keyId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/api-keys/${keyId}`,
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
  environment_id: string;
  name: string;
  uri_prefix: string;
  created_at: string;
}

export async function fetchTargetRoots(
  workspaceId: string,
  environmentId: string,
): Promise<TargetRoot[]> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/target-roots`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch target roots: ${response.statusText}`);
  }
  return response.json();
}

export async function createTargetRoot(
  workspaceId: string,
  environmentId: string,
  data: { name: string; uri_prefix: string },
): Promise<TargetRoot> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/target-roots`,
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
  workspaceId: string,
  environmentId: string,
  rootId: string,
  data: { name?: string; uri_prefix?: string },
): Promise<TargetRoot> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/target-roots/${rootId}`,
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
  workspaceId: string,
  environmentId: string,
  rootId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${API_BASE}/workspaces/${workspaceId}/environments/${environmentId}/target-roots/${rootId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to delete target root`);
  }
}
