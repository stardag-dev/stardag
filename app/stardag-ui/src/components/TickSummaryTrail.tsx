import { useState } from "react";
import type { BuildTickSummary } from "../types/task";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";

// What each tick outcome means, in the words an operator needs. Unknown
// outcomes (a newer SDK) fall through to the raw value with no gloss —
// better a bare word than a wrong explanation.
const OUTCOMES: Record<string, { label: string; help: string; tone: string }> = {
  not_reactive: {
    label: "not reactive",
    help: "The build carries no reactive-app marker, so the tick did not drive it. Nothing will advance this build on a schedule.",
    tone: "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300",
  },
  lease_held: {
    label: "lease held",
    help: "Another tick already held this build's lease, so this one exited without acting. Normal under overlapping wake-ups.",
    tone: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
  },
  terminal: {
    label: "terminal",
    help: "The build was already in a terminal state when the tick ran.",
    tone: "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300",
  },
  lingered_out: {
    label: "lingered out",
    help: "The tick waited for work to become actionable and gave up without anything appearing. Repeated on every tick, this is the signature of a stalled build.",
    tone: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  },
  foreign_app: {
    label: "foreign app",
    help: "The build is owned by a different reactive app than the one that ticked, so this tick declined to drive it.",
    tone: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  },
};

// Counters this UI knows how to explain. Everything else in `summary` is
// rendered generically — see `summaryEntries`. Deliberately a lookup and
// not an exhaustive union: the SDK adds counters continuously (external
// blocker counts are the current example) and a summary key must never be
// dropped just because this build of the UI predates it.
const COUNTERS: Record<string, { label: string; help: string }> = {
  spawned: { label: "spawned", help: "Executions this tick started." },
  self_healed: {
    label: "self-healed",
    help: "Tasks recorded complete after their detached execution was found finished.",
  },
  failed_recorded: {
    label: "failures recorded",
    help: "Tasks whose detached execution was found failed.",
  },
  cancelled_refs: {
    label: "executions cancelled",
    help: "Detached executions cancelled by this tick.",
  },
  iterations: { label: "iterations", help: "Scheduling passes within this tick." },
  limit_denied: {
    label: "concurrency-limit denied",
    help: "Spawns refused because a named concurrency limit was full. The build is waiting for a slot, not stuck.",
  },
  claim_denied: {
    label: "claim denied",
    help: "Spawns refused because another build already holds the task's execution claim. The claim holder must finish or be released.",
  },
  skipped: { label: "skipped", help: "Tasks skipped because an upstream failed." },
  terminal_status: {
    label: "terminal status",
    help: "The status the tick moved the build to.",
  },
};

/** "claim_denied" -> "claim denied"; used for keys we have no gloss for. */
function humanise(key: string): string {
  return key.replace(/_/g, " ");
}

interface Entry {
  key: string;
  label: string;
  help?: string;
  value: string;
  /** Numeric zero / empty: rendered muted, since it is context not signal. */
  muted: boolean;
  known: boolean;
}

/**
 * One renderable chip per key of a tick summary.
 *
 * Known keys get a label and a tooltip; unknown ones get the key with its
 * underscores relaxed. Numbers, strings and booleans render inline;
 * anything structured renders as compact JSON with the full value in a
 * `title`. Nothing is dropped — a counter this UI has never heard of is
 * exactly the counter most likely to explain a new failure mode.
 */
function summaryEntries(summary: Record<string, unknown>): Entry[] {
  return Object.entries(summary).map(([key, value]) => {
    const known = COUNTERS[key];
    let text: string;
    let muted = false;
    if (typeof value === "number") {
      text = String(value);
      muted = value === 0;
    } else if (typeof value === "string") {
      text = value;
      muted = value === "";
    } else if (typeof value === "boolean") {
      text = value ? "yes" : "no";
      muted = !value;
    } else if (value === null || value === undefined) {
      text = "—";
      muted = true;
    } else {
      text = JSON.stringify(value);
      muted = text === "{}" || text === "[]";
    }
    return {
      key,
      label: known?.label ?? humanise(key),
      help: known?.help,
      value: text,
      muted,
      known: Boolean(known),
    };
  });
}

