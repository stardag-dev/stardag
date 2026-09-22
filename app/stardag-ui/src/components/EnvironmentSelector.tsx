import { useCallback, useRef, useState } from "react";
import { useEnvironment } from "../context/EnvironmentContext";
import { useClickOutside } from "../hooks/useClickOutside";
import { CRUMB_MENU, CRUMB_TRIGGER, CrumbChevron, CrumbSeparator } from "./ui/Crumb";

/**
 * The environment crumb — its own control, next to the workspace.
 *
 * It used to be a second line under the workspace name, switchable only
 * from inside the workspace dropdown. Two things were wrong with that.
 * The environment is what nearly every screen is scoped by, so reaching
 * it through the control for the thing that changes least was backwards;
 * and a stacked two-line block cannot sit on the same baseline as the
 * rest of the trail, which is what made the header look misaligned.
 *
 * Absent until there is a choice to describe: no workspace, or a
 * workspace whose environments have not loaded, renders nothing rather
 * than an empty control.
 */
export function EnvironmentSelector() {
  const { activeWorkspace, environments, activeEnvironment, setActiveEnvironment } =
    useEnvironment();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useClickOutside(ref, open, close);

  if (!activeWorkspace || environments.length === 0) return null;

  // The separator belongs to this crumb rather than to the header, so
  // that a workspace with no environments leaves no dangling "/".
  return (
    <>
      <CrumbSeparator />
      <div className="relative" ref={ref}>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={CRUMB_TRIGGER}
          title={
            activeEnvironment
              ? `Environment ${activeEnvironment.name} — switch environment`
              : "Select an environment"
          }
        >
          <span className="max-w-[12rem] truncate">
            {activeEnvironment?.name ?? "Select environment"}
          </span>
          <CrumbChevron open={open} />
        </button>

        {open && (
          <div className={CRUMB_MENU}>
            <p className="border-b border-gray-200 px-3 py-2 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:border-gray-700 dark:text-gray-400">
              Environments
            </p>
            <div className="max-h-80 overflow-y-auto py-1">
              {environments.map((environment) => {
                const active = activeEnvironment?.id === environment.id;
                return (
                  <button
                    key={environment.id}
                    type="button"
                    onClick={() => {
                      setActiveEnvironment(environment);
                      setOpen(false);
                    }}
                    className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset ${
                      active
                        ? "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
                        : "text-gray-900 hover:bg-gray-100 dark:text-gray-100 dark:hover:bg-gray-700"
                    }`}
                  >
                    <span className="min-w-0 flex-1 truncate">{environment.name}</span>
                    {active && (
                      <svg
                        aria-hidden="true"
                        className="h-4 w-4 flex-shrink-0"
                        fill="currentColor"
                        viewBox="0 0 20 20"
                      >
                        <path
                          fillRule="evenodd"
                          clipRule="evenodd"
                          d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                        />
                      </svg>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </>
  );
}
