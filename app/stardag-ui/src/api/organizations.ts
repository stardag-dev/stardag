import { fetchWithAuth } from "./client";

const API_BASE = "/api/v1/ui";

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
