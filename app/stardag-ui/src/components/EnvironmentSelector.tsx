import { useState, useRef, useEffect } from "react";
import { useEnvironment } from "../context/EnvironmentContext";
import { useAuth } from "../context/AuthContext";
import { fetchPendingInvites, type PendingInvite } from "../api/organizations";

export function EnvironmentSelector() {
  const { isAuthenticated } = useAuth();
  const {
    organizations,
    activeOrg,
    setActiveOrg,
    environments,
    activeEnvironment,
    setActiveEnvironment,
    isLoading,
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
      <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-blue-500" />
        Loading...
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
          className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-200 underline"
        >
          {pendingInvites.length} pending invite{pendingInvites.length > 1 ? "s" : ""}
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
        className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-200 underline"
      >
        Create Organization
      </button>
    );
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <span className="font-medium">{activeOrg?.name ?? "Select org"}</span>
        {activeEnvironment && (
          <>
            <span className="text-gray-400 dark:text-gray-500">/</span>
            <span>{activeEnvironment.name}</span>
          </>
        )}
        <svg
          className={`h-4 w-4 transition-transform ${isOpen ? "rotate-180" : ""}`}
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
      </button>

      {isOpen && (
        <div className="absolute left-0 top-full z-50 mt-1 min-w-[280px] rounded-md border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 shadow-lg">
          {/* Organizations section */}
          <div className="border-b border-gray-200 dark:border-gray-700 p-2">
            <div className="px-2 py-1 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
              Organization
            </div>
            {organizations.map((org) => (
              <button
                key={org.id}
                onClick={() => {
                  setActiveOrg(org);
                  // Don't close, let user select environment
                }}
                className={`flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                  activeOrg?.id === org.id
                    ? "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                    : "text-gray-900 dark:text-gray-100"
                }`}
              >
                <span>{org.name}</span>
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {org.role}
                </span>
              </button>
            ))}
            <button
              onClick={() => {
                setIsOpen(false);
                window.history.pushState({}, "", "/organizations/new");
                window.dispatchEvent(new PopStateEvent("popstate"));
              }}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-blue-600 dark:text-blue-400 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              <svg
                className="h-4 w-4"
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
              Create Organization
            </button>
          </div>

          {/* Environments section */}
          {activeOrg && environments.length > 0 && (
            <div className="p-2">
              <div className="px-2 py-1 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
                Environment
              </div>
              {environments.map((env) => (
                <button
                  key={env.id}
                  onClick={() => {
                    setActiveEnvironment(env);
                    setIsOpen(false);
                  }}
                  className={`flex w-full items-center rounded px-2 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                    activeEnvironment?.id === env.id
                      ? "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                      : "text-gray-900 dark:text-gray-100"
                  }`}
                >
                  {env.name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
