import { useState, useRef, useEffect } from "react";
import { isAuthConfigured } from "../auth/config";
import { useAuth } from "../context/AuthContext";

export function UserMenu() {
  const { user, isLoading, isAuthenticated, login, logout } = useAuth();
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close menu when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // If auth is not configured, don't show anything
  if (!isAuthConfigured()) {
    return null;
  }

  if (isLoading) {
    return (
      <div className="h-8 w-8 rounded-full bg-gray-200 dark:bg-gray-700 animate-pulse" />
    );
  }

  if (!isAuthenticated) {
    return (
      <button
        onClick={() => login()}
        className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 dark:focus:ring-offset-gray-800"
      >
        Sign in
      </button>
    );
  }

  const displayName =
    user?.profile?.name ||
    user?.profile?.preferred_username ||
    user?.profile?.email ||
    "User";
  const email = user?.profile?.email;
  const initials = displayName
    .split(" ")
    .map((n) => n[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 dark:focus:ring-offset-gray-800"
        aria-expanded={isOpen}
        aria-haspopup="true"
      >
        <div className="h-8 w-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-sm font-medium">
          {initials}
        </div>
      </button>

      {isOpen && (
        <div className="absolute right-0 mt-2 w-56 rounded-md bg-white dark:bg-gray-800 shadow-lg ring-1 ring-black ring-opacity-5 dark:ring-gray-700 z-50">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
              {displayName}
            </p>
            {email && (
              <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                {email}
              </p>
            )}
          </div>
          <div className="py-1">
            <button
              onClick={() => {
                setIsOpen(false);
                logout();
              }}
              className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
