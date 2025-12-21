import { useState, useRef, useEffect } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import { useAuth } from "../context/AuthContext";

export function WorkspaceSelector() {
  const { isAuthenticated } = useAuth();
  const {
    organizations,
    activeOrg,
    setActiveOrg,
    workspaces,
    activeWorkspace,
    setActiveWorkspace,
    isLoading,
  } = useWorkspace();

  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

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

  // No organizations yet
  if (organizations.length === 0) {
    return (
      <span className="text-sm text-gray-500 dark:text-gray-400">No organizations</span>
    );
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <span className="font-medium">{activeOrg?.name ?? "Select org"}</span>
        {activeWorkspace && (
          <>
            <span className="text-gray-400 dark:text-gray-500">/</span>
            <span>{activeWorkspace.name}</span>
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
                  // Don't close, let user select workspace
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
          </div>

          {/* Workspaces section */}
          {activeOrg && workspaces.length > 0 && (
            <div className="p-2">
              <div className="px-2 py-1 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
                Workspace
              </div>
              {workspaces.map((ws) => (
                <button
                  key={ws.id}
                  onClick={() => {
                    setActiveWorkspace(ws);
                    setIsOpen(false);
                  }}
                  className={`flex w-full items-center rounded px-2 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                    activeWorkspace?.id === ws.id
                      ? "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                      : "text-gray-900 dark:text-gray-100"
                  }`}
                >
                  {ws.name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
