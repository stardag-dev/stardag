import { useEffect, type ReactNode } from "react";

// TODO(a11y): this modal does not trap focus. Escape closes it and the
// body is scroll-locked, but Tab walks straight out of the dialog into the
// page behind it, and focus is not restored to the trigger on close. Every
// caller (ChangePasswordModal, OnboardingModal, ConfirmDialog, …) inherits
// the gap. Fixing it means cycling Tab/Shift-Tab within the dialog,
// marking the rest of the page `aria-hidden`/`inert` while open, and
// remembering `document.activeElement` — a change to shared behaviour that
// deserves its own PR rather than riding along with a feature.

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  /** Whether clicking outside closes the modal */
  closeOnOverlay?: boolean;
  /** Whether to show the close button */
  showCloseButton?: boolean;
  /**
   * Tailwind max-width class for the dialog. Defaults to the narrow
   * form-sized dialog; widen it for content that has to be read and
   * compared (e.g. a dry-run preview listing what is about to change).
   */
  maxWidthClass?: string;
}

export function Modal({
  isOpen,
  onClose,
  title,
  children,
  closeOnOverlay = true,
  showCloseButton = true,
  maxWidthClass = "max-w-md",
}: ModalProps) {
  // Handle escape key
  useEffect(() => {
    if (!isOpen) return;

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };

    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [isOpen, onClose]);

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 transition-opacity"
        onClick={closeOnOverlay ? onClose : undefined}
      />

      {/* Modal content */}
      <div
        className={`relative z-10 w-full ${maxWidthClass} rounded-lg bg-white dark:bg-gray-800 shadow-xl mx-4`}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {title}
          </h2>
          {showCloseButton && (
            <button
              onClick={onClose}
              className="rounded-md p-1 text-gray-400 hover:text-gray-500 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              <svg
                className="h-5 w-5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          )}
        </div>

        {/* Body */}
        <div className="px-6 py-4">{children}</div>
      </div>
    </div>
  );
}
