import { useState } from "react";
import { createWorkspace } from "../api/workspaces";
import { useEnvironment } from "../context/EnvironmentContext";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
import { UserMenu } from "./UserMenu";

interface CreateWorkspaceProps {
  onNavigate: (path: string) => void;
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

  // Step 3: Target root details
  const [targetRootName, setTargetRootName] = useState("default");
  const [targetRootUri, setTargetRootUri] = useState("");

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

  const totalSteps = 3;

  const canProceedToStep2 = name.trim() && slug.trim();
  const canProceedToStep3 = environmentName.trim() && environmentSlug.trim();
  const canSubmit = targetRootName.trim() && targetRootUri.trim();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;

    const workspaceSlug = slug.trim();
    try {
      setLoading(true);
      setError(null);
      await createWorkspace({
        name: name.trim(),
        slug: workspaceSlug,
        description: description.trim() || undefined,
        initial_environment_name: environmentName.trim(),
        initial_environment_slug: environmentSlug.trim(),
        initial_target_root_name: targetRootName.trim(),
        initial_target_root_uri: targetRootUri.trim(),
      });
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
          {[1, 2, 3].map((s) => (
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
            {step === 3 && "Target Root"}
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
            </>
          )}

          {/* Step 3: Target Root */}
          {step === 3 && (
            <>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Configure where task outputs will be stored. This is usually an S3
                bucket or local path.
              </p>

              <div>
                <label
                  htmlFor="targetName"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Target Root Name
                </label>
                <input
                  type="text"
                  id="targetName"
                  value={targetRootName}
                  onChange={(e) => setTargetRootName(e.target.value)}
                  placeholder="default"
                  required
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
              </div>

              <div>
                <label
                  htmlFor="targetUri"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  URI Prefix
                </label>
                <input
                  type="text"
                  id="targetUri"
                  value={targetRootUri}
                  onChange={(e) => setTargetRootUri(e.target.value)}
                  placeholder="s3://my-bucket/stardag/ or ~/.stardag/targets/"
                  required
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  e.g., s3://bucket/prefix/ for cloud storage or ~/.stardag/targets/ for
                  local
                </p>
              </div>
            </>
          )}

          {/* Navigation buttons */}
          <div className="flex gap-3 pt-4">
            {step > 1 && (
              <button
                type="button"
                onClick={() => setStep(step - 1)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600"
              >
                Back
              </button>
            )}

            {step < totalSteps ? (
              <button
                type="button"
                onClick={() => setStep(step + 1)}
                disabled={
                  (step === 1 && !canProceedToStep2) ||
                  (step === 2 && !canProceedToStep3)
                }
                className="flex-1 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                Next
              </button>
            ) : (
              <button
                type="submit"
                disabled={loading || !canSubmit}
                className="flex-1 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {loading ? "Creating..." : "Create Workspace"}
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
