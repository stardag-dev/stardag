import { useCallback, useEffect, useState } from "react";
import {
  acceptInvite,
  declineInvite,
  fetchPendingInvites,
  type PendingInvite,
} from "../api/workspaces";
import { useEnvironment } from "../context/EnvironmentContext";
import { useAuth } from "../context/AuthContext";
import { Modal } from "./Modal";

const DISMISSED_KEY = "stardag_onboarding_dismissed";

/**
 * Modal that appears on first login to prompt users to:
 * 1. Accept/decline pending invites (if any)
 * 2. Create their first workspace (if they have none)
 */
export function OnboardingModal() {
  const { isAuthenticated } = useAuth();
  const { workspaces, isLoading, refresh } = useEnvironment();

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
    // Only persist dismissal if user has workspaces
    // If no workspaces, modal will reappear on next login
    if (workspaces.length > 0) {
      sessionStorage.setItem(DISMISSED_KEY, "true");
    }
  }, [workspaces.length]);

  const navigateToCreateWorkspace = () => {
    setHidden(true);
    window.history.pushState({}, "", "/workspaces/new");
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  // Don't show if not authenticated, still loading, or hidden
  if (!isAuthenticated || isLoading || invitesLoading || hidden) {
    return null;
  }

  // Determine what to show
  const hasInvites = invites.length > 0;
  const hasWorkspaces = workspaces.length > 0;
  // Check for personal workspace - either explicitly marked or effectively personal
  // (single workspace where user is owner with is_personal undefined - for backwards
  // compatibility with data created before is_personal field was added)
  const hasOnlyPersonalWorkspace =
    hasWorkspaces &&
    workspaces.length === 1 &&
    workspaces[0].role === "owner" &&
    workspaces[0].is_personal !== false;

  // Count workspaces where user is owner (limit is 3)
  const ownedWorkspacesCount = workspaces.filter((w) => w.role === "owner").length;
  const canCreateWorkspace = ownedWorkspacesCount < 3;

  // If user has workspaces (not just personal) and no invites, nothing to prompt
  if (hasWorkspaces && !hasOnlyPersonalWorkspace && !hasInvites) {
    return null;
  }

  // If user has workspaces (not just personal) and dismissed, don't show
  if (hasWorkspaces && !hasOnlyPersonalWorkspace && dismissed) {
    return null;
  }

  // If user has only personal workspace and dismissed, don't show
  if (hasOnlyPersonalWorkspace && dismissed) {
    return null;
  }

  // Determine if modal should be blocking (no dismiss option)
  // Users with only personal workspaces can dismiss (they have a workspace to use)
  const isBlocking = !hasWorkspaces;

  // Show welcome modal for users with only a personal workspace (with or without invites)
  if (hasOnlyPersonalWorkspace) {
    const personalWorkspace = workspaces[0];
    return (
      <Modal
        isOpen={true}
        onClose={handleDismiss}
        title="Welcome to Stardag!"
        closeOnOverlay={false}
        showCloseButton={true}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            A personal workspace &quot;{personalWorkspace.name}&quot; has been created
            for you with a &quot;local&quot; environment. This is your space to explore
            Stardag.
          </p>

          {/* Show pending invites if any */}
          {hasInvites && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className="h-2 w-2 rounded-full bg-orange-500" />
                <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  You have {invites.length} pending invitation
                  {invites.length > 1 ? "s" : ""}
                </h3>
              </div>

              {error && (
                <div className="rounded-md bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300">
                  {error}
                </div>
              )}

              <div className="space-y-2 max-h-48 overflow-y-auto">
                {invites.map((invite) => (
                  <div
                    key={invite.id}
                    className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50 p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <h4 className="font-medium text-gray-900 dark:text-gray-100 truncate">
                          {invite.workspace_name}
                        </h4>
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
            </div>
          )}

          {/* Show helpful links only if no invites */}
          {!hasInvites && (
            <div className="flex flex-col gap-2 py-2">
              <a
                href="https://docs.stardag.com/getting-started"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
              >
                Getting Started Guide
              </a>
              <a
                href="https://docs.stardag.com/configuration/environments/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
              >
                About Workspaces and Environments
              </a>
            </div>
          )}

          {canCreateWorkspace ? (
            <p className="text-xs text-gray-500 dark:text-gray-500">
              You can{" "}
              <button
                onClick={navigateToCreateWorkspace}
                className="text-blue-600 dark:text-blue-400 hover:underline"
              >
                create additional workspaces
              </button>{" "}
              later to collaborate with others.
            </p>
          ) : (
            <p className="text-xs text-gray-500 dark:text-gray-500">
              You&apos;ve reached the workspace limit ({ownedWorkspacesCount}/3).
            </p>
          )}

          <div className="flex justify-end pt-2 border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={handleDismiss}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              Get Started
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  // Show pending invites modal for users with multiple workspaces (non-personal)
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
            You have {invites.length} pending workspace invitation
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
                      {invite.workspace_name}
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
            {/* Show create workspace option if user has no workspaces and can create */}
            {isBlocking && canCreateWorkspace && (
              <button
                onClick={navigateToCreateWorkspace}
                className="text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 underline"
              >
                Or create a new workspace
              </button>
            )}
            {isBlocking && !canCreateWorkspace && (
              <span className="text-sm text-gray-500 dark:text-gray-400">
                Workspace limit reached ({ownedWorkspacesCount}/3)
              </span>
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

  // Show create workspace modal (no workspaces and no invites)
  // This is always blocking since user has no workspaces
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
          To get started, you need to create a workspace. Workspaces help you manage
          environments, team members, and API access.
        </p>

        <div className="flex justify-end pt-2">
          <button
            onClick={navigateToCreateWorkspace}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Create Workspace
          </button>
        </div>
      </div>
    </Modal>
  );
}
