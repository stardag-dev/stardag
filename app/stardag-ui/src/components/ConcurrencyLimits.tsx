import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteConcurrencyLimit,
  fetchConcurrencyLimits,
  setConcurrencyLimit,
} from "../api/registry";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type { ConcurrencyLimit, ConcurrencyLimitHolder } from "../types/task";
import { shortBuildId, shortTaskId } from "../utils/ids";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { TaskDetail } from "./TaskDetail";

interface ConcurrencyLimitsProps {
  onSelectBuild?: (buildId: string) => void;
  onOpenTask?: (taskId: string) => void;
}

// The routes carry the key as a path segment, where a "/" (even encoded)
// becomes a separator and the request 404s.
const KEY_SLASH_MESSAGE =
  'A key cannot contain "/": the registry addresses a limit by its key in the URL path';

const HEADER =
  "px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400";
const SUB_HEADER =
  "py-1 pr-4 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400";

/**
 * The environment's named concurrency limits: list, create, edit and
 * delete keys, and see which tasks hold a key's slots (live claims), from
 * `GET /concurrency-limits?include_holders=true`.
 *
 * Ported from v1 without evict: v2 has no route to fail a holder in place.
 * A slot held by an execution that is gone is freed by stopping it —
 * `stardag builds stop --mark-lost` — which ends the claim through the
 * execution ledger. Mutations are offered to workspace admins only, as v1
 * did.
 */
