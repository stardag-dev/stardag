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
  fetchApiKeys,
  createApiKey,
  revokeApiKey,
  fetchTargetRoots,
  createTargetRoot,
  updateTargetRoot,
  deleteTargetRoot,
  type OrganizationDetail,
  type Member,
  type Invite,
  type Workspace,
  type ApiKey,
  type TargetRoot,
} from "../api/organizations";

interface OrganizationSettingsProps {
  onNavigate: (path: string) => void;
}

export function OrganizationSettings({ onNavigate }: OrganizationSettingsProps) {
  const { activeOrg, activeOrgRole, activeWorkspace, refresh } = useWorkspace();

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

  // API Keys state - keys grouped by workspace
  const [apiKeysByWorkspace, setApiKeysByWorkspace] = useState<Map<string, ApiKey[]>>(
    new Map(),
  );
  const [newKeyWorkspace, setNewKeyWorkspace] = useState<string>("");
  const [newKeyName, setNewKeyName] = useState("");
  const [isCreatingKey, setIsCreatingKey] = useState(false);
  const [newlyCreatedKey, setNewlyCreatedKey] = useState<string | null>(null);
  const [newlyCreatedKeyWorkspace, setNewlyCreatedKeyWorkspace] = useState<
    string | null
  >(null);
  const [copiedKey, setCopiedKey] = useState(false);

  // Target Roots state - grouped by workspace
  const [targetRootsByWorkspace, setTargetRootsByWorkspace] = useState<
    Map<string, TargetRoot[]>
  >(new Map());
  const [newRootWorkspace, setNewRootWorkspace] = useState<string>("");
  const [newRootName, setNewRootName] = useState("");
  const [newRootUriPrefix, setNewRootUriPrefix] = useState("");
  const [isCreatingRoot, setIsCreatingRoot] = useState(false);
  const [editingRoot, setEditingRoot] = useState<string | null>(null);
  const [editRootName, setEditRootName] = useState("");
  const [editRootUriPrefix, setEditRootUriPrefix] = useState("");

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

  // Initialize default workspace for new key/root creation
  useEffect(() => {
    if (workspacesList.length > 0) {
      const defaultWs = activeWorkspace?.id || workspacesList[0].id;
      if (!newKeyWorkspace) {
        setNewKeyWorkspace(defaultWs);
      }
      if (!newRootWorkspace) {
        setNewRootWorkspace(defaultWs);
      }
    }
  }, [workspacesList, activeWorkspace, newKeyWorkspace, newRootWorkspace]);

  // Load API keys for all workspaces
  useEffect(() => {
    async function loadAllApiKeys() {
      if (!activeOrg || !isAdmin || workspacesList.length === 0) return;
      try {
        const keysByWs = new Map<string, ApiKey[]>();
        await Promise.all(
          workspacesList.map(async (ws) => {
            const keys = await fetchApiKeys(activeOrg.id, ws.id);
            keysByWs.set(ws.id, keys);
          }),
        );
        setApiKeysByWorkspace(keysByWs);
      } catch (err) {
        console.error("Failed to load API keys:", err);
      }
    }
    loadAllApiKeys();
  }, [activeOrg, workspacesList, isAdmin]);

  // Load target roots for all workspaces
  useEffect(() => {
    async function loadAllTargetRoots() {
      if (!activeOrg || !isAdmin || workspacesList.length === 0) return;
      try {
        const rootsByWs = new Map<string, TargetRoot[]>();
        await Promise.all(
          workspacesList.map(async (ws) => {
            const roots = await fetchTargetRoots(activeOrg.id, ws.id);
            rootsByWs.set(ws.id, roots);
          }),
        );
        setTargetRootsByWorkspace(rootsByWs);
      } catch (err) {
        console.error("Failed to load target roots:", err);
      }
    }
    loadAllTargetRoots();
  }, [activeOrg, workspacesList, isAdmin]);

  const handleCreateApiKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeOrg || !newKeyWorkspace || !newKeyName) return;

    setIsCreatingKey(true);
    try {
      const result = await createApiKey(activeOrg.id, newKeyWorkspace, newKeyName);
      // Add the new key to the appropriate workspace's list
      setApiKeysByWorkspace((prev) => {
        const newMap = new Map(prev);
        const existingKeys = newMap.get(newKeyWorkspace) || [];
        newMap.set(newKeyWorkspace, [result, ...existingKeys]);
        return newMap;
      });
      setNewKeyName("");
      setNewlyCreatedKey(result.key);
      setNewlyCreatedKeyWorkspace(newKeyWorkspace);
      setCopiedKey(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create API key");
    } finally {
      setIsCreatingKey(false);
    }
  };

  const handleRevokeApiKey = async (workspaceId: string, keyId: string) => {
    if (!activeOrg) return;
    if (
      !confirm("Are you sure you want to revoke this API key? This cannot be undone.")
    )
      return;

    try {
      await revokeApiKey(activeOrg.id, workspaceId, keyId);
      setApiKeysByWorkspace((prev) => {
        const newMap = new Map(prev);
        const existingKeys = newMap.get(workspaceId) || [];
        newMap.set(
          workspaceId,
          existingKeys.filter((k) => k.id !== keyId),
        );
        return newMap;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke API key");
    }
  };

  const handleCopyKey = async () => {
    if (!newlyCreatedKey) return;
    try {
      await navigator.clipboard.writeText(newlyCreatedKey);
      setCopiedKey(true);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  // Target Root handlers
  const handleCreateTargetRoot = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeOrg || !newRootWorkspace || !newRootName || !newRootUriPrefix) return;

    setIsCreatingRoot(true);
    try {
      const result = await createTargetRoot(activeOrg.id, newRootWorkspace, {
        name: newRootName,
        uri_prefix: newRootUriPrefix,
      });
      setTargetRootsByWorkspace((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(newRootWorkspace) || [];
        newMap.set(newRootWorkspace, [result, ...existingRoots]);
        return newMap;
      });
      setNewRootName("");
      setNewRootUriPrefix("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create target root");
    } finally {
      setIsCreatingRoot(false);
    }
  };

  const handleUpdateTargetRoot = async (workspaceId: string, rootId: string) => {
    if (!activeOrg || !editRootName || !editRootUriPrefix) return;
    try {
      const updated = await updateTargetRoot(activeOrg.id, workspaceId, rootId, {
        name: editRootName,
        uri_prefix: editRootUriPrefix,
      });
      setTargetRootsByWorkspace((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(workspaceId) || [];
        newMap.set(
          workspaceId,
          existingRoots.map((r) => (r.id === rootId ? updated : r)),
        );
        return newMap;
      });
      setEditingRoot(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update target root");
    }
  };

  const handleDeleteTargetRoot = async (workspaceId: string, rootId: string) => {
    if (!activeOrg) return;
    if (!confirm("Are you sure you want to delete this target root?")) return;

    try {
      await deleteTargetRoot(activeOrg.id, workspaceId, rootId);
      setTargetRootsByWorkspace((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(workspaceId) || [];
        newMap.set(
          workspaceId,
          existingRoots.filter((r) => r.id !== rootId),
        );
        return newMap;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete target root");
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

        {/* API Keys */}
        {isAdmin && (
          <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
            <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
              API Keys
            </h2>
            <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
              API keys are used by the SDK to authenticate with the API. Each key is
              scoped to a specific workspace.
            </p>

            {/* Create key form */}
            <form
              onSubmit={handleCreateApiKey}
              className="mb-6 flex flex-wrap gap-2 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
            >
              <select
                value={newKeyWorkspace}
                onChange={(e) => setNewKeyWorkspace(e.target.value)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              >
                {workspacesList.map((ws) => (
                  <option key={ws.id} value={ws.id}>
                    {ws.name}
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={newKeyName}
                onChange={(e) => setNewKeyName(e.target.value)}
                placeholder="Key name (e.g., 'CI/CD Pipeline')"
                required
                className="flex-1 min-w-48 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <button
                type="submit"
                disabled={isCreatingKey}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isCreatingKey ? "Creating..." : "Create Key"}
              </button>
            </form>

            {/* Newly created key modal */}
            {newlyCreatedKey && (
              <div className="mb-6 rounded-md bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 p-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <h3 className="text-sm font-medium text-green-800 dark:text-green-200">
                      API Key Created for{" "}
                      {workspacesList.find((ws) => ws.id === newlyCreatedKeyWorkspace)
                        ?.name || "workspace"}
                    </h3>
                    <p className="mt-1 text-sm text-green-700 dark:text-green-300">
                      Copy this key now. You won't be able to see it again!
                    </p>
                    <div className="mt-2 flex items-center gap-2">
                      <code className="flex-1 rounded bg-green-100 dark:bg-green-800 px-2 py-1 text-sm font-mono text-green-900 dark:text-green-100 break-all">
                        {newlyCreatedKey}
                      </code>
                      <button
                        onClick={handleCopyKey}
                        className="rounded-md bg-green-600 px-3 py-1 text-white hover:bg-green-700"
                      >
                        {copiedKey ? "Copied!" : "Copy"}
                      </button>
                    </div>
                  </div>
                  <button
                    onClick={() => {
                      setNewlyCreatedKey(null);
                      setNewlyCreatedKeyWorkspace(null);
                    }}
                    className="ml-2 text-green-600 hover:text-green-800 dark:text-green-400"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}

            {/* Keys grouped by workspace */}
            <div className="space-y-6">
              {workspacesList.map((ws) => {
                const keys = apiKeysByWorkspace.get(ws.id) || [];
                return (
                  <div key={ws.id}>
                    {/* Workspace header */}
                    <div className="flex items-center gap-2 mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">
                        {ws.name}
                      </h3>
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        /{ws.slug}
                      </span>
                      <div className="flex-1 border-t border-gray-200 dark:border-gray-700 ml-2" />
                    </div>

                    {/* Keys for this workspace */}
                    {keys.length === 0 ? (
                      <p className="py-2 text-sm text-gray-500 dark:text-gray-400 italic">
                        No API keys for this workspace
                      </p>
                    ) : (
                      <div className="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-md">
                        {keys.map((key) => (
                          <div
                            key={key.id}
                            className="flex items-center justify-between py-3 px-4"
                          >
                            <div>
                              <div className="font-medium text-gray-900 dark:text-gray-100">
                                {key.name}
                              </div>
                              <div className="text-sm text-gray-500 dark:text-gray-400">
                                <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">
                                  sk_{key.key_prefix}...
                                </code>
                                {key.last_used_at && (
                                  <span className="ml-2">
                                    Last used:{" "}
                                    {new Date(key.last_used_at).toLocaleDateString()}
                                  </span>
                                )}
                                {!key.last_used_at && (
                                  <span className="ml-2">Never used</span>
                                )}
                              </div>
                            </div>
                            <button
                              onClick={() => handleRevokeApiKey(ws.id, key.id)}
                              className="text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                            >
                              Revoke
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* Target Roots Section */}
        {isAdmin && (
          <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
            <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
              Target Roots
            </h2>
            <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
              Target roots define where task outputs are stored. These are shared across
              all users in a workspace.
            </p>

            {/* Create new target root form */}
            <form
              onSubmit={handleCreateTargetRoot}
              className="mb-6 flex flex-wrap gap-2 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
            >
              <select
                value={newRootWorkspace}
                onChange={(e) => setNewRootWorkspace(e.target.value)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              >
                {workspacesList.map((ws) => (
                  <option key={ws.id} value={ws.id}>
                    {ws.name}
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={newRootName}
                onChange={(e) => setNewRootName(e.target.value)}
                placeholder="Name (e.g., 'default')"
                required
                className="w-32 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <input
                type="text"
                value={newRootUriPrefix}
                onChange={(e) => setNewRootUriPrefix(e.target.value)}
                placeholder="URI prefix (e.g., 's3://bucket/path/')"
                required
                className="flex-1 min-w-64 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <button
                type="submit"
                disabled={isCreatingRoot}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isCreatingRoot ? "Creating..." : "Add Root"}
              </button>
            </form>

            {/* Target roots grouped by workspace */}
            <div className="space-y-6">
              {workspacesList.map((ws) => {
                const roots = targetRootsByWorkspace.get(ws.id) || [];
                return (
                  <div key={ws.id}>
                    {/* Workspace header */}
                    <div className="flex items-center gap-2 mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">
                        {ws.name}
                      </h3>
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        /{ws.slug}
                      </span>
                      <div className="flex-1 border-t border-gray-200 dark:border-gray-700 ml-2" />
                    </div>

                    {/* Roots for this workspace */}
                    {roots.length === 0 ? (
                      <p className="py-2 text-sm text-gray-500 dark:text-gray-400 italic">
                        No target roots configured
                      </p>
                    ) : (
                      <div className="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-md">
                        {roots.map((root) => (
                          <div key={root.id} className="py-3 px-4">
                            {editingRoot === root.id ? (
                              <div className="flex flex-wrap gap-2 items-center">
                                <input
                                  type="text"
                                  value={editRootName}
                                  onChange={(e) => setEditRootName(e.target.value)}
                                  className="w-32 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-sm text-gray-900 dark:text-gray-100"
                                />
                                <input
                                  type="text"
                                  value={editRootUriPrefix}
                                  onChange={(e) => setEditRootUriPrefix(e.target.value)}
                                  className="flex-1 min-w-48 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-sm text-gray-900 dark:text-gray-100"
                                />
                                <button
                                  onClick={() => handleUpdateTargetRoot(ws.id, root.id)}
                                  className="text-blue-600 hover:text-blue-800 dark:text-blue-400 text-sm"
                                >
                                  Save
                                </button>
                                <button
                                  onClick={() => setEditingRoot(null)}
                                  className="text-gray-600 hover:text-gray-800 dark:text-gray-400 text-sm"
                                >
                                  Cancel
                                </button>
                              </div>
                            ) : (
                              <div className="flex items-center justify-between">
                                <div>
                                  <div className="font-medium text-gray-900 dark:text-gray-100">
                                    {root.name}
                                  </div>
                                  <div className="text-sm text-gray-500 dark:text-gray-400">
                                    <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded break-all">
                                      {root.uri_prefix}
                                    </code>
                                  </div>
                                </div>
                                <div className="flex gap-2">
                                  <button
                                    onClick={() => {
                                      setEditingRoot(root.id);
                                      setEditRootName(root.name);
                                      setEditRootUriPrefix(root.uri_prefix);
                                    }}
                                    className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                                  >
                                    Edit
                                  </button>
                                  <button
                                    onClick={() =>
                                      handleDeleteTargetRoot(ws.id, root.id)
                                    }
                                    className="text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                                  >
                                    Delete
                                  </button>
                                </div>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

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
