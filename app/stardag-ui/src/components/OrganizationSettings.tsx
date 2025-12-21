import { useState, useEffect } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import {
  fetchOrganization,
  fetchMembers,
  fetchInvites,
  fetchWorkspaces,
  updateOrganization,
  createInvite,
  cancelInvite,
  updateMemberRole,
  removeMember,
  deleteOrganization,
  createWorkspace,
  updateWorkspace,
  deleteWorkspace,
  type OrganizationDetail,
  type Member,
  type Invite,
  type Workspace,
} from "../api/organizations";

interface OrganizationSettingsProps {
  onNavigate: (path: string) => void;
}

export function OrganizationSettings({ onNavigate }: OrganizationSettingsProps) {
  const { activeOrg, activeOrgRole, refresh } = useWorkspace();

  const [organization, setOrganization] = useState<OrganizationDetail | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [workspacesList, setWorkspacesList] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Edit form state
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  // Invite form state
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
  const [isInviting, setIsInviting] = useState(false);

  // Workspace form state
  const [newWorkspaceName, setNewWorkspaceName] = useState("");
  const [newWorkspaceSlug, setNewWorkspaceSlug] = useState("");
  const [isCreatingWorkspace, setIsCreatingWorkspace] = useState(false);
  const [editingWorkspace, setEditingWorkspace] = useState<string | null>(null);
  const [editWorkspaceName, setEditWorkspaceName] = useState("");

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  const isAdmin = activeOrgRole === "owner" || activeOrgRole === "admin";
  const isOwner = activeOrgRole === "owner";

  useEffect(() => {
    async function loadData() {
      if (!activeOrg) return;

      setLoading(true);
      setError(null);
      try {
        const [org, membersList, invitesList, wsList] = await Promise.all([
          fetchOrganization(activeOrg.id),
          fetchMembers(activeOrg.id),
          isAdmin ? fetchInvites(activeOrg.id) : Promise.resolve([]),
          fetchWorkspaces(activeOrg.id),
        ]);
        setOrganization(org);
        setMembers(membersList);
        setInvites(invitesList);
        setWorkspacesList(wsList);
        setEditName(org.name);
        setEditDescription(org.description || "");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load organization");
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [activeOrg, isAdmin]);

  const handleSave = async () => {
    if (!activeOrg) return;

    setIsSaving(true);
    try {
      await updateOrganization(activeOrg.id, {
        name: editName,
        description: editDescription || undefined,
      });
      await refresh();
      setOrganization((prev) =>
        prev ? { ...prev, name: editName, description: editDescription } : null,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setIsSaving(false);
    }
  };

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeOrg || !inviteEmail) return;

    setIsInviting(true);
    try {
      const newInvite = await createInvite(activeOrg.id, {
        email: inviteEmail,
        role: inviteRole,
      });
      setInvites((prev) => [...prev, newInvite]);
      setInviteEmail("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send invite");
    } finally {
      setIsInviting(false);
    }
  };

  const handleCancelInvite = async (inviteId: string) => {
    if (!activeOrg) return;
    try {
      await cancelInvite(activeOrg.id, inviteId);
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel invite");
    }
  };

  const handleUpdateRole = async (
    memberId: string,
    role: "owner" | "admin" | "member",
  ) => {
    if (!activeOrg) return;
    try {
      const updated = await updateMemberRole(activeOrg.id, memberId, role);
      setMembers((prev) => prev.map((m) => (m.id === memberId ? updated : m)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update role");
    }
  };

  const handleRemoveMember = async (memberId: string) => {
    if (!activeOrg) return;
    if (!confirm("Are you sure you want to remove this member?")) return;
    try {
      await removeMember(activeOrg.id, memberId);
      setMembers((prev) => prev.filter((m) => m.id !== memberId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove member");
    }
  };

  const handleCreateWorkspace = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeOrg || !newWorkspaceName || !newWorkspaceSlug) return;

    setIsCreatingWorkspace(true);
    try {
      const ws = await createWorkspace(activeOrg.id, {
        name: newWorkspaceName,
        slug: newWorkspaceSlug,
      });
      setWorkspacesList((prev) => [...prev, ws]);
      setNewWorkspaceName("");
      setNewWorkspaceSlug("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace");
    } finally {
      setIsCreatingWorkspace(false);
    }
  };

  const handleUpdateWorkspace = async (workspaceId: string) => {
    if (!activeOrg || !editWorkspaceName) return;
    try {
      const updated = await updateWorkspace(activeOrg.id, workspaceId, {
        name: editWorkspaceName,
      });
      setWorkspacesList((prev) =>
        prev.map((ws) => (ws.id === workspaceId ? updated : ws)),
      );
      setEditingWorkspace(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update workspace");
    }
  };

  const handleDeleteWorkspace = async (workspaceId: string) => {
    if (!activeOrg) return;
    if (!confirm("Are you sure you want to delete this workspace?")) return;
    try {
      await deleteWorkspace(activeOrg.id, workspaceId);
      setWorkspacesList((prev) => prev.filter((ws) => ws.id !== workspaceId));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete workspace");
    }
  };

  const handleDelete = async () => {
    if (!activeOrg || deleteConfirmText !== activeOrg.slug) return;
    try {
      await deleteOrganization(activeOrg.id);
      await refresh();
      onNavigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete organization");
    }
  };

  if (!activeOrg) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
        <p className="text-gray-500 dark:text-gray-400">No organization selected</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-500" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-gray-900">
      {/* Header */}
      <header className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <div className="mx-auto max-w-4xl flex items-center gap-4">
          <button
            onClick={() => onNavigate("/")}
            className="text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100"
          >
            ← Back to Dashboard
          </button>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">
            Organization Settings
          </h1>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-8">
        {error && (
          <div className="mb-4 rounded-md bg-red-50 dark:bg-red-900/20 p-4 text-red-700 dark:text-red-400">
            {error}
            <button onClick={() => setError(null)} className="ml-2 underline">
              Dismiss
            </button>
          </div>
        )}

        {/* Organization Details */}
        <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
          <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
            Organization Details
          </h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name
              </label>
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                disabled={!isAdmin}
                className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 disabled:opacity-50"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Description
              </label>
              <textarea
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                disabled={!isAdmin}
                rows={3}
                className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 disabled:opacity-50"
              />
            </div>
            <div className="text-sm text-gray-500 dark:text-gray-400">
              Slug:{" "}
              <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">
                {organization?.slug}
              </code>
            </div>
            {isAdmin && (
              <button
                onClick={handleSave}
                disabled={isSaving}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isSaving ? "Saving..." : "Save Changes"}
              </button>
            )}
          </div>
        </section>

        {/* Members */}
        <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
          <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
            Members ({members.length})
          </h2>
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {members.map((member) => (
              <div key={member.id} className="flex items-center justify-between py-3">
                <div>
                  <div className="font-medium text-gray-900 dark:text-gray-100">
                    {member.display_name || member.email}
                  </div>
                  <div className="text-sm text-gray-500 dark:text-gray-400">
                    {member.email}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {isOwner ? (
                    <select
                      value={member.role}
                      onChange={(e) =>
                        handleUpdateRole(
                          member.id,
                          e.target.value as "owner" | "admin" | "member",
                        )
                      }
                      className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-sm text-gray-900 dark:text-gray-100"
                    >
                      <option value="owner">Owner</option>
                      <option value="admin">Admin</option>
                      <option value="member">Member</option>
                    </select>
                  ) : (
                    <span className="rounded-full bg-gray-100 dark:bg-gray-700 px-2 py-1 text-xs text-gray-700 dark:text-gray-300">
                      {member.role}
                    </span>
                  )}
                  {isOwner && member.role !== "owner" && (
                    <button
                      onClick={() => handleRemoveMember(member.id)}
                      className="text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* Invites */}
        {isAdmin && (
          <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
            <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
              Pending Invites ({invites.filter((i) => i.status === "pending").length})
            </h2>

            {/* Invite form */}
            <form onSubmit={handleInvite} className="mb-4 flex gap-2">
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="Email address"
                required
                className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value as "admin" | "member")}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
              <button
                type="submit"
                disabled={isInviting}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isInviting ? "Sending..." : "Send Invite"}
              </button>
            </form>

            {/* Pending invites list */}
            <div className="divide-y divide-gray-200 dark:divide-gray-700">
              {invites
                .filter((i) => i.status === "pending")
                .map((invite) => (
                  <div
                    key={invite.id}
                    className="flex items-center justify-between py-3"
                  >
                    <div>
                      <div className="font-medium text-gray-900 dark:text-gray-100">
                        {invite.email}
                      </div>
                      <div className="text-sm text-gray-500 dark:text-gray-400">
                        Role: {invite.role}
                      </div>
                    </div>
                    <button
                      onClick={() => handleCancelInvite(invite.id)}
                      className="text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                    >
                      Cancel
                    </button>
                  </div>
                ))}
            </div>
          </section>
        )}

        {/* Workspaces */}
        <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
          <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
            Workspaces ({workspacesList.length})
          </h2>

          {/* Create workspace form */}
          {isAdmin && (
            <form onSubmit={handleCreateWorkspace} className="mb-4 flex gap-2">
              <input
                type="text"
                value={newWorkspaceName}
                onChange={(e) => {
                  setNewWorkspaceName(e.target.value);
                  // Auto-generate slug from name
                  setNewWorkspaceSlug(
                    e.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-|-$/g, ""),
                  );
                }}
                placeholder="Workspace name"
                required
                className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <input
                type="text"
                value={newWorkspaceSlug}
                onChange={(e) => setNewWorkspaceSlug(e.target.value)}
                placeholder="slug"
                required
                pattern="[a-z0-9-]+"
                className="w-32 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <button
                type="submit"
                disabled={isCreatingWorkspace}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isCreatingWorkspace ? "Creating..." : "Create"}
              </button>
            </form>
          )}

          {/* Workspaces list */}
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {workspacesList.map((ws) => (
              <div key={ws.id} className="flex items-center justify-between py-3">
                <div className="flex-1">
                  {editingWorkspace === ws.id ? (
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        value={editWorkspaceName}
                        onChange={(e) => setEditWorkspaceName(e.target.value)}
                        className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-gray-900 dark:text-gray-100"
                      />
                      <button
                        onClick={() => handleUpdateWorkspace(ws.id)}
                        className="text-blue-600 hover:text-blue-800 dark:text-blue-400"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingWorkspace(null)}
                        className="text-gray-500 hover:text-gray-700 dark:text-gray-400"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="font-medium text-gray-900 dark:text-gray-100">
                        {ws.name}
                      </div>
                      <div className="text-sm text-gray-500 dark:text-gray-400">
                        /{ws.slug}
                      </div>
                    </>
                  )}
                </div>
                {isAdmin && editingWorkspace !== ws.id && (
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => {
                        setEditingWorkspace(ws.id);
                        setEditWorkspaceName(ws.name);
                      }}
                      className="text-gray-600 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
                    >
                      Edit
                    </button>
                    {workspacesList.length > 1 && (
                      <button
                        onClick={() => handleDeleteWorkspace(ws.id)}
                        className="text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* Danger Zone */}
        {isOwner && (
          <section className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-6">
            <h2 className="mb-4 text-lg font-semibold text-red-800 dark:text-red-400">
              Danger Zone
            </h2>
            {!showDeleteConfirm ? (
              <button
                onClick={() => setShowDeleteConfirm(true)}
                className="rounded-md bg-red-600 px-4 py-2 text-white hover:bg-red-700"
              >
                Delete Organization
              </button>
            ) : (
              <div className="space-y-3">
                <p className="text-red-800 dark:text-red-400">
                  This will permanently delete the organization and all associated data.
                  Type{" "}
                  <code className="bg-red-100 dark:bg-red-800 px-1 rounded">
                    {activeOrg.slug}
                  </code>{" "}
                  to confirm.
                </p>
                <input
                  type="text"
                  value={deleteConfirmText}
                  onChange={(e) => setDeleteConfirmText(e.target.value)}
                  placeholder="Type organization slug"
                  className="w-full rounded-md border border-red-300 dark:border-red-600 bg-white dark:bg-gray-800 px-3 py-2 text-gray-900 dark:text-gray-100"
                />
                <div className="flex gap-2">
                  <button
                    onClick={handleDelete}
                    disabled={deleteConfirmText !== activeOrg.slug}
                    className="rounded-md bg-red-600 px-4 py-2 text-white hover:bg-red-700 disabled:opacity-50"
                  >
                    Delete Forever
                  </button>
                  <button
                    onClick={() => {
                      setShowDeleteConfirm(false);
                      setDeleteConfirmText("");
                    }}
                    className="rounded-md border border-gray-300 dark:border-gray-600 px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
