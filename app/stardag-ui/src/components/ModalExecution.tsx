import { useState } from "react";
import type { ExecutorMetadata } from "../types/task";
import {
  isModalMetadata,
  modalAppUrl,
  modalEnvironmentUrl,
  modalFunctionCallUrl,
  modalFunctionUrl,
} from "../utils/modalLinks";

export function CopyButton({
  text,
  className = "",
}: {
  text: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  return (
    <button
      onClick={handleCopy}
      className={`p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 ${className}`}
      title={copied ? "Copied!" : "Copy to clipboard"}
    >
      {copied ? (
        <svg
          className="h-4 w-4 text-green-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M5 13l4 4L19 7"
          />
        </svg>
      ) : (
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
          />
        </svg>
      )}
    </button>
  );
}

// The Modal function-call reference (fc-…) for an execution: the raw id as a
// deep link to the function call in the Modal dashboard when a genuine
// call-level URL is resolvable, otherwise plain text, always followed by a
// click-to-copy button.
//
// The call-level link is gated on modalFunctionUrl being non-null.
// modalFunctionCallUrl falls back to the app page when function_id is missing
// (other callers, e.g. the "View on Modal" links, rely on that shared
// fallback), but here a clickable call ref must never navigate to a coarser
// level — so we only link when the function itself is addressable; otherwise
// the ref renders as plain text (the copy button stays either way).
//
// Gated to Modal executions (reuses isModalMetadata): renders for modal
// metadata and the legacy kind-less case (treated as modal). Renders nothing
// for an explicitly non-modal kind, or when there is no call ref to show.
export function ModalExecutionCallRef({
  metadata,
  executorRef,
}: {
  metadata?: ExecutorMetadata | null;
  executorRef?: string | null;
}) {
  const isModal = !metadata || isModalMetadata(metadata);
  if (!isModal) return null;

  const fcId =
    typeof executorRef === "string" && executorRef.length > 0 ? executorRef : null;
  if (!fcId) return null;

  const callUrl = modalFunctionUrl(metadata)
    ? modalFunctionCallUrl(metadata, fcId)
    : null;

  return (
    <div className="flex items-center gap-1 font-mono">
      {callUrl ? (
        <a
          href={callUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="break-all text-blue-600 hover:underline dark:text-blue-400"
        >
          {fcId}
        </a>
      ) : (
        <span className="break-all">{fcId}</span>
      )}
      <CopyButton text={fcId} className="flex-shrink-0" />
    </div>
  );
}

// Collapsible "more details" block: a 2-column table of the captured Modal
// identifiers verbatim, each value click-to-copy. Rows are grouped top-down —
// human-readable names first (Workspace, Environment, App, Function), then a
// hairline divider, then the raw ids (App ID, Function ID, Call ref). Modal
// gives no URL-format guarantee (see utils/modalLinks.ts), so surfacing the
// raw ids lets a user reconstruct or paste a reference by hand even if the
// dashboard URL format drifts. Only present fields render; the divider shows
// only when both groups are non-empty; renders nothing when none are.
//
// Gated to Modal executions: this block's labels ("App", "Function ID", …)
// are Modal-specific, so it renders only for modal metadata, for the legacy
// kind-less case (treated as modal for back-compat), and for the
// executorRef-only case (no metadata at all). It never renders Modal-labeled
// fields for an explicitly non-modal kind (e.g. "k8s").
export function ModalExecutionDetails({
  metadata,
  executorRef,
}: {
  metadata?: ExecutorMetadata | null;
  executorRef?: string | null;
}) {
  const [open, setOpen] = useState(false);

  const isModal = !metadata || isModalMetadata(metadata);

  const collect = (entries: [string, unknown, string | null][]) => {
    const rows: { label: string; value: string; url: string | null }[] = [];
    for (const [label, value, url] of entries) {
      if (typeof value === "string" && value.length > 0) {
        rows.push({ label, value, url });
      }
    }
    return rows;
  };

  // Best-effort Modal dashboard links per level (null when not resolvable —
  // see utils/modalLinks.ts). Workspace has no meaningful standalone URL, so
  // it stays plain text. The call ref is gated on the function being
  // addressable (modalFunctionUrl non-null) so it never links to a coarser
  // level; see ModalExecutionCallRef.
  const appUrl = modalAppUrl(metadata);
  const funcUrl = modalFunctionUrl(metadata);
  const callUrl = funcUrl ? modalFunctionCallUrl(metadata, executorRef) : null;

  // Human-readable names first, then the raw object ids.
  const names = collect([
    ["Workspace", metadata?.workspace, null],
    ["Environment", metadata?.environment, modalEnvironmentUrl(metadata)],
    ["App", metadata?.app_name, appUrl],
    ["Function", metadata?.function_name, funcUrl],
  ]);
  const ids = collect([
    ["App ID", metadata?.app_id, appUrl],
    ["Function ID", metadata?.function_id, funcUrl],
    ["Call ref", executorRef, callUrl],
  ]);

  if (!isModal || (names.length === 0 && ids.length === 0)) return null;

  const renderRow = (field: { label: string; value: string; url: string | null }) => (
    <tr key={field.label}>
      <th
        scope="row"
        className="whitespace-nowrap py-0.5 pr-3 align-top font-normal text-gray-500 dark:text-gray-400"
      >
        {field.label}
      </th>
      <td className="py-0.5 align-top">
        <span className="flex items-start gap-1">
          {field.url ? (
            <a
              href={field.url}
              target="_blank"
              rel="noopener noreferrer"
              className="break-all font-mono text-blue-600 hover:underline dark:text-blue-400"
            >
              {field.value}
            </a>
          ) : (
            <span className="break-all font-mono">{field.value}</span>
          )}
          <CopyButton text={field.value} className="flex-shrink-0" />
        </span>
      </td>
    </tr>
  );

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
      >
        <svg
          className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9 5l7 7-7 7"
          />
        </svg>
        {open ? "Hide details" : "More details"}
      </button>
      {open && (
        <table className="mt-1 w-full text-left align-top">
          <tbody>
            {names.map(renderRow)}
            {names.length > 0 && ids.length > 0 && (
              <tr aria-hidden="true">
                {/* Same total height as py-1, but the hairline is nudged down
                    (less top, more bottom padding) to sit visually centered
                    between the groups: the align-top value rows leave
                    line-height descender slack above the divider, so equal
                    padding would render the line too close to the group
                    below. */}
                <td colSpan={2} className="pt-0.5 pb-1.5">
                  <hr className="border-gray-200 dark:border-gray-700" />
                </td>
              </tr>
            )}
            {ids.map(renderRow)}
          </tbody>
        </table>
      )}
    </div>
  );
}
