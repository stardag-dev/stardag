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
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
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

  const generateSlug = (text: string) => {
    return text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 64);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !slug.trim()) return;

    const workspaceSlug = slug.trim();
    try {
      setLoading(true);
      setError(null);
      await createWorkspace({
        name: name.trim(),
        slug: workspaceSlug,
        description: description.trim() || undefined,
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
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">
          Create Workspace
        </h1>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300">
              {error}
            </div>
          )}

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
                setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))
              }
              placeholder="my-workspace"
              required
              pattern="^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$"
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Lowercase letters, numbers, and hyphens only
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

          <div className="flex gap-3 pt-4">
            <button
              type="submit"
              disabled={loading || !name.trim() || !slug.trim()}
              className="flex-1 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {loading ? "Creating..." : "Create Workspace"}
            </button>
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
