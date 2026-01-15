import { useState, useEffect } from "react";
import { useEnvironment } from "../context/EnvironmentContext";
import {
  fetchWorkspace,
  fetchMembers,
  fetchInvites,
  fetchEnvironments,
  updateWorkspace,
  createInvite,
  cancelInvite,
  updateMemberRole,
  removeMember,
  deleteWorkspace,
  createEnvironment,
  updateEnvironment,
  deleteEnvironment,
  fetchApiKeys,
  createApiKey,
  revokeApiKey,
  fetchTargetRoots,
  createTargetRoot,
  updateTargetRoot,
  deleteTargetRoot,
  type WorkspaceDetail,
  type Member,
  type Invite,
  type Environment,
  type ApiKey,
  type TargetRoot,
} from "../api/workspaces";

interface WorkspaceSettingsProps {
  onNavigate: (path: string) => void;
}

export function WorkspaceSettings({ onNavigate }: WorkspaceSettingsProps) {
  const { activeWorkspace, activeWorkspaceRole, activeEnvironment, refresh } =
    useEnvironment();

  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [environmentsList, setEnvironmentsList] = useState<Environment[]>([]);
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

  // Environment form state
  const [newEnvironmentName, setNewEnvironmentName] = useState("");
  const [newEnvironmentSlug, setNewEnvironmentSlug] = useState("");
  const [isCreatingEnvironment, setIsCreatingEnvironment] = useState(false);
  const [editingEnvironment, setEditingEnvironment] = useState<string | null>(null);
  const [editEnvironmentName, setEditEnvironmentName] = useState("");

  // API Keys state - keys grouped by environment
  const [apiKeysByEnvironment, setApiKeysByEnvironment] = useState<
    Map<string, ApiKey[]>
  >(new Map());
  const [newKeyEnvironment, setNewKeyEnvironment] = useState<string>("");
  const [newKeyName, setNewKeyName] = useState("");
  const [isCreatingKey, setIsCreatingKey] = useState(false);
  const [newlyCreatedKey, setNewlyCreatedKey] = useState<string | null>(null);
  const [newlyCreatedKeyEnvironment, setNewlyCreatedKeyEnvironment] = useState<
    string | null
  >(null);
  const [copiedKey, setCopiedKey] = useState(false);

  // Target Roots state - grouped by environment
  const [targetRootsByEnvironment, setTargetRootsByEnvironment] = useState<
    Map<string, TargetRoot[]>
  >(new Map());
  const [newRootEnvironment, setNewRootEnvironment] = useState<string>("");
  const [newRootName, setNewRootName] = useState("");
  const [newRootUriPrefix, setNewRootUriPrefix] = useState("");
  const [isCreatingRoot, setIsCreatingRoot] = useState(false);
  const [editingRoot, setEditingRoot] = useState<string | null>(null);
  const [editRootName, setEditRootName] = useState("");
  const [editRootUriPrefix, setEditRootUriPrefix] = useState("");

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  // Personal environments collapsed state
  const [showPersonalEnvironments, setShowPersonalEnvironments] = useState(false);

  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";
  const isOwner = activeWorkspaceRole === "owner";

  useEffect(() => {
    async function loadData() {
      if (!activeWorkspace) return;

      setLoading(true);
      setError(null);
      try {
        const [ws, membersList, invitesList, envList] = await Promise.all([
          fetchWorkspace(activeWorkspace.id),
          fetchMembers(activeWorkspace.id),
          isAdmin ? fetchInvites(activeWorkspace.id) : Promise.resolve([]),
          fetchEnvironments(activeWorkspace.id),
        ]);
        setWorkspace(ws);
        setMembers(membersList);
        setInvites(invitesList);
        setEnvironmentsList(envList);
        setEditName(ws.name);
        setEditDescription(ws.description || "");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load workspace");
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [activeWorkspace, isAdmin]);

  const handleSave = async () => {
    if (!activeWorkspace) return;

    setIsSaving(true);
    try {
      await updateWorkspace(activeWorkspace.id, {
        name: editName,
        description: editDescription || undefined,
      });
      await refresh();
      setWorkspace((prev) =>
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
    if (!activeWorkspace || !inviteEmail) return;

    setIsInviting(true);
    try {
      const newInvite = await createInvite(activeWorkspace.id, {
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
    if (!activeWorkspace) return;
    try {
      await cancelInvite(activeWorkspace.id, inviteId);
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel invite");
    }
  };

  const handleUpdateRole = async (
    memberId: string,
    role: "owner" | "admin" | "member",
  ) => {
    if (!activeWorkspace) return;
    try {
      const updated = await updateMemberRole(activeWorkspace.id, memberId, role);
      setMembers((prev) => prev.map((m) => (m.id === memberId ? updated : m)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update role");
    }
  };

  const handleRemoveMember = async (memberId: string) => {
    if (!activeWorkspace) return;
    if (!confirm("Are you sure you want to remove this member?")) return;
    try {
      await removeMember(activeWorkspace.id, memberId);
      setMembers((prev) => prev.filter((m) => m.id !== memberId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove member");
    }
  };

  const handleCreateEnvironment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeWorkspace || !newEnvironmentName || !newEnvironmentSlug) return;

    setIsCreatingEnvironment(true);
    try {
      const env = await createEnvironment(activeWorkspace.id, {
        name: newEnvironmentName,
        slug: newEnvironmentSlug,
      });
      setEnvironmentsList((prev) => [...prev, env]);
      setNewEnvironmentName("");
      setNewEnvironmentSlug("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create environment");
    } finally {
      setIsCreatingEnvironment(false);
    }
  };

  const handleUpdateEnvironment = async (environmentId: string) => {
    if (!activeWorkspace || !editEnvironmentName) return;
    try {
      const updated = await updateEnvironment(activeWorkspace.id, environmentId, {
        name: editEnvironmentName,
      });
      setEnvironmentsList((prev) =>
        prev.map((env) => (env.id === environmentId ? updated : env)),
      );
      setEditingEnvironment(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update environment");
    }
  };

  const handleDeleteEnvironment = async (environmentId: string) => {
    if (!activeWorkspace) return;
    if (!confirm("Are you sure you want to delete this environment?")) return;
    try {
      await deleteEnvironment(activeWorkspace.id, environmentId);
      setEnvironmentsList((prev) => prev.filter((env) => env.id !== environmentId));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete environment");
    }
  };

  const handleDelete = async () => {
    if (!activeWorkspace || deleteConfirmText !== activeWorkspace.slug) return;
    try {
      await deleteWorkspace(activeWorkspace.id);
      await refresh();
      onNavigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete workspace");
    }
  };

  // Initialize default environment for new key/root creation
  useEffect(() => {
    if (environmentsList.length > 0) {
      const defaultEnv = activeEnvironment?.id || environmentsList[0].id;
      if (!newKeyEnvironment) {
        setNewKeyEnvironment(defaultEnv);
      }
      if (!newRootEnvironment) {
        setNewRootEnvironment(defaultEnv);
      }
    }
  }, [environmentsList, activeEnvironment, newKeyEnvironment, newRootEnvironment]);

  // Load API keys for all environments
  useEffect(() => {
    async function loadAllApiKeys() {
      if (!activeWorkspace || !isAdmin || environmentsList.length === 0) return;
      try {
        const keysByEnv = new Map<string, ApiKey[]>();
        await Promise.all(
          environmentsList.map(async (env) => {
            const keys = await fetchApiKeys(activeWorkspace.id, env.id);
            keysByEnv.set(env.id, keys);
          }),
        );
        setApiKeysByEnvironment(keysByEnv);
      } catch (err) {
        console.error("Failed to load API keys:", err);
      }
    }
    loadAllApiKeys();
  }, [activeWorkspace, environmentsList, isAdmin]);

  // Load target roots for all environments
  useEffect(() => {
    async function loadAllTargetRoots() {
      if (!activeWorkspace || !isAdmin || environmentsList.length === 0) return;
      try {
        const rootsByEnv = new Map<string, TargetRoot[]>();
        await Promise.all(
          environmentsList.map(async (env) => {
            const roots = await fetchTargetRoots(activeWorkspace.id, env.id);
            rootsByEnv.set(env.id, roots);
          }),
        );
        setTargetRootsByEnvironment(rootsByEnv);
      } catch (err) {
        console.error("Failed to load target roots:", err);
      }
    }
    loadAllTargetRoots();
  }, [activeWorkspace, environmentsList, isAdmin]);

  const handleCreateApiKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeWorkspace || !newKeyEnvironment || !newKeyName) return;

    setIsCreatingKey(true);
    try {
      const result = await createApiKey(
        activeWorkspace.id,
        newKeyEnvironment,
        newKeyName,
      );
      // Add the new key to the appropriate environment's list
      setApiKeysByEnvironment((prev) => {
        const newMap = new Map(prev);
        const existingKeys = newMap.get(newKeyEnvironment) || [];
        newMap.set(newKeyEnvironment, [result, ...existingKeys]);
        return newMap;
      });
      setNewKeyName("");
      setNewlyCreatedKey(result.key);
      setNewlyCreatedKeyEnvironment(newKeyEnvironment);
      setCopiedKey(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create API key");
    } finally {
      setIsCreatingKey(false);
    }
  };

  const handleRevokeApiKey = async (environmentId: string, keyId: string) => {
    if (!activeWorkspace) return;
    if (
      !confirm("Are you sure you want to revoke this API key? This cannot be undone.")
    )
      return;

    try {
      await revokeApiKey(activeWorkspace.id, environmentId, keyId);
      setApiKeysByEnvironment((prev) => {
        const newMap = new Map(prev);
        const existingKeys = newMap.get(environmentId) || [];
        newMap.set(
          environmentId,
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
    if (!activeWorkspace || !newRootEnvironment || !newRootName || !newRootUriPrefix)
      return;

    setIsCreatingRoot(true);
    try {
      const result = await createTargetRoot(activeWorkspace.id, newRootEnvironment, {
        name: newRootName,
        uri_prefix: newRootUriPrefix,
      });
      setTargetRootsByEnvironment((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(newRootEnvironment) || [];
        newMap.set(newRootEnvironment, [result, ...existingRoots]);
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

  const handleUpdateTargetRoot = async (environmentId: string, rootId: string) => {
    if (!activeWorkspace || !editRootName || !editRootUriPrefix) return;
    try {
      const updated = await updateTargetRoot(
        activeWorkspace.id,
        environmentId,
        rootId,
        {
          name: editRootName,
          uri_prefix: editRootUriPrefix,
        },
      );
      setTargetRootsByEnvironment((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(environmentId) || [];
        newMap.set(
          environmentId,
          existingRoots.map((r) => (r.id === rootId ? updated : r)),
        );
        return newMap;
      });
      setEditingRoot(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update target root");
    }
  };

  const handleDeleteTargetRoot = async (environmentId: string, rootId: string) => {
    if (!activeWorkspace) return;
    if (!confirm("Are you sure you want to delete this target root?")) return;

    try {
      await deleteTargetRoot(activeWorkspace.id, environmentId, rootId);
      setTargetRootsByEnvironment((prev) => {
        const newMap = new Map(prev);
        const existingRoots = newMap.get(environmentId) || [];
        newMap.set(
          environmentId,
          existingRoots.filter((r) => r.id !== rootId),
        );
        return newMap;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete target root");
    }
  };

  if (!activeWorkspace) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
        <p className="text-gray-500 dark:text-gray-400">No workspace selected</p>
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
            Workspace Settings
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

        {/* Workspace Details */}
        <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
          <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
            Workspace Details
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
                {workspace?.slug}
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

        {/* Environments */}
        <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
          <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
            Environments ({environmentsList.filter((env) => !env.owner_id).length})
          </h2>

          {/* Create environment form */}
          {isAdmin && (
            <form onSubmit={handleCreateEnvironment} className="mb-4 flex gap-2">
              <input
                type="text"
                value={newEnvironmentName}
                onChange={(e) => {
                  setNewEnvironmentName(e.target.value);
                  // Auto-generate slug from name
                  setNewEnvironmentSlug(
                    e.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-|-$/g, ""),
                  );
                }}
                placeholder="Environment name"
                required
                className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <input
                type="text"
                value={newEnvironmentSlug}
                onChange={(e) => setNewEnvironmentSlug(e.target.value)}
                placeholder="slug"
                required
                pattern="[a-z0-9-]+"
                className="w-32 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              />
              <button
                type="submit"
                disabled={isCreatingEnvironment}
                className="rounded-md bg-blue-600 px-4 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {isCreatingEnvironment ? "Creating..." : "Create"}
              </button>
            </form>
          )}

          {/* Shared environments list */}
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {environmentsList
              .filter((env) => !env.owner_id)
              .map((env) => (
                <div key={env.id} className="flex items-center justify-between py-3">
                  <div className="flex-1">
                    {editingEnvironment === env.id ? (
                      <div className="flex items-center gap-2">
                        <input
                          type="text"
                          value={editEnvironmentName}
                          onChange={(e) => setEditEnvironmentName(e.target.value)}
                          className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-gray-900 dark:text-gray-100"
                        />
                        <button
                          onClick={() => handleUpdateEnvironment(env.id)}
                          className="text-blue-600 hover:text-blue-800 dark:text-blue-400"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => setEditingEnvironment(null)}
                          className="text-gray-500 hover:text-gray-700 dark:text-gray-400"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {env.name}
                        </div>
                        <div className="text-sm text-gray-500 dark:text-gray-400">
                          /{env.slug}
                        </div>
                      </>
                    )}
                  </div>
                  {isAdmin && editingEnvironment !== env.id && (
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => {
                          setEditingEnvironment(env.id);
                          setEditEnvironmentName(env.name);
                        }}
                        className="text-gray-600 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
                      >
                        Edit
                      </button>
                      {environmentsList.filter((e) => !e.owner_id).length > 1 && (
                        <button
                          onClick={() => handleDeleteEnvironment(env.id)}
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

          {/* Personal environments (collapsed) */}
          {environmentsList.filter((env) => env.owner_id).length > 0 && (
            <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4">
              <button
                onClick={() => setShowPersonalEnvironments(!showPersonalEnvironments)}
                className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
              >
                <span
                  className={`transform transition-transform ${
                    showPersonalEnvironments ? "rotate-90" : ""
                  }`}
                >
                  ▶
                </span>
                Personal environments (
                {environmentsList.filter((env) => env.owner_id).length})
              </button>
              {showPersonalEnvironments && (
                <div className="mt-2 ml-4 divide-y divide-gray-200 dark:divide-gray-700">
                  {environmentsList
                    .filter((env) => env.owner_id)
                    .map((env) => (
                      <div
                        key={env.id}
                        className="flex items-center justify-between py-2"
                      >
                        <div className="flex-1">
                          <div className="font-medium text-gray-900 dark:text-gray-100 text-sm">
                            {env.name}
                          </div>
                          <div className="text-xs text-gray-500 dark:text-gray-400">
                            /{env.slug}
                          </div>
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}
        </section>

        {/* API Keys */}
        {isAdmin && (
          <section className="mb-8 rounded-lg bg-white dark:bg-gray-800 p-6 shadow">
            <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
              API Keys
            </h2>
            <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
              API keys are used by the SDK to authenticate with the API. Each key is
              scoped to a specific environment.
            </p>

            {/* Create key form */}
            <form
              onSubmit={handleCreateApiKey}
              className="mb-6 flex flex-wrap gap-2 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
            >
              <select
                value={newKeyEnvironment}
                onChange={(e) => setNewKeyEnvironment(e.target.value)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              >
                {environmentsList.map((env) => (
                  <option key={env.id} value={env.id}>
                    {env.name}
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
                      {environmentsList.find(
                        (env) => env.id === newlyCreatedKeyEnvironment,
                      )?.name || "environment"}
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
                      setNewlyCreatedKeyEnvironment(null);
                    }}
                    className="ml-2 text-green-600 hover:text-green-800 dark:text-green-400"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}

            {/* Keys grouped by environment */}
            <div className="space-y-6">
              {environmentsList.map((env) => {
                const keys = apiKeysByEnvironment.get(env.id) || [];
                return (
                  <div key={env.id}>
                    {/* Environment header */}
                    <div className="flex items-center gap-2 mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">
                        {env.name}
                      </h3>
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        /{env.slug}
                      </span>
                      <div className="flex-1 border-t border-gray-200 dark:border-gray-700 ml-2" />
                    </div>

                    {/* Keys for this environment */}
                    {keys.length === 0 ? (
                      <p className="py-2 text-sm text-gray-500 dark:text-gray-400 italic">
                        No API keys for this environment
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
                              onClick={() => handleRevokeApiKey(env.id, key.id)}
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
              all users in an environment.
            </p>

            {/* Create new target root form */}
            <form
              onSubmit={handleCreateTargetRoot}
              className="mb-6 flex flex-wrap gap-2 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
            >
              <select
                value={newRootEnvironment}
                onChange={(e) => setNewRootEnvironment(e.target.value)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
              >
                {environmentsList.map((env) => (
                  <option key={env.id} value={env.id}>
                    {env.name}
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

            {/* Target roots grouped by environment */}
            <div className="space-y-6">
              {environmentsList.map((env) => {
                const roots = targetRootsByEnvironment.get(env.id) || [];
                return (
                  <div key={env.id}>
                    {/* Environment header */}
                    <div className="flex items-center gap-2 mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">
                        {env.name}
                      </h3>
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        /{env.slug}
                      </span>
                      <div className="flex-1 border-t border-gray-200 dark:border-gray-700 ml-2" />
                    </div>

                    {/* Roots for this environment */}
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
                                  onClick={() =>
                                    handleUpdateTargetRoot(env.id, root.id)
                                  }
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
                                      handleDeleteTargetRoot(env.id, root.id)
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
                Delete Workspace
              </button>
            ) : (
              <div className="space-y-3">
                <p className="text-red-800 dark:text-red-400">
                  This will permanently delete the workspace and all associated data.
                  Type{" "}
                  <code className="bg-red-100 dark:bg-red-800 px-1 rounded">
                    {activeWorkspace.slug}
                  </code>{" "}
                  to confirm.
                </p>
                <input
                  type="text"
                  value={deleteConfirmText}
                  onChange={(e) => setDeleteConfirmText(e.target.value)}
                  placeholder="Type workspace slug"
                  className="w-full rounded-md border border-red-300 dark:border-red-600 bg-white dark:bg-gray-800 px-3 py-2 text-gray-900 dark:text-gray-100"
                />
                <div className="flex gap-2">
                  <button
                    onClick={handleDelete}
                    disabled={deleteConfirmText !== activeWorkspace.slug}
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
