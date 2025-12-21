import { useCallback, useEffect, useState } from "react";
import {
  acceptInvite,
  declineInvite,
  fetchPendingInvites,
  type PendingInvite,
} from "../api/organizations";
import { useWorkspace } from "../context/WorkspaceContext";

interface PendingInvitesProps {
  /** Whether to show in compact mode (for header) */
  compact?: boolean;
}

export function PendingInvites({ compact = false }: PendingInvitesProps) {
  const [invites, setInvites] = useState<PendingInvite[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { refresh } = useWorkspace();

  const loadInvites = useCallback(async () => {
    try {
      setLoading(true);
      const data = await fetchPendingInvites();
      setInvites(data);
    } catch (err) {
      console.error("Failed to load invites:", err);
      setError("Failed to load invites");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInvites();
  }, [loadInvites]);

  const handleAccept = async (inviteId: string) => {
    try {
      setActionLoading(inviteId);
      setError(null);
      await acceptInvite(inviteId);
      // Remove from local state
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
      // Refresh workspace context to load new org
      await refresh();
    } catch (err) {
      console.error("Failed to accept invite:", err);
      setError("Failed to accept invite");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDecline = async (inviteId: string) => {
    try {
      setActionLoading(inviteId);
      setError(null);
      await declineInvite(inviteId);
      // Remove from local state
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
    } catch (err) {
      console.error("Failed to decline invite:", err);
      setError("Failed to decline invite");
    } finally {
      setActionLoading(null);
    }
  };

  if (loading) {
    return null;
  }

  if (invites.length === 0) {
    return null;
  }

  if (compact) {
    // Compact mode: show as a notification badge/dropdown
    return (
      <div className="relative">
        <div className="flex items-center gap-2 rounded-md bg-blue-50 dark:bg-blue-900/30 px-3 py-1.5 text-sm">
          <span className="text-blue-700 dark:text-blue-300">
            {invites.length} pending invite{invites.length > 1 ? "s" : ""}
          </span>
          <button
            onClick={() => window.history.pushState({}, "", "/invites")}
            className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-200 underline"
          >
            View
          </button>
        </div>
      </div>
    );
  }

  // Full mode: show all invites as cards
  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
        Pending Invitations
      </h2>

      {error && (
        <div className="rounded-md bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      <div className="space-y-3">
        {invites.map((invite) => (
          <div
            key={invite.id}
            className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <h3 className="font-medium text-gray-900 dark:text-gray-100">
                  {invite.organization_name}
                </h3>
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Role: <span className="capitalize">{invite.role}</span>
                </p>
                {invite.invited_by_email && (
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Invited by: {invite.invited_by_email}
                  </p>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => handleAccept(invite.id)}
                  disabled={actionLoading === invite.id}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {actionLoading === invite.id ? "..." : "Accept"}
                </button>
                <button
                  onClick={() => handleDecline(invite.id)}
                  disabled={actionLoading === invite.id}
                  className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
                >
                  Decline
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
