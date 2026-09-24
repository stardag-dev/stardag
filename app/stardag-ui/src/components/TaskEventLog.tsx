import { useCallback, useRef, useState } from "react";
import { EVENT_LIST_LIMIT, fetchTaskEvents } from "../api/registry";
import type { TaskEvent } from "../types/task";
import { eventTypeStyle, formatEventType } from "../utils/events";
import { shortBuildId } from "../utils/ids";
import { FullscreenModal } from "./FullscreenModal";

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  const centiseconds = Math.floor(d.getMilliseconds() / 10)
    .toString()
    .padStart(2, "0");
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString(undefined, {
    hour12: false,
  })}.${centiseconds}`;
}

const HEADER =
  "px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400";

/**
 * v1's "See full event log" button and its fullscreen table, over
 * `GET /tasks/{id}/events`: the task's append-only log across every build,
 * oldest first. A report the registry recorded but refused is marked, and
 * an event's metadata (a structure divergence's differing edges, say) is
 * on hover.
 */
export function TaskEventLog({
  taskId,
  taskLabel,
  environmentId,
}: {
  taskId: string;
  taskLabel: string;
  environmentId: string;
}) {
  const [open, setOpen] = useState(false);
  const [events, setEvents] = useState<TaskEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const epochRef = useRef(0);

  const show = useCallback(() => {
    setOpen(true);
    setEvents(null);
    setError(null);
    const epoch = ++epochRef.current;
    fetchTaskEvents(taskId, environmentId)
      .then((rows) => {
        if (epochRef.current === epoch) setEvents(rows);
      })
      .catch((err: unknown) => {
        if (epochRef.current !== epoch) return;
        setEvents([]);
        setError(err instanceof Error ? err.message : "Failed to load events");
      });
  }, [taskId, environmentId]);

  return (
    <>
      <button
        type="button"
        onClick={show}
        className="flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
      >
        <svg
          aria-hidden="true"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"
          />
        </svg>
        See full event log
      </button>

      <FullscreenModal
        isOpen={open}
        onClose={() => {
          epochRef.current += 1;
          setOpen(false);
        }}
        title="Task Event Log"
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Complete event history for task{" "}
            <span className="font-mono font-medium text-gray-700 dark:text-gray-300">
              {taskLabel}
            </span>{" "}
            across all builds, oldest first.
          </p>
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          {events === null ? (
            <div className="flex items-center justify-center py-8">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            </div>
          ) : events.length === 0 ? (
            !error && (
              <div className="py-8 text-center text-gray-500 dark:text-gray-400">
                No events found for this task.
              </div>
            )
          ) : (
            <>
              {events.length >= EVENT_LIST_LIMIT && (
                <p className="text-xs text-amber-800 dark:text-amber-300">
                  Showing the first {EVENT_LIST_LIMIT} events; later ones are not
                  listed.
                </p>
              )}
              <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-800">
                    <tr>
                      <th className={HEADER}>Timestamp</th>
                      <th className={HEADER}>Event</th>
                      <th className={HEADER}>Build</th>
                      <th className={HEADER}>Execution</th>
                      <th className={HEADER}>Details</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
                    {events.map((event) => (
                      <EventRow key={event.id} event={event} />
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </FullscreenModal>
    </>
  );
}

function EventRow({ event }: { event: TaskEvent }) {
  const metadataKeys = event.event_metadata ? Object.keys(event.event_metadata) : [];
  return (
    <tr className="hover:bg-gray-50 dark:hover:bg-gray-800">
      <td className="px-4 py-3 text-sm whitespace-nowrap text-gray-900 dark:text-gray-100">
        {formatTimestamp(event.created_at)}
      </td>
      <td className="px-4 py-3 whitespace-nowrap">
        <span
          className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${eventTypeStyle(
            event.event_type,
          )}`}
        >
          {formatEventType(event.event_type)}
        </span>
        {!event.report_applied && (
          <span
            className="ml-1.5 rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600 dark:bg-gray-700 dark:text-gray-300"
            title="Recorded but refused: the report did not change the task's state"
          >
            not applied
          </span>
        )}
      </td>
      <td className="px-4 py-3 font-mono text-sm whitespace-nowrap text-gray-500 dark:text-gray-400">
        {event.build_id ? (
          <span title={event.build_id}>{shortBuildId(event.build_id)}</span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-4 py-3 font-mono text-sm whitespace-nowrap text-gray-500 dark:text-gray-400">
        {event.execution_id ? (
          <span title={event.execution_id}>{event.execution_id.slice(0, 8)}</span>
        ) : (
          "—"
        )}
      </td>
      <td className="max-w-xs truncate px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
        {event.error_message ? (
          <span className="text-red-600 dark:text-red-400" title={event.error_message}>
            {event.error_message.length > 50
              ? `${event.error_message.slice(0, 50)}...`
              : event.error_message}
          </span>
        ) : metadataKeys.length > 0 ? (
          <span title={JSON.stringify(event.event_metadata, null, 2)}>
            {metadataKeys.length} field{metadataKeys.length === 1 ? "" : "s"}
          </span>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}
