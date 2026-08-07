import type { ReactNode } from "react";
import { Modal } from "../Modal";
import { ResultBanner } from "./ResultBanner";

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  /** Body: prose, options, and — for bulk actions — a preview of the effect. */
  children?: ReactNode;
  confirmLabel: string;
  /** Label while the action is in flight (defaults to confirmLabel + "…"). */
  busyLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  /** Red confirm button and an alert-toned error area. */
  destructive?: boolean;
  confirmDisabled?: boolean;
  busy?: boolean;
  /** Failure of the confirmed action, rendered above the buttons. */
  error?: string | null;
  /** Tailwind max-width class forwarded to the underlying Modal. */
  maxWidthClass?: string;
}

/**
 * Confirmation dialog for actions whose consequences are worth showing
 * before they happen.
 *
 * Composed from ``Modal`` (escape-to-close, overlay, scroll lock) rather
 * than being a third dialog implementation. The body is free-form so a
 * destructive bulk action can render its dry-run preview inside the same
 * dialog the user will confirm from — see ``BulkCancelDialog``.
 *
 * Focus lands on Cancel, not on the destructive button: an accidental
 * Enter must not commit.
 */
export function ConfirmDialog({
  isOpen,
  title,
  children,
  confirmLabel,
  busyLabel,
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
  destructive = false,
  confirmDisabled = false,
  busy = false,
  error = null,
  maxWidthClass = "max-w-md",
}: ConfirmDialogProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onCancel}
      title={title}
      maxWidthClass={maxWidthClass}
      closeOnOverlay={!busy}
    >
      <div className="space-y-4">
        {children}

        {error && <ResultBanner tone="error">{error}</ResultBanner>}

        <div className="flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={confirmDisabled || busy}
            className={`rounded-md px-3 py-1.5 text-sm font-medium text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:ring-offset-gray-800 ${
              destructive
                ? "bg-red-600 hover:bg-red-700 focus-visible:ring-red-500"
                : "bg-blue-600 hover:bg-blue-700 focus-visible:ring-blue-500"
            }`}
          >
            {busy ? busyLabel ?? `${confirmLabel}…` : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
