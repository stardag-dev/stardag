import { useState } from "react";
import type { ExecutorMetadata } from "../types/task";
import { modalAppUrl } from "../utils/modalLinks";

interface ExecutorBadgeProps {
  executor?: string | null;
  executorRef?: string | null;
}

// Small chip identifying the executor backend a task last ran on
// ("⚡ Modal" for modal; the raw executor name otherwise). Renders
// nothing when no executor was recorded. When a call ref is present the
// tooltip shows it and clicking copies it to the clipboard.
export function ExecutorBadge({ executor, executorRef }: ExecutorBadgeProps) {
  const [copied, setCopied] = useState(false);

  if (!executor) return null;

  const label = executor === "modal" ? "⚡ Modal" : executor;
  const hasRef = Boolean(executorRef);

  const tooltip = copied
    ? "Copied!"
    : hasRef
      ? `Call ref: ${executorRef} (click to copy)`
      : `Executor: ${executor}`;

  const handleCopy = async (e: React.MouseEvent) => {
    // Badges render inside clickable rows/nodes — don't trigger those.
    e.stopPropagation();
    if (!executorRef) return;
    try {
      await navigator.clipboard.writeText(executorRef);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  const baseClasses =
    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium " +
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300";

  if (!hasRef) {
    return (
      <span className={baseClasses} title={tooltip}>
        {label}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={`${baseClasses} cursor-pointer hover:ring-1 hover:ring-emerald-400`}
      title={tooltip}
    >
      {label}
      {copied && <span aria-hidden="true">✓</span>}
    </button>
  );
}

interface BuildExecutorChipsProps {
  metadata?: ExecutorMetadata | null;
}

// Build-level executor chips: "Modal: {app_name}" linking to the app's
// dashboard page (plain chip when the link can't be built) plus a
// "reactive" badge for tick-scheduled builds. Renders nothing without
// recorded metadata.
export function BuildExecutorChips({ metadata }: BuildExecutorChipsProps) {
  if (!metadata) return null;

  const isModal = metadata.kind === "modal" || metadata.kind === undefined;
  const appName =
    typeof metadata.app_name === "string" && metadata.app_name.length > 0
      ? metadata.app_name
      : null;
  // Non-null appUrl implies a non-empty app_name (modalAppUrl requires it),
  // so the label needs no fallback.
  const appUrl = modalAppUrl(metadata);

  const modalChipLabel = isModal && appName ? `Modal: ${appName}` : null;

  // max-w + truncate: app_name is unvalidated server-side, so an oversized
  // value must not blow up the breadcrumb row (full label in the tooltip).
  const chipClasses =
    "inline-flex max-w-64 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium " +
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300";

  if (!modalChipLabel && metadata.reactive !== true) return null;

  return (
    <span className="inline-flex items-center gap-1.5">
      {modalChipLabel &&
        (appUrl ? (
          <a
            href={appUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className={`${chipClasses} hover:ring-1 hover:ring-emerald-400`}
            title={`${modalChipLabel} — open the Modal app dashboard`}
          >
            <span className="truncate">{modalChipLabel}</span>
          </a>
        ) : (
          <span className={chipClasses} title={modalChipLabel}>
            <span className="truncate">{modalChipLabel}</span>
          </span>
        ))}
      {metadata.reactive === true && (
        <span
          className="inline-flex items-center rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-800 dark:bg-sky-900/30 dark:text-sky-300"
          title="Reactive build: scheduled by tick functions, no resident orchestrator"
        >
          reactive
        </span>
      )}
    </span>
  );
}
