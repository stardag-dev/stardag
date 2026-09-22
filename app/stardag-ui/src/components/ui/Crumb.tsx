/**
 * The header trail: workspace / environment / page / record.
 *
 * All four are the same kind of thing — one step in a path — so they are
 * drawn the same way rather than by each owner inventing a treatment.
 * Before this, the workspace and environment were a two-line block with a
 * 36px avatar while the rest of the trail was single-line text, which is
 * what made the row look misaligned: the two halves were not the same
 * kind of object.
 *
 * `leading-none` on every crumb is what does the vertical alignment. A
 * dropdown trigger has padding and an icon, a terminal crumb is bare
 * text, and only a shared line-height keeps their baselines together
 * inside the flex row.
 */

/** The `/` between two crumbs. Decorative: the trail is not a list. */
export function CrumbSeparator() {
  return (
    <span aria-hidden="true" className="select-none text-gray-300 dark:text-gray-600">
      /
    </span>
  );
}

/**
 * A crumb that opens something — a dropdown, or a previous page.
 *
 * Muted until hovered, because a trail's earlier steps are context, not
 * the subject of the screen.
 */
export const CRUMB_TRIGGER =
  "flex min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-base leading-none " +
  "text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 " +
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 " +
  "disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 " +
  "dark:hover:text-gray-100";

/** The last crumb: where you are. Full contrast, never interactive. */
export const CRUMB_CURRENT =
  "truncate text-base leading-none font-medium text-gray-900 dark:text-gray-100";

/** The chevron a dropdown trigger carries, rotated while open. */
export function CrumbChevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      className={`h-3.5 w-3.5 flex-shrink-0 text-gray-400 transition-transform ${
        open ? "rotate-180" : ""
      }`}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
    </svg>
  );
}

/**
 * The panel a crumb's dropdown renders into.
 *
 * Deliberately **not** `role="menu"`. That role promises menu keyboard
 * semantics — arrow keys, Home/End, typeahead, roving focus — and none
 * of these panels implement them; a screen-reader user told "menu" would
 * be handed a set of keys that do nothing. What they actually are is a
 * disclosure over a group of ordinary buttons, which Tab already
 * reaches. The trigger's `aria-expanded` says so honestly, and that is
 * the whole contract.
 *
 * Giving them real menu semantics is a worthwhile change, and a separate
 * one — the same shape as the focus-trap gap noted in `Modal`.
 */
export const CRUMB_MENU =
  "absolute left-0 top-full z-50 mt-1.5 min-w-[16rem] overflow-hidden rounded-lg " +
  "border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800";
