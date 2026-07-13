import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteConcurrencyLimit,
  evictConcurrencyLimitHolder,
  fetchConcurrencyLimitHolders,
  fetchConcurrencyLimits,
  upsertConcurrencyLimit,
  type ConcurrencyLimit,
  type ConcurrencyLimitHolder,
} from "../api/concurrencyLimits";
import { fetchTask } from "../api/tasks";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Task } from "../types/task";
import { modalFunctionCallUrl } from "../utils/modalLinks";
import { ExecutorBadge } from "./ExecutorBadge";
import { TaskDetail } from "./TaskDetail";

// Env-scoped admin view for named concurrency limits: list/create/edit/
// delete limit keys and drill into a key's current slot holders (RUNNING
// tasks), with an evict action to recover leaked slots. Limits and
// holders are viewable by all workspace members; mutations (create/
// edit/delete/evict) are workspace-admin-only, matching the server-side
// role gate and the WorkspaceSettings gating pattern.
export function ConcurrencyLimits() {
  const { activeEnvironment, activeWorkspaceRole } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";

  const [limits, setLimits] = useState<ConcurrencyLimit[]>([]);
  const [holderCounts, setHolderCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Create form
  const [newKey, setNewKey] = useState("");
  const [newMax, setNewMax] = useState("1");
  const [creating, setCreating] = useState(false);

  // Inline edit
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);

  // Per-key holder drill-down
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [holders, setHolders] = useState<ConcurrencyLimitHolder[]>([]);
  const [holdersTotal, setHoldersTotal] = useState(0);
  const [holdersLoading, setHoldersLoading] = useState(false);
  const [evictingTaskId, setEvictingTaskId] = useState<string | null>(null);

  // Holder task detail panel
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  // Stale-response guards: each async load captures an epoch at start and
  // drops its results if a newer load (or an environment switch) has
  // bumped it since — a slow response from a previous environment must
  // not overwrite the current one's state.
  const limitsEpochRef = useRef(0);
  const holdersEpochRef = useRef(0);

  useEffect(() => {
    setBreadcrumb([{ label: "Concurrency Limits" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  const loadLimits = useCallback(async () => {
    const epoch = ++limitsEpochRef.current;
    const fresh = () => limitsEpochRef.current === epoch;
    if (!activeEnvironment?.id) {
      setLimits([]);
      setHolderCounts({});
      setLoading(false);
      return;
    }
    setError(null);
    try {
      const fetched = await fetchConcurrencyLimits(activeEnvironment.id);
      if (!fresh()) return;
      setLimits(fetched);
      // Holder counts per key: a holders fetch with limit=1 returns the
      // full count in `total`. Limit lists are small (admin-configured),
      // so a request per key is fine. Count failures degrade to "—".
      const counts = await Promise.all(
        fetched.map(async (limit) => {
          try {
            const response = await fetchConcurrencyLimitHolders(
              limit.key,
              activeEnvironment.id,
              1,
            );
            return [limit.key, response.total] as const;
          } catch {
            return null;
          }
        }),
      );
      if (!fresh()) return;
      setHolderCounts(
        Object.fromEntries(counts.filter((c): c is [string, number] => c !== null)),
      );
    } catch (err) {
      if (!fresh()) return;
      setError(
        err instanceof Error ? err.message : "Failed to load concurrency limits",
      );
    } finally {
      if (fresh()) {
        setLoading(false);
      }
    }
  }, [activeEnvironment?.id]);

  useEffect(() => {
    // Invalidate any in-flight holders load from the previous environment
    // (loadLimits bumps its own epoch on each call).
    holdersEpochRef.current++;
    setLoading(true);
    setExpandedKey(null);
    setSelectedTask(null);
    // Clear counts eagerly: environments can share key names, so a stale
    // count from the previous environment must not show on the new
    // environment's row while its own count fetch is in flight (or after
    // it failed).
    setHolderCounts({});
    loadLimits();
  }, [loadLimits]);

  const loadHolders = useCallback(
    async (key: string) => {
      if (!activeEnvironment?.id) return;
      const epoch = ++holdersEpochRef.current;
      const fresh = () => holdersEpochRef.current === epoch;
      setHoldersLoading(true);
      try {
        const response = await fetchConcurrencyLimitHolders(key, activeEnvironment.id);
        if (!fresh()) return;
        setHolders(response.holders);
        setHoldersTotal(response.total);
      } catch (err) {
        if (!fresh()) return;
        setActionError(err instanceof Error ? err.message : "Failed to load holders");
      } finally {
        if (fresh()) {
          setHoldersLoading(false);
        }
      }
    },
    [activeEnvironment?.id],
  );

  const handleToggleHolders = useCallback(
    (key: string) => {
      setActionError(null);
      if (expandedKey === key) {
        setExpandedKey(null);
        return;
      }
      setExpandedKey(key);
      setHolders([]);
      setHoldersTotal(0);
      loadHolders(key);
    },
    [expandedKey, loadHolders],
  );

  const handleCreate = async () => {
    if (!activeEnvironment?.id) return;
    const key = newKey.trim();
    const max = Number(newMax);
    if (!key || !Number.isInteger(max) || max < 1) {
      setActionError("Enter a key and a max concurrency of at least 1");
      return;
    }
    setCreating(true);
    setActionError(null);
    try {
      await upsertConcurrencyLimit(key, max, activeEnvironment.id);
      setNewKey("");
      setNewMax("1");
      await loadLimits();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to create limit");
    } finally {
      setCreating(false);
    }
  };

  const handleStartEdit = (limit: ConcurrencyLimit) => {
    setActionError(null);
    setEditingKey(limit.key);
    setEditValue(String(limit.max_concurrent));
  };

  const handleSaveEdit = async (key: string) => {
    if (!activeEnvironment?.id) return;
    const max = Number(editValue);
    if (!Number.isInteger(max) || max < 1) {
      setActionError("Max concurrency must be an integer of at least 1");
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      await upsertConcurrencyLimit(key, max, activeEnvironment.id);
      setEditingKey(null);
      await loadLimits();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to update limit");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (key: string) => {
    if (!activeEnvironment?.id) return;
    const confirmed = window.confirm(
      `Delete the concurrency limit "${key}"? The key becomes unlimited.`,
    );
    if (!confirmed) return;
    setActionError(null);
    try {
      await deleteConcurrencyLimit(key, activeEnvironment.id);
      if (expandedKey === key) setExpandedKey(null);
      await loadLimits();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to delete limit");
    }
  };

  const handleEvict = async (key: string, holder: ConcurrencyLimitHolder) => {
    if (!activeEnvironment?.id) return;
    const confirmed = window.confirm(
      `Evict task "${holder.task_name}" (${holder.task_id.slice(0, 12)}...) from ` +
        `"${key}"?\n\nThe task is marked FAILED and all its slots are freed — but ` +
        `the underlying process is NOT stopped. Only evict holders whose process ` +
        `you know is dead: evicting a live worker oversubscribes the cap, and in ` +
        `a fail-fast reactive build the evicted-but-alive execution is the one ` +
        `that never gets cancelled (it can still complete after the build failed).`,
    );
    if (!confirmed) return;
    setEvictingTaskId(holder.task_id);
    setActionError(null);
    try {
      await evictConcurrencyLimitHolder(key, holder.task_id, activeEnvironment.id);
      await Promise.all([loadHolders(key), loadLimits()]);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to evict holder");
    } finally {
      setEvictingTaskId(null);
    }
  };

  const handleSelectHolderTask = async (holder: ConcurrencyLimitHolder) => {
    if (!activeEnvironment?.id) return;
    setActionError(null);
    try {
      const task = await fetchTask(holder.task_id, activeEnvironment.id);
      setSelectedTask(task);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to load task");
    }
  };

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        Select an environment to manage its concurrency limits.
      </div>
    );
  }

  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex-1 overflow-auto p-4">
        <div className="mx-auto max-w-4xl space-y-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Concurrency Limits
            </h1>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Named per-environment caps on concurrently running tasks. Tasks started
              with a key count against its limit until they leave the running state.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-red-50 p-4 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
              {error}
            </div>
          )}
          {actionError && (
            <div className="rounded-md bg-red-50 p-4 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
              {actionError}
            </div>
          )}

          {/* Create form (workspace admins only) */}
          {isAdmin && (
            <div className="flex items-end gap-2 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800">
              <div className="flex-1">
                <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">
                  Key
                </label>
                <input
                  type="text"
                  value={newKey}
                  onChange={(e) => setNewKey(e.target.value)}
                  placeholder="e.g. gpu"
                  className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                />
              </div>
              <div className="w-32">
                <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">
                  Max concurrent
                </label>
                <input
                  type="number"
                  min={1}
                  value={newMax}
                  onChange={(e) => setNewMax(e.target.value)}
                  aria-label="Max concurrent"
                  className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                />
              </div>
              <button
                onClick={handleCreate}
                disabled={creating || !newKey.trim()}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {creating ? "Adding..." : "Add limit"}
              </button>
            </div>
          )}

          {/* Limits table */}
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            </div>
          ) : limits.length === 0 ? (
            <div className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
              No concurrency limits configured for this environment.
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead className="bg-gray-50 dark:bg-gray-800">
                  <tr>
                    <th className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Key
                    </th>
                    <th className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Max Concurrent
                    </th>
                    <th className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Current Holders
                    </th>
                    {isAdmin && (
                      <th className="px-4 py-2 text-right text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                        Actions
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
                  {limits.map((limit) => (
                    <LimitRow
                      key={limit.key}
                      limit={limit}
                      isAdmin={isAdmin}
                      holderCount={holderCounts[limit.key]}
                      expanded={expandedKey === limit.key}
                      editing={editingKey === limit.key}
                      editValue={editValue}
                      saving={saving}
                      holders={holders}
                      holdersTotal={holdersTotal}
                      holdersLoading={holdersLoading}
                      evictingTaskId={evictingTaskId}
                      onToggleHolders={() => handleToggleHolders(limit.key)}
                      onStartEdit={() => handleStartEdit(limit)}
                      onEditValueChange={setEditValue}
                      onSaveEdit={() => handleSaveEdit(limit.key)}
                      onCancelEdit={() => setEditingKey(null)}
                      onDelete={() => handleDelete(limit.key)}
                      onEvict={(holder) => handleEvict(limit.key, holder)}
                      onSelectTask={handleSelectHolderTask}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Holder task detail panel */}
      {selectedTask && (
        <div className="w-96 flex-shrink-0 border-l border-gray-200 dark:border-gray-700">
          <TaskDetail task={selectedTask} onClose={() => setSelectedTask(null)} />
        </div>
      )}
    </div>
  );
}

interface LimitRowProps {
  limit: ConcurrencyLimit;
  // Workspace-admin mutations (edit/delete/evict) are hidden otherwise.
  isAdmin: boolean;
  holderCount: number | undefined;
  expanded: boolean;
  editing: boolean;
  editValue: string;
  saving: boolean;
  holders: ConcurrencyLimitHolder[];
  holdersTotal: number;
  holdersLoading: boolean;
  evictingTaskId: string | null;
  onToggleHolders: () => void;
  onStartEdit: () => void;
  onEditValueChange: (value: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onDelete: () => void;
  onEvict: (holder: ConcurrencyLimitHolder) => void;
  onSelectTask: (holder: ConcurrencyLimitHolder) => void;
}

function LimitRow({
  limit,
  isAdmin,
  holderCount,
  expanded,
  editing,
  editValue,
  saving,
  holders,
  holdersTotal,
  holdersLoading,
  evictingTaskId,
  onToggleHolders,
  onStartEdit,
  onEditValueChange,
  onSaveEdit,
  onCancelEdit,
  onDelete,
  onEvict,
  onSelectTask,
}: LimitRowProps) {
  return (
    <>
      <tr className="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td className="px-4 py-2 font-mono text-sm text-gray-900 dark:text-gray-100">
          {limit.key}
        </td>
        <td className="px-4 py-2 text-sm text-gray-900 dark:text-gray-100">
          {editing ? (
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                value={editValue}
                onChange={(e) => onEditValueChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onSaveEdit();
                  if (e.key === "Escape") onCancelEdit();
                }}
                autoFocus
                className="w-20 rounded-md border border-gray-300 bg-white px-2 py-0.5 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                aria-label={`Max concurrent for ${limit.key}`}
              />
              <button
                onClick={onSaveEdit}
                disabled={saving}
                className="text-xs font-medium text-blue-600 hover:text-blue-700 disabled:opacity-50 dark:text-blue-400"
              >
                {saving ? "Saving..." : "Save"}
              </button>
              <button
                onClick={onCancelEdit}
                className="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
              >
                Cancel
              </button>
            </div>
          ) : isAdmin ? (
            <button
              onClick={onStartEdit}
              className="rounded px-1 hover:bg-gray-100 dark:hover:bg-gray-700"
              title="Edit max concurrency"
            >
              {limit.max_concurrent}
              <span className="ml-1.5 text-xs text-gray-400">✎</span>
            </button>
          ) : (
            <span className="px-1">{limit.max_concurrent}</span>
          )}
        </td>
        <td className="px-4 py-2 text-sm">
          <button
            onClick={onToggleHolders}
            className="flex items-center gap-1 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            title="Show current slot holders"
          >
            <svg
              className={`h-3 w-3 transition-transform ${expanded ? "rotate-90" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 5l7 7-7 7"
              />
            </svg>
            {holderCount ?? "—"}
          </button>
        </td>
        {isAdmin && (
          <td className="px-4 py-2 text-right">
            <button
              onClick={onDelete}
              className="rounded-md px-2 py-1 text-xs font-medium text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/20"
              title="Delete this limit (the key becomes unlimited)"
            >
              Delete
            </button>
          </td>
        )}
      </tr>
      {expanded && (
        <tr>
          <td
            colSpan={isAdmin ? 4 : 3}
            className="bg-gray-50 px-4 py-3 dark:bg-gray-800/50"
          >
            {holdersLoading ? (
              <div className="flex items-center justify-center py-4">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
              </div>
            ) : holders.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No tasks currently hold a slot of this key.
              </p>
            ) : (
              <div className="space-y-2">
                {holdersTotal > holders.length && (
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    Showing {holders.length} of {holdersTotal} holders (oldest running
                    first).
                  </p>
                )}
                <table className="min-w-full">
                  <thead>
                    <tr>
                      <th className="py-1 pr-4 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                        Task
                      </th>
                      <th className="py-1 pr-4 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                        Running Since
                      </th>
                      <th className="py-1 pr-4 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                        Executor
                      </th>
                      {isAdmin && (
                        <th className="py-1 text-right text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                          Actions
                        </th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {holders.map((holder) => {
                      const callUrl = modalFunctionCallUrl(
                        holder.latest_executor_metadata,
                        holder.latest_executor_ref,
                      );
                      return (
                        <tr key={holder.task_id}>
                          <td className="py-1.5 pr-4">
                            <button
                              onClick={() => onSelectTask(holder)}
                              className="text-left text-sm text-blue-600 hover:text-blue-700 hover:underline dark:text-blue-400 dark:hover:text-blue-300"
                              title="View task details"
                            >
                              {holder.task_name}
                            </button>
                            <span className="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400">
                              {holder.task_id.slice(0, 12)}...
                            </span>
                          </td>
                          <td className="py-1.5 pr-4 text-sm text-gray-900 dark:text-gray-100">
                            {holder.latest_status_at
                              ? new Date(holder.latest_status_at).toLocaleString()
                              : "—"}
                          </td>
                          <td className="py-1.5 pr-4">
                            <span className="flex items-center gap-1.5">
                              <ExecutorBadge
                                executor={holder.latest_executor}
                                executorRef={holder.latest_executor_ref}
                              />
                              {callUrl && (
                                <a
                                  href={callUrl}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                                  title="Open the function call in the Modal dashboard"
                                >
                                  View on Modal
                                </a>
                              )}
                            </span>
                          </td>
                          {isAdmin && (
                            <td className="py-1.5 text-right">
                              <button
                                onClick={() => onEvict(holder)}
                                disabled={evictingTaskId === holder.task_id}
                                className="rounded-md bg-red-100 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-200 disabled:opacity-50 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
                                title="Mark the task failed and free all its slots"
                              >
                                {evictingTaskId === holder.task_id
                                  ? "Evicting..."
                                  : "Evict"}
                              </button>
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
