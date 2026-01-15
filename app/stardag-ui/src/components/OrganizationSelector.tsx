import { useState, useRef, useEffect } from "react";
import { useEnvironment } from "../context/EnvironmentContext";
import { useAuth } from "../context/AuthContext";
import { fetchPendingInvites, type PendingInvite } from "../api/organizations";

/**
 * Slack-like organization selector component.
 *
 * Displays the current organization prominently in the top-left,
 * with a dropdown to switch organizations or create new ones.
 * Token exchange happens automatically when switching orgs.
 */
export function OrganizationSelector() {
  const { isAuthenticated } = useAuth();
  const {
    organizations,
    activeOrg,
    setActiveOrg,
    environments,
    activeEnvironment,
    setActiveEnvironment,
    isLoading,
    isExchangingToken,
  } = useEnvironment();

  const [isOpen, setIsOpen] = useState(false);
  const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Load pending invites when authenticated and no orgs
  useEffect(() => {
    if (isAuthenticated && organizations.length === 0 && !isLoading) {
      fetchPendingInvites()
        .then(setPendingInvites)
        .catch((err) => console.error("Failed to load invites:", err));
    }
  }, [isAuthenticated, organizations.length, isLoading]);

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

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

  // No organizations yet - show appropriate action
  if (organizations.length === 0) {
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

    // Otherwise, prompt to create organization
    return (
      <button
        onClick={() => {
          window.history.pushState({}, "", "/organizations/new");
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
        Create Organization
      </button>
    );
  }

  // Get first letter of org name for avatar
  const orgInitial = activeOrg?.name.charAt(0).toUpperCase() || "?";

  return (
    <div className="relative" ref={dropdownRef}>
      {/* Main button - Slack-like org selector */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        disabled={isExchangingToken}
        className="flex items-center gap-3 rounded-lg px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors disabled:opacity-50"
      >
        {/* Org avatar */}
        <div className="relative">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 text-white font-bold text-lg">
            {orgInitial}
          </div>
          {/* Token exchange indicator */}
          {isExchangingToken && (
            <div className="absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-white dark:border-gray-800 bg-yellow-400 animate-pulse" />
          )}
        </div>

        {/* Org and environment name */}
        <div className="flex flex-col items-start min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="font-semibold text-gray-900 dark:text-gray-100 truncate max-w-[150px]">
              {activeOrg?.name ?? "Select org"}
            </span>
            <svg
              className={`h-4 w-4 text-gray-500 transition-transform ${
                isOpen ? "rotate-180" : ""
              }`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 9l-7 7-7-7"
              />
            </svg>
          </div>
          {activeEnvironment && (
            <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[150px]">
              {activeEnvironment.name}
            </span>
          )}
        </div>
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div className="absolute left-0 top-full z-50 mt-2 min-w-[300px] rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-xl overflow-hidden">
          {/* Current org header */}
          {activeOrg && (
            <div className="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-4 py-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 text-white font-bold text-xl">
                  {orgInitial}
                </div>
                <div>
                  <div className="font-semibold text-gray-900 dark:text-gray-100">
                    {activeOrg.name}
                  </div>
                  <div className="text-xs text-gray-500 dark:text-gray-400 capitalize">
                    {activeOrg.role}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Environments section */}
          {activeOrg && environments.length > 0 && (
            <div className="border-b border-gray-200 dark:border-gray-700 py-2">
              <div className="px-4 py-1.5 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
                Environments
              </div>
              {environments.map((env) => (
                <button
                  key={env.id}
                  onClick={() => {
                    setActiveEnvironment(env);
                    setIsOpen(false);
                  }}
                  className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                    activeEnvironment?.id === env.id
                      ? "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                      : "text-gray-900 dark:text-gray-100"
                  }`}
                >
                  <svg
                    className="h-4 w-4 text-gray-400"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
                    />
                  </svg>
                  {env.name}
                  {activeEnvironment?.id === env.id && (
                    <svg
                      className="ml-auto h-4 w-4"
                      fill="currentColor"
                      viewBox="0 0 20 20"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                </button>
              ))}
            </div>
          )}

          {/* Switch organization section */}
          {organizations.length > 1 && (
            <div className="border-b border-gray-200 dark:border-gray-700 py-2">
              <div className="px-4 py-1.5 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
                Switch Organization
              </div>
              {organizations
                .filter((org) => org.id !== activeOrg?.id)
                .map((org) => (
                  <button
                    key={org.id}
                    onClick={() => {
                      setActiveOrg(org);
                      // Don't close immediately - show loading state
                    }}
                    disabled={isExchangingToken}
                    className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                  >
                    <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-gray-400 to-gray-600 text-white font-semibold text-sm">
                      {org.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="truncate">{org.name}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 capitalize">
                        {org.role}
                      </div>
                    </div>
                  </button>
                ))}
            </div>
          )}

          {/* Actions */}
          <div className="py-2">
            <button
              onClick={() => {
                setIsOpen(false);
                window.history.pushState({}, "", "/organizations/new");
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
              Create New Organization
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