export function ConcurrencyLimits({
  onSelectBuild,
  onOpenTask,
}: ConcurrencyLimitsProps) {
  const { activeEnvironment, activeWorkspaceRole } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";
  const environmentId = activeEnvironment?.id ?? null;

  const [limits, setLimits] = useState<ConcurrencyLimit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [newKey, setNewKey] = useState("");
  const [newMax, setNewMax] = useState("1");
  const [creating, setCreating] = useState(false);

  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);

  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  // A slow response from a previous environment must not overwrite the
  // current one's state.
  const epochRef = useRef(0);
  // The environment on screen now. A mutation started under another one
  // must neither reload nor write state when it completes: its handler
  // closes over the old environment, and a reload from it would bump the
  // epoch and land the old environment's limits on the new one's page.
  const environmentRef = useRef(environmentId);
  useEffect(() => {
    environmentRef.current = environmentId;
  }, [environmentId]);
  const stillOn = (env: string) => environmentRef.current === env;

  // Environments can share key names: nothing expanded or selected in one
  // survives a switch (adjusted during render, before any fetch).
  const [shownEnvironmentId, setShownEnvironmentId] = useState(environmentId);
  if (environmentId !== shownEnvironmentId) {
    setShownEnvironmentId(environmentId);
    setLimits([]);
    setLoading(true);
    setExpandedKey(null);
    setSelectedTaskId(null);
    setEditingKey(null);
  }

  useEffect(() => {
    setBreadcrumb([{ label: "Concurrency Limits" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  const loadLimits = useCallback(async () => {
    const epoch = ++epochRef.current;
    const fresh = () => epochRef.current === epoch;
    if (!environmentId) return;
    try {
      const fetched = await fetchConcurrencyLimits(environmentId, true);
      if (!fresh() || !stillOn(environmentId)) return;
      setLimits(fetched);
      setError(null);
    } catch (err) {
      if (!fresh() || !stillOn(environmentId)) return;
      setError(
        err instanceof Error ? err.message : "Failed to load concurrency limits",
      );
    } finally {
      if (fresh() && stillOn(environmentId)) setLoading(false);
    }
  }, [environmentId]);

  useEffect(() => {
    void loadLimits();
  }, [loadLimits]);

  const parseMax = (value: string): number | null => {
    const max = Number(value);
    return value.trim() !== "" && Number.isInteger(max) && max >= 0 ? max : null;
  };

  const handleCreate = async () => {
    if (!environmentId) return;
    const key = newKey.trim();
    const max = parseMax(newMax);
    if (!key || max === null) {
      setActionError("Enter a key and a max concurrency of 0 or more");
      return;
    }
    if (key.includes("/")) {
      setActionError(KEY_SLASH_MESSAGE);
      return;
    }
    const env = environmentId;
    setCreating(true);
    setActionError(null);
    try {
      await setConcurrencyLimit(key, max, env);
      if (!stillOn(env)) return;
      setNewKey("");
      setNewMax("1");
      await loadLimits();
    } catch (err) {
      if (!stillOn(env)) return;
      setActionError(err instanceof Error ? err.message : "Failed to create limit");
    } finally {
      setCreating(false);
    }
  };

  const handleSaveEdit = async (key: string) => {
    if (!environmentId) return;
    const max = parseMax(editValue);
    if (max === null) {
      setActionError("Max concurrency must be an integer of 0 or more");
      return;
    }
    const env = environmentId;
    setSaving(true);
    setActionError(null);
    try {
      await setConcurrencyLimit(key, max, env);
      if (!stillOn(env)) return;
      setEditingKey(null);
      await loadLimits();
    } catch (err) {
      if (!stillOn(env)) return;
      setActionError(err instanceof Error ? err.message : "Failed to update limit");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (key: string) => {
    if (!environmentId) return;
    if (
      !window.confirm(
        `Delete the concurrency limit "${key}"? The key becomes unlimited.`,
      )
    )
      return;
    const env = environmentId;
    setActionError(null);
    try {
      await deleteConcurrencyLimit(key, env);
      if (!stillOn(env)) return;
      if (expandedKey === key) setExpandedKey(null);
      await loadLimits();
    } catch (err) {
      if (!stillOn(env)) return;
      setActionError(err instanceof Error ? err.message : "Failed to delete limit");
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
              Named per-environment caps on concurrently running tasks. A task started
              with a key holds one of its slots while it holds its claim.
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

          {isAdmin && (
            <div className="flex items-end gap-2 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800">
              <div className="flex-1">
                <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">
                  Key
                  <input
                    type="text"
                    value={newKey}
                    onChange={(e) => setNewKey(e.target.value)}
                    placeholder="e.g. gpu"
                    className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                  />
                </label>
              </div>
              <div className="w-32">
                <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">
                  Max concurrent
                  <input
                    type="number"
                    min={0}
                    value={newMax}
                    onChange={(e) => setNewMax(e.target.value)}
                    className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
                  />
                </label>
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

          {loading ? (
            <div className="flex items-center justify-center py-8">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            </div>
          ) : limits.length === 0 ? (
            !error && (
              <div className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
                No concurrency limits configured for this environment.
              </div>
            )
          ) : (
            <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead className="bg-gray-50 dark:bg-gray-800">
                  <tr>
                    <th className={HEADER}>Key</th>
                    <th className={HEADER}>Max Concurrent</th>
                    <th className={HEADER}>Current Holders</th>
                    {isAdmin && <th className={`${HEADER} text-right`}>Actions</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
                  {limits.map((limit) => (
                    <LimitRow
                      key={limit.key}
                      limit={limit}
                      isAdmin={isAdmin}
                      expanded={expandedKey === limit.key}
                      editing={editingKey === limit.key}
                      editValue={editValue}
                      saving={saving}
                      onToggleHolders={() => {
                        setActionError(null);
                        setExpandedKey(expandedKey === limit.key ? null : limit.key);
                      }}
                      onStartEdit={() => {
                        setActionError(null);
                        setEditingKey(limit.key);
                        setEditValue(String(limit.max_concurrent));
                      }}
                      onEditValueChange={setEditValue}
                      onSaveEdit={() => handleSaveEdit(limit.key)}
                      onCancelEdit={() => setEditingKey(null)}
                      onDelete={() => handleDelete(limit.key)}
                      onSelectTask={setSelectedTaskId}
                      onSelectBuild={onSelectBuild}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {selectedTaskId && environmentId && (
        <div className="w-96 flex-shrink-0 border-l border-gray-200 dark:border-gray-700">
          <TaskDetail
            taskId={selectedTaskId}
            environmentId={environmentId}
            onClose={() => setSelectedTaskId(null)}
            onOpenTaskPage={onOpenTask ? () => onOpenTask(selectedTaskId) : undefined}
            // A remedy that ends the claim frees the slot: re-read the counts.
            onChanged={() => void loadLimits()}
          />
        </div>
      )}
    </div>
  );
}

interface LimitRowProps {
  limit: ConcurrencyLimit;
  isAdmin: boolean;
  expanded: boolean;
  editing: boolean;
  editValue: string;
  saving: boolean;
  onToggleHolders: () => void;
  onStartEdit: () => void;
  onEditValueChange: (value: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onDelete: () => void;
  onSelectTask: (taskId: string) => void;
  onSelectBuild?: (buildId: string) => void;
}

function LimitRow({
  limit,
  isAdmin,
  expanded,
  editing,
  editValue,
  saving,
  onToggleHolders,
  onStartEdit,
  onEditValueChange,
  onSaveEdit,
  onCancelEdit,
  onDelete,
  onSelectTask,
  onSelectBuild,
}: LimitRowProps) {
  const holders = limit.holders ?? [];
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
                min={0}
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
            aria-expanded={expanded}
            className="flex items-center gap-1 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            title="Show current slot holders"
          >
            <svg
              aria-hidden="true"
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
            {limit.in_use}
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
            {holders.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No tasks currently hold a slot of this key.
              </p>
            ) : (
              <div className="space-y-2">
                <table className="min-w-full">
                  <thead>
                    <tr>
                      <th className={SUB_HEADER}>Task</th>
                      <th className={SUB_HEADER}>Build</th>
                      <th className={SUB_HEADER}>Execution</th>
                      <th className={SUB_HEADER}>Running Since</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {holders.map((holder) => (
                      <HolderRow
                        key={holder.task_id}
                        holder={holder}
                        onSelectTask={onSelectTask}
                        onSelectBuild={onSelectBuild}
                      />
                    ))}
                  </tbody>
                </table>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  A slot held by an execution that is gone is freed by stopping it:{" "}
                  <code className="rounded bg-gray-100 px-1 dark:bg-gray-700">
                    stardag builds stop --mark-lost
                  </code>
                  .
                </p>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function HolderRow({
  holder,
  onSelectTask,
  onSelectBuild,
}: {
  holder: ConcurrencyLimitHolder;
  onSelectTask: (taskId: string) => void;
  onSelectBuild?: (buildId: string) => void;
}) {
  return (
    <tr>
      <td className="py-1.5 pr-4">
        <button
          onClick={() => onSelectTask(holder.task_id)}
          className="text-left text-sm text-blue-600 hover:text-blue-700 hover:underline dark:text-blue-400 dark:hover:text-blue-300"
          title="View task details"
        >
          {holder.task_name}
        </button>
        <span
          className="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400"
          title={holder.task_id}
        >
          {shortTaskId(holder.task_id)}
        </span>
      </td>
      <td className="py-1.5 pr-4 font-mono text-xs">
        {onSelectBuild ? (
          <button
            onClick={() => onSelectBuild(holder.build_id)}
            className="text-blue-600 hover:underline dark:text-blue-400"
            title={`Open build ${holder.build_id}`}
          >
            {shortBuildId(holder.build_id)}
          </button>
        ) : (
          <span className="text-gray-500 dark:text-gray-400" title={holder.build_id}>
            {shortBuildId(holder.build_id)}
          </span>
        )}
      </td>
      <td
        className="py-1.5 pr-4 font-mono text-xs text-gray-500 dark:text-gray-400"
        title={holder.execution_id ?? undefined}
      >
        {holder.execution_id ? holder.execution_id.slice(0, 8) : "—"}
      </td>
      <td
        className="py-1.5 pr-4 text-sm text-gray-900 dark:text-gray-100"
        title={holder.started_at ? formatAbsoluteTime(holder.started_at) : undefined}
      >
        {holder.started_at ? formatRelativeTime(holder.started_at) : "—"}
      </td>
    </tr>
  );
}
