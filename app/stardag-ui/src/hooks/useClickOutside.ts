import { useEffect, type RefObject } from "react";

/**
 * Close a popover when the pointer goes down anywhere outside it.
 *
 * `mousedown` rather than `click`, so the menu closes before the element
 * under the pointer gets its own click — otherwise closing the menu by
 * pressing a button behind it both closes and activates.
 *
 * Does nothing while `active` is false, which keeps a closed popover from
 * holding a document listener.
 */
export function useClickOutside(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  onOutside: () => void,
) {
  useEffect(() => {
    if (!active) return;
    const handle = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onOutside();
    };
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [ref, active, onOutside]);
}
