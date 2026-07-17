import { useEffect, useRef, useState, type FormEvent } from "react";
import { changePasswordLocal } from "../api/auth";
import { useAuth } from "../context/AuthContext";
import { Modal } from "./Modal";

const MIN_PASSWORD_LENGTH = 8;

interface ChangePasswordModalProps {
  isOpen: boolean;
  onClose: () => void;
}

/**
 * Change-password dialog for local auth mode (email/password accounts
 * managed by the API). Verifies the current password server-side via
 * POST /api/v1/auth/change-password.
 */
export function ChangePasswordModal({ isOpen, onClose }: ChangePasswordModalProps) {
  const { getAccessToken } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const closeTimeoutRef = useRef<number | null>(null);

  // Clear any pending auto-close timer on unmount
  useEffect(() => {
    return () => {
      if (closeTimeoutRef.current !== null) {
        window.clearTimeout(closeTimeoutRef.current);
      }
    };
  }, []);

  function resetAndClose() {
    if (closeTimeoutRef.current !== null) {
      window.clearTimeout(closeTimeoutRef.current);
      closeTimeoutRef.current = null;
    }
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setError(null);
    setIsSubmitting(false);
    setSuccess(false);
    onClose();
  }

  /**
   * User-initiated close (Cancel, X button, overlay click, Escape).
   * Ignored while the request is in flight so an in-flight submit can't
   * later flip to the success state / schedule a stale delayed close.
   */
  function handleClose() {
    if (isSubmitting) return;
    resetAndClose();
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);

    // Client-side checks (the API enforces the policy authoritatively)
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setError(`New password must be at least ${MIN_PASSWORD_LENGTH} characters`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New passwords do not match");
      return;
    }

    setIsSubmitting(true);
    try {
      // No workspace ID: returns the user-scoped session token in local mode
      const token = await getAccessToken(null);
      if (!token) {
        throw new Error("Your session has expired — please sign in again");
      }
      await changePasswordLocal(currentPassword, newPassword, token);
      setIsSubmitting(false);
      setSuccess(true);
      // Brief confirmation, then close (dismissable early by the user)
      closeTimeoutRef.current = window.setTimeout(resetAndClose, 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to change password");
      setIsSubmitting(false);
    }
  }

  const inputClassName =
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100";
  const labelClassName =
    "mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300";

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title="Change password">
      {success ? (
        <p className="py-2 text-sm text-green-600 dark:text-green-400" role="status">
          Password changed successfully.
        </p>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="currentPassword" className={labelClassName}>
              Current password
            </label>
            <input
              id="currentPassword"
              type="password"
              required
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoComplete="current-password"
              className={inputClassName}
            />
          </div>
          <div>
            <label htmlFor="newPassword" className={labelClassName}>
              New password
            </label>
            <input
              id="newPassword"
              type="password"
              required
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
              className={inputClassName}
            />
          </div>
          <div>
            <label htmlFor="confirmPassword" className={labelClassName}>
              Confirm new password
            </label>
            <input
              id="confirmPassword"
              type="password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              autoComplete="new-password"
              className={inputClassName}
            />
          </div>
          {error && (
            <p className="text-sm text-red-600 dark:text-red-400" role="alert">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={handleClose}
              disabled={isSubmitting}
              className="rounded-md px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isSubmitting ? "Changing…" : "Change password"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
