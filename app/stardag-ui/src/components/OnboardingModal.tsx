import { useCallback, useEffect, useState } from "react";
import {
  acceptInvite,
  declineInvite,
  fetchPendingInvites,
  type PendingInvite,
} from "../api/organizations";
import { useEnvironment } from "../context/EnvironmentContext";
import { useAuth } from "../context/AuthContext";
import { Modal } from "./Modal";

const DISMISSED_KEY = "stardag_onboarding_dismissed";

/**
 * Modal that appears on first login to prompt users to:
 * 1. Accept/decline pending invites (if any)
 * 2. Create their first organization (if they have none)
 */
export function OnboardingModal() {
  const { isAuthenticated } = useAuth();
  const { organizations, isLoading, refresh } = useEnvironment();

  const [invites, setInvites] = useState<PendingInvite[]>([]);
  const [invitesLoading, setInvitesLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(() => {
    return sessionStorage.getItem(DISMISSED_KEY) === "true";
  });
  const [hidden, setHidden] = useState(false);

  // Load pending invites
  useEffect(() => {
    if (!isAuthenticated) {
      setInvites([]);
      setInvitesLoading(false);
      return;
    }

    fetchPendingInvites()
      .then(setInvites)
      .catch((err) => {
        console.error("Failed to load invites:", err);
      })
      .finally(() => setInvitesLoading(false));
  }, [isAuthenticated]);

  const handleAccept = async (inviteId: string) => {
    try {
      setActionLoading(inviteId);
      setError(null);
      await acceptInvite(inviteId);
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
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
      setInvites((prev) => prev.filter((i) => i.id !== inviteId));
    } catch (err) {
      console.error("Failed to decline invite:", err);
      setError("Failed to decline invite");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDismiss = useCallback(() => {
    setDismissed(true);
    // Only persist dismissal if user has orgs
    // If no orgs, modal will reappear on next login
    if (organizations.length > 0) {
      sessionStorage.setItem(DISMISSED_KEY, "true");
    }
  }, [organizations.length]);

  const navigateToCreateOrg = () => {
    setHidden(true);
    window.history.pushState({}, "", "/organizations/new");
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  // Don't show if not authenticated, still loading, or hidden
  if (!isAuthenticated || isLoading || invitesLoading || hidden) {
    return null;
  }

  // Determine what to show
  const hasInvites = invites.length > 0;
  const hasOrgs = organizations.length > 0;

  // If user has orgs and no invites, nothing to prompt
  if (hasOrgs && !hasInvites) {
    return null;
  }

  // If user has orgs and dismissed, don't show
  // But if user has NO orgs, always show (blocking) - they must create or join one
  if (hasOrgs && dismissed) {
    return null;
  }

  // Determine if modal should be blocking (no dismiss option)
  const isBlocking = !hasOrgs;

  // Show pending invites modal
  if (hasInvites) {
    return (
      <Modal
        isOpen={true}
        onClose={isBlocking ? () => {} : handleDismiss}
        title={isBlocking ? "Get Started" : "Pending Invitations"}
        closeOnOverlay={false}
        showCloseButton={!isBlocking}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            You have {invites.length} pending organization invitation
            {invites.length > 1 ? "s" : ""}.
          </p>

          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300">
              {error}
            </div>
          )}

          <div className="space-y-3 max-h-64 overflow-y-auto">
            {invites.map((invite) => (
              <div
                key={invite.id}
                className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50 p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <h3 className="font-medium text-gray-900 dark:text-gray-100 truncate">
                      {invite.organization_name}
                    </h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Role: <span className="capitalize">{invite.role}</span>
                      {invite.invited_by_email && (
                        <> &middot; From: {invite.invited_by_email}</>
                      )}
                    </p>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => handleAccept(invite.id)}
                      disabled={actionLoading === invite.id}
                      className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                    >
                      {actionLoading === invite.id ? "..." : "Accept"}
                    </button>
                    <button
                      onClick={() => handleDecline(invite.id)}
                      disabled={actionLoading === invite.id}
                      className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
                    >
                      Decline
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center justify-between gap-3 pt-2 border-t border-gray-200 dark:border-gray-700">
            {/* Show create org option if user has no orgs */}
            {isBlocking && (
              <button
                onClick={navigateToCreateOrg}
                className="text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 underline"
              >
                Or create a new organization
              </button>
            )}
            {!isBlocking && (
              <button
                onClick={handleDismiss}
                className="rounded-md px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                Decide Later
              </button>
            )}
          </div>
        </div>
      </Modal>
    );
  }

  // Show create organization modal (no orgs and no invites)
  // This is always blocking since user has no orgs
  return (
    <Modal
      isOpen={true}
      onClose={() => {}}
      title="Welcome to Stardag"
      closeOnOverlay={false}
      showCloseButton={false}
    >
      <div className="space-y-4">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          To get started, you need to create an organization. Organizations help you
          manage environments, team members, and API access.
        </p>

        <div className="flex justify-end pt-2">
          <button
            onClick={navigateToCreateOrg}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Create Organization
          </button>
        </div>
      </div>
    </Modal>
  );
}