function SummaryChips({ summary }: { summary: Record<string, unknown> }) {
  const entries = summaryEntries(summary);
  if (entries.length === 0) {
    return (
      <span className="text-xs text-gray-500 dark:text-gray-400">
        No counters reported.
      </span>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map((entry) => (
        <span
          key={entry.key}
          title={entry.help ?? `${entry.key}: ${entry.value}`}
          className={`inline-flex items-baseline gap-1 rounded px-1.5 py-0.5 text-xs ${
            entry.muted
              ? "bg-gray-50 text-gray-400 dark:bg-gray-800 dark:text-gray-500"
              : "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200"
          }`}
        >
          <span>{entry.label}</span>
          <span
            className={
              entry.muted ? "" : "font-medium text-gray-900 dark:text-gray-100"
            }
          >
            {entry.value}
          </span>
        </span>
      ))}
    </div>
  );
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const known = OUTCOMES[outcome];
  return (
    <span
      title={known?.help ?? `Tick outcome reported by the scheduler: ${outcome}`}
      className={`inline-flex flex-shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        known?.tone ?? "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
      }`}
    >
      {known?.label ?? humanise(outcome)}
    </span>
  );
}

function SummaryRow({ summary }: { summary: BuildTickSummary }) {
  return (
    <li className="space-y-1 py-1.5">
      <div className="flex items-center gap-2">
        <OutcomeBadge outcome={summary.outcome} />
        <span
          className="text-xs text-gray-500 dark:text-gray-400"
          title={formatAbsoluteTime(summary.created_at)}
        >
          {formatRelativeTime(summary.created_at)}
        </span>
      </div>
      <SummaryChips summary={summary.summary} />
    </li>
  );
}

interface TickSummaryTrailProps {
  summaries: BuildTickSummary[];
  loading: boolean;
  /** True when the server has no tick-summaries endpoint (404). */
  unavailable: boolean;
  error: string | null;
  /** Ticks shown before the "show earlier" disclosure. */
  compactCount?: number;
}

/**
 * The scheduler's own account of what it did, newest first.
 *
 * A stalled build repeats the same outcome tick after tick, so the trail
 * reads far better than a single row: "lingered out ×6, claim denied 3
 * every time" is a diagnosis, while one row of it is an anecdote. Kept to
 * the two most recent by default and expandable from there.
 */
export function TickSummaryTrail({
  summaries,
  loading,
  unavailable,
  error,
  compactCount = 2,
}: TickSummaryTrailProps) {
  const [expanded, setExpanded] = useState(false);

  if (loading) {
    return (
      <p className="text-xs text-gray-500 dark:text-gray-400">Loading tick history…</p>
    );
  }
  if (unavailable) {
    return (
      <p className="text-xs text-gray-500 dark:text-gray-400">
        This server does not record tick history, so the scheduler&rsquo;s own reasoning
        is not available here — it is in the scheduler&rsquo;s logs.
      </p>
    );
  }
  if (error) {
    return (
      <p role="alert" className="text-xs text-red-600 dark:text-red-400">
        {error}
      </p>
    );
  }
  if (summaries.length === 0) {
    return (
      <p className="text-xs text-gray-500 dark:text-gray-400">
        No ticks recorded for this build yet.
      </p>
    );
  }

  const shown = expanded ? summaries : summaries.slice(0, compactCount);
  const hidden = summaries.length - shown.length;

  return (
    <div>
      <ul className="divide-y divide-gray-200 dark:divide-gray-700">
        {shown.map((summary) => (
          <SummaryRow key={summary.id} summary={summary} />
        ))}
      </ul>
      {(hidden > 0 || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-1 rounded text-xs text-blue-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400"
        >
          {expanded
            ? "Show fewer ticks"
            : `Show ${hidden} earlier tick${hidden === 1 ? "" : "s"}`}
        </button>
      )}
    </div>
  );
}
