import { useState } from "react";
import {
  createWorkspace,
  createTargetRoot,
  createInvite,
  fetchEnvironments,
} from "../api/workspaces";
import { useEnvironment } from "../context/EnvironmentContext";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
import { UserMenu } from "./UserMenu";

interface CreateWorkspaceProps {
  onNavigate: (path: string) => void;
}

interface TargetRootEntry {
  name: string;
  uri: string;
}

interface InviteEntry {
  email: string;
  role: "admin" | "member";
}

export function CreateWorkspace({ onNavigate }: CreateWorkspaceProps) {
  const { refresh } = useEnvironment();
  const [step, setStep] = useState(1);

  // Step 1: Workspace details
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");

  // Step 2: Environment details
  const [environmentName, setEnvironmentName] = useState("main");
  const [environmentSlug, setEnvironmentSlug] = useState("main");

  // Step 3: Target roots (multiple)
  const [targetRoots, setTargetRoots] = useState<TargetRootEntry[]>([
    { name: "default", uri: "" },
  ]);
  const [skipTargetRoots, setSkipTargetRoots] = useState(false);

  // Step 4: Invites (multiple)
  const [invites, setInvites] = useState<InviteEntry[]>([]);
  const [newInviteEmail, setNewInviteEmail] = useState("");
  const [newInviteRole, setNewInviteRole] = useState<"admin" | "member">("member");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-generate slug from name
  const handleNameChange = (value: string) => {
    setName(value);
    // Only auto-generate if slug hasn't been manually edited
    if (!slug || slug === generateSlug(name)) {
      setSlug(generateSlug(value));
    }
  };

  // Auto-generate environment slug from name
  const handleEnvironmentNameChange = (value: string) => {
    setEnvironmentName(value);
    if (!environmentSlug || environmentSlug === generateSlug(environmentName)) {
      setEnvironmentSlug(generateSlug(value));
    }
  };

  const generateSlug = (text: string) => {
    return text
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "-")
      .replace(/^[-_]+|[-_]+$/g, "")
      .slice(0, 64);
  };

  const totalSteps = 4;

  const canProceedToStep2 = name.trim() && slug.trim();
  const canProceedToStep3 = environmentName.trim() && environmentSlug.trim();
  // Next requires at least one complete target root (use "Setup later" to skip)
  const canProceedToStep4 = targetRoots.some((tr) => tr.name.trim() && tr.uri.trim());

  // Target root management
  const updateTargetRoot = (index: number, field: "name" | "uri", value: string) => {
    setTargetRoots((prev) =>
      prev.map((tr, i) => (i === index ? { ...tr, [field]: value } : tr)),
    );
  };

  const addTargetRoot = () => {
    setTargetRoots((prev) => [...prev, { name: "", uri: "" }]);
  };

  const removeTargetRoot = (index: number) => {
    setTargetRoots((prev) => prev.filter((_, i) => i !== index));
  };

  // Invite management
  const addInvite = () => {
    if (!newInviteEmail.trim()) return;
    setInvites((prev) => [
      ...prev,
      { email: newInviteEmail.trim(), role: newInviteRole },
    ]);
    setNewInviteEmail("");
    setNewInviteRole("member");
  };

  const removeInvite = (index: number) => {
    setInvites((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const workspaceSlug = slug.trim();
    try {
      setLoading(true);
      setError(null);

      // Step 1: Create workspace with first target root (if any)
      const firstTargetRoot =
        !skipTargetRoots && targetRoots.length > 0 && targetRoots[0].uri.trim()
          ? targetRoots[0]
          : null;

      const workspace = await createWorkspace({
        name: name.trim(),
        slug: workspaceSlug,
        description: description.trim() || undefined,
        initial_environment_name: environmentName.trim(),
        initial_environment_slug: environmentSlug.trim(),
        initial_target_root_name: firstTargetRoot?.name.trim(),
        initial_target_root_uri: firstTargetRoot?.uri.trim(),
      });

      // Step 2: Create additional target roots (if any)
      if (!skipTargetRoots && targetRoots.length > 1) {
        // Fetch the environment ID
        const environments = await fetchEnvironments(workspace.id);
        const env = environments[0]; // We just created one environment

        if (env) {
          // Create remaining target roots (skip first one, already created)
          for (let i = 1; i < targetRoots.length; i++) {
            const tr = targetRoots[i];
            if (tr.name.trim() && tr.uri.trim()) {
              await createTargetRoot(workspace.id, env.id, {
                name: tr.name.trim(),
                uri_prefix: tr.uri.trim(),
              });
            }
          }
        }
      }

      // Step 3: Send invites (if any)
      for (const invite of invites) {
        try {
          await createInvite(workspace.id, {
            email: invite.email,
            role: invite.role,
          });
        } catch (inviteErr) {
          // Log but don't fail workspace creation for invite errors
          console.warn("Failed to send invite to", invite.email, inviteErr);
        }
      }

      // Refresh environment context and select the newly created workspace
      await refresh(workspaceSlug);
      // Navigate to dashboard with new workspace in URL
      onNavigate(`/${workspaceSlug}`);
    } catch (err) {
      console.error("Failed to create workspace:", err);
      setError(err instanceof Error ? err.message : "Failed to create workspace");
    } finally {
      setLoading(false);
    }
  };

  const handleBack = () => {
    // Reset skip flag when going back from step 4 to step 3
    if (step === 4) {
      setSkipTargetRoots(false);
    }
    setStep(step - 1);
  };

  const handleSkipTargetRoots = () => {
    setSkipTargetRoots(true);
    setStep(4);
  };

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-gray-900">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <div className="flex items-center gap-4">
          <button
            onClick={() => onNavigate("/")}
            className="text-gray-900 dark:text-gray-100 hover:text-blue-600 dark:hover:text-blue-400"
          >
            <Logo size="md" />
          </button>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>

      {/* Content */}
      <main className="mx-auto max-w-lg px-4 py-8">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-2">
          Create Workspace
        </h1>

        {/* Progress indicator */}
        <div className="flex items-center gap-2 mb-6">
          {[1, 2, 3, 4].map((s) => (
            <div key={s} className="flex items-center">
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${
                  s === step
                    ? "bg-blue-600 text-white"
                    : s < step
                      ? "bg-green-500 text-white"
                      : "bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400"
                }`}
              >
                {s < step ? "✓" : s}
              </div>
              {s < totalSteps && (
                <div
                  className={`w-8 h-0.5 ${
                    s < step ? "bg-green-500" : "bg-gray-200 dark:bg-gray-700"
                  }`}
                />
              )}
            </div>
          ))}
          <span className="ml-2 text-sm text-gray-500 dark:text-gray-400">
            {step === 1 && "Workspace Details"}
            {step === 2 && "Environment Setup"}
            {step === 3 && "Target Roots"}
            {step === 4 && "Collaborate"}
          </span>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300">
              {error}
            </div>
          )}

          {/* Step 1: Workspace Details */}
          {step === 1 && (
            <>
              <div>
                <label
                  htmlFor="name"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Workspace Name
                </label>
                <input
                  type="text"
                  id="name"
                  value={name}
                  onChange={(e) => handleNameChange(e.target.value)}
                  placeholder="My Workspace"
                  required
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>

              <div>
                <label
                  htmlFor="slug"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  URL Slug
                </label>
                <input
                  type="text"
                  id="slug"
                  value={slug}
                  onChange={(e) =>
                    setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))
                  }
                  placeholder="my-workspace"
                  required
                  pattern="^[a-z0-9][a-z0-9_-]*[a-z0-9]$|^[a-z0-9]$"
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Lowercase letters, numbers, hyphens, and underscores only
                </p>
              </div>

              <div>
                <label
                  htmlFor="description"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Description (optional)
                </label>
                <textarea
                  id="description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="What is this workspace for?"
                  rows={3}
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>
            </>
          )}

          {/* Step 2: Environment Setup */}
          {step === 2 && (
            <>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Set up your first environment. Common choices: &quot;main&quot;,
                &quot;production&quot;, &quot;development&quot;.
              </p>

              <div>
                <label
                  htmlFor="envName"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Environment Name
                </label>
                <input
                  type="text"
                  id="envName"
                  value={environmentName}
                  onChange={(e) => handleEnvironmentNameChange(e.target.value)}
                  placeholder="main"
                  required
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>

              <div>
                <label
                  htmlFor="envSlug"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Environment Slug
                </label>
                <input
                  type="text"
                  id="envSlug"
                  value={environmentSlug}
                  onChange={(e) =>
                    setEnvironmentSlug(
                      e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""),
                    )
                  }
                  placeholder="main"
                  required
                  pattern="^[a-z0-9][a-z0-9_-]*[a-z0-9]$|^[a-z0-9]$"
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>

              <p className="text-xs text-gray-500 dark:text-gray-400 mt-4">
                You can create additional environments later in workspace settings.
              </p>
            </>
          )}

          {/* Step 3: Target Roots */}
          {step === 3 && (
            <>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Configure where task outputs will be stored. This is typically an S3/GCS
                bucket or Modal volume. Read more in the{" "}
                <a
                  href="https://docs.stardag.com/stardag/concepts/targets/#target-roots"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 underline"
                >
                  documentation
                </a>
                .
              </p>

              <div className="space-y-4">
                {targetRoots.map((tr, index) => (
                  <div
                    key={index}
                    className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg space-y-3"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                        Target Root {index + 1}
                      </span>
                      {targetRoots.length > 1 && (
                        <button
                          type="button"
                          onClick={() => removeTargetRoot(index)}
                          className="text-sm text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                        >
                          Remove
                        </button>
                      )}
                    </div>

                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        Name
                      </label>
                      <input
                        type="text"
                        value={tr.name}
                        onChange={(e) =>
                          updateTargetRoot(index, "name", e.target.value)
                        }
                        placeholder="default"
                        className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                      />
                    </div>

                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        URI Prefix
                      </label>
                      <input
                        type="text"
                        value={tr.uri}
                        onChange={(e) => updateTargetRoot(index, "uri", e.target.value)}
                        placeholder="s3://bucket/prefix/ or modalvol://volume/prefix"
                        className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                      />
                    </div>
                  </div>
                ))}

                <button
                  type="button"
                  onClick={addTargetRoot}
                  className="w-full rounded-md border border-dashed border-gray-300 dark:border-gray-600 px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:border-blue-500 hover:text-blue-600 dark:hover:border-blue-400 dark:hover:text-blue-400"
                >
                  + Add another target root
                </button>
              </div>

              <p className="text-xs text-gray-500 dark:text-gray-400">
                e.g., s3://bucket/prefix/ for AWS S3 or modalvol://volume/prefix for
                Modal volumes
              </p>
            </>
          )}

          {/* Step 4: Collaborate */}
          {step === 4 && (
            <>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Invite team members to collaborate in this workspace.
              </p>

              {/* Add invite form */}
              <div className="flex gap-2">
                <input
                  type="email"
                  value={newInviteEmail}
                  onChange={(e) => setNewInviteEmail(e.target.value)}
                  placeholder="colleague@example.com"
                  className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
                <select
                  value={newInviteRole}
                  onChange={(e) =>
                    setNewInviteRole(e.target.value as "admin" | "member")
                  }
                  className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100"
                >
                  <option value="member">Member</option>
                  <option value="admin">Admin</option>
                </select>
                <button
                  type="button"
                  onClick={addInvite}
                  disabled={!newInviteEmail.trim()}
                  className="rounded-md bg-gray-200 dark:bg-gray-600 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-500 disabled:opacity-50"
                >
                  Add
                </button>
              </div>

              {/* Pending invites list */}
              {invites.length > 0 && (
                <div className="mt-4 divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-md">
                  {invites.map((invite, index) => (
                    <div
                      key={index}
                      className="flex items-center justify-between px-4 py-3"
                    >
                      <div>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {invite.email}
                        </div>
                        <div className="text-sm text-gray-500 dark:text-gray-400">
                          {invite.role}
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeInvite(index)}
                        className="text-sm text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {invites.length === 0 && (
                <p className="text-sm text-gray-500 dark:text-gray-400 italic">
                  No invites added yet. You can invite members later in workspace
                  settings.
                </p>
              )}

              {invites.length > 0 && (
                <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
                  *Collaborators will <em>not</em> receive automatic invite emails at
                  the moment. You&apos;ll need to notify them directly. They will see
                  the invite once they create an account or log in.
                </p>
              )}
            </>
          )}

          {/* Navigation buttons */}
          <div className="flex gap-3 pt-4">
            {step > 1 && (
              <button
                type="button"
                onClick={handleBack}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600"
              >
                Back
              </button>
            )}

            {step < totalSteps ? (
              <>
                {step === 3 && (
                  <button
                    type="button"
                    onClick={handleSkipTargetRoots}
                    className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600"
                  >
                    Setup later
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setStep(step + 1)}
                  disabled={
                    (step === 1 && !canProceedToStep2) ||
                    (step === 2 && !canProceedToStep3) ||
                    (step === 3 && !canProceedToStep4)
                  }
                  className="flex-1 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  Next
                </button>
              </>
            ) : (
              <button
                type="submit"
                disabled={loading}
                className="flex-1 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {loading
                  ? "Creating..."
                  : invites.length > 0
                    ? "Create and Invite*"
                    : "Create Workspace (invite later)"}
              </button>
            )}

            <button
              type="button"
              onClick={() => onNavigate("/")}
              className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600"
            >
              Cancel
            </button>
          </div>
        </form>

        <p className="mt-6 text-center text-sm text-gray-500 dark:text-gray-400">
          You can create up to 3 workspaces.
        </p>
      </main>
    </div>
  );
}
