import { useCallback, useEffect, useRef, useState } from "react";
import { useEnvironment } from "../context/EnvironmentContext";
import { useAuth } from "../context/AuthContext";
import { useClickOutside } from "../hooks/useClickOutside";
import { fetchPendingInvites, type PendingInvite } from "../api/workspaces";
import { CRUMB_MENU, CRUMB_TRIGGER, CrumbChevron } from "./ui/Crumb";

/**
 * The workspace crumb: the first step in the header trail.
 *
 * It switches workspaces and nothing else. Environments moved out to
 * `EnvironmentSelector`, which is a sibling crumb rather than a section
 * inside this dropdown — see that file for why.
 *
 * Token exchange happens automatically when switching workspaces, and
 * the trigger shows it: the avatar carries a pulsing dot while the new
 * workspace's token is being fetched, because until it lands the rest of
 * the page is still showing the old workspace's data.
 */
export function WorkspaceSelector() {
  const { isAuthenticated } = useAuth();
  const {
    workspaces,
    activeWorkspace,
    setActiveWorkspace,
    isLoading,
    isExchangingToken,
  } = useEnvironment();

  const [isOpen, setIsOpen] = useState(false);
  const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Load pending invites when authenticated (regardless of workspace count)
  useEffect(() => {
    if (isAuthenticated && !isLoading) {
      fetchPendingInvites()
        .then(setPendingInvites)
        .catch((err) => console.error("Failed to load invites:", err));
    }
  }, [isAuthenticated, isLoading]);

  const closeDropdown = useCallback(() => setIsOpen(false), []);
  useClickOutside(dropdownRef, isOpen, closeDropdown);

  // Don't show if not authenticated
  if (!isAuthenticated) {
    return null;
  }

  // Show loading state
  if (isLoading) {
    return (
      <div className="flex items-center gap-3">
        <div className="h-9 w-9 rounded-lg bg-gray-200 dark:bg-gray-700 animate-pulse" />
        <div className="h-5 w-24 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" />
      </div>
    );
  }

  // No workspaces yet - show appropriate action
  if (workspaces.length === 0) {
    // If there are pending invites, show link to view them
    if (pendingInvites.length > 0) {
      return (
        <button
          onClick={() => {
            window.history.pushState({}, "", "/invites");
            window.dispatchEvent(new PopStateEvent("popstate"));
          }}
          className="flex items-center gap-2 rounded-lg bg-blue-50 dark:bg-blue-900/30 px-3 py-2 text-sm text-blue-700 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-900/50"
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
              d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
            />
          </svg>
          {pendingInvites.length} pending invite
          {pendingInvites.length > 1 ? "s" : ""}
        </button>
      );
    }

    // Otherwise, prompt to create workspace
    return (
      <button
        onClick={() => {
          window.history.pushState({}, "", "/workspaces/new");
          window.dispatchEvent(new PopStateEvent("popstate"));
        }}
        className="flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
      >
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M12 4v16m8-8H4"
          />
        </svg>
        Create Workspace
      </button>
    );
  }

  // Count workspaces where user is owner (limit is 3)
  const ownedWorkspacesCount = workspaces.filter((w) => w.role === "owner").length;
  const canCreateWorkspace = ownedWorkspacesCount < 3;

  // Get first letter of workspace name for avatar
  const workspaceInitial = activeWorkspace?.name.charAt(0).toUpperCase() || "?";

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        disabled={isExchangingToken}
        aria-expanded={isOpen}
        aria-haspopup="menu"
        className={CRUMB_TRIGGER}
        title={
          activeWorkspace
            ? `Workspace ${activeWorkspace.name} — switch workspace`
            : "Select a workspace"
        }
      >
        {/* A 24px mark, not a 36px tile: at the trail's type size a
            larger avatar sets its own baseline and drags the row out of
            alignment, which is the misalignment this row had. */}
        <span className="relative flex-shrink-0">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-gradient-to-br from-blue-500 to-purple-600 text-xs font-bold text-white">
            {workspaceInitial}
          </span>
          {isExchangingToken && (
            <span
              title="Switching workspace…"
              className="absolute -right-0.5 -bottom-0.5 h-2 w-2 animate-pulse rounded-full border border-white bg-yellow-400 dark:border-gray-800"
            />
          )}
          {!isExchangingToken && pendingInvites.length > 0 && (
            <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-white bg-orange-500 text-[9px] font-bold text-white dark:border-gray-800">
              {pendingInvites.length > 9 ? "9+" : pendingInvites.length}
            </span>
          )}
        </span>
        <span className="max-w-[12rem] truncate font-medium text-gray-900 dark:text-gray-100">
          {activeWorkspace?.name ?? "Select workspace"}
        </span>
        <CrumbChevron open={isOpen} />
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div role="menu" className={`${CRUMB_MENU} min-w-[19rem]`}>
          {/* Current workspace header */}
          {activeWorkspace && (
            <div className="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-4 py-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 text-white font-bold text-xl">
                  {workspaceInitial}
                </div>
                <div>
                  <div className="font-semibold text-gray-900 dark:text-gray-100">
                    {activeWorkspace.name}
                  </div>
                  <div className="text-xs text-gray-500 dark:text-gray-400 capitalize">
                    {activeWorkspace.role}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Switch workspace section */}
          {workspaces.length > 1 && (
            <div className="border-b border-gray-200 dark:border-gray-700 py-2">
              <div className="px-4 py-1.5 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
                Switch Workspace
              </div>
              {workspaces
                .filter((workspace) => workspace.id !== activeWorkspace?.id)
                .map((workspace) => (
                  <button
                    key={workspace.id}
                    onClick={() => {
                      setActiveWorkspace(workspace);
                      // Don't close immediately - show loading state
                    }}
                    disabled={isExchangingToken}
                    className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                  >
                    <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-gray-400 to-gray-600 text-white font-semibold text-sm">
                      {workspace.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="truncate">{workspace.name}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 capitalize">
                        {workspace.role}
                      </div>
                    </div>
                  </button>
                ))}
            </div>
          )}

          {/* Pending Invites section */}
          {pendingInvites.length > 0 && (
            <div className="border-b border-gray-200 dark:border-gray-700 py-2">
              <button
                onClick={() => {
                  setIsOpen(false);
                  window.history.pushState({}, "", "/invites");
                  window.dispatchEvent(new PopStateEvent("popstate"));
                }}
                className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm text-orange-600 dark:text-orange-400 hover:bg-gray-100 dark:hover:bg-gray-700"
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
                    d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
                  />
                </svg>
                <span>
                  {pendingInvites.length} pending invite
                  {pendingInvites.length > 1 ? "s" : ""}
                </span>
              </button>
            </div>
          )}

          {/* Actions */}
          <div className="py-2">
            {canCreateWorkspace ? (
              <button
                onClick={() => {
                  setIsOpen(false);
                  window.history.pushState({}, "", "/workspaces/new");
                  window.dispatchEvent(new PopStateEvent("popstate"));
                }}
                className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm text-blue-600 dark:text-blue-400 hover:bg-gray-100 dark:hover:bg-gray-700"
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
                    d="M12 4v16m8-8H4"
                  />
                </svg>
                Create New Workspace
              </button>
            ) : (
              <div className="px-4 py-2 text-sm text-gray-500 dark:text-gray-400">
                <div className="flex items-center gap-3">
                  <svg
                    className="h-5 w-5 text-gray-400"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M12 4v16m8-8H4"
                    />
                  </svg>
                  <span>Workspace limit reached</span>
                </div>
                <p className="mt-1 ml-8 text-xs">
                  You&apos;ve created {ownedWorkspacesCount} workspaces (max 3). Delete
                  an existing workspace to create a new one.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
