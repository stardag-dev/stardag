import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

// Distance from the anchor, and the margin kept to the window's edges.
const GAP = 6;
const EDGE = 8;

interface TooltipProps {
  /** What the tooltip says. Nothing is drawn when empty. */
  content: ReactNode;
  /** The element hovered or focused. */
  children: ReactNode;
  /** Classes for the wrapping span; `inline-flex` by default. */
  className?: string;
}

/**
 * The one hover explanation in the app: dark box, small text, shown at
 * once on hover or keyboard focus.
 *
 * Not the native `title` attribute, which waits about a second, is styled
 * by the browser, and differs per platform — so the toolbar's tooltips and
 * a table header's said the same kind of thing in two different ways.
 *
 * Portalled to `document.body` and placed with fixed coordinates, then
 * clamped into the window: a tooltip next to the right edge shifts left
 * rather than being cropped, one near the bottom flips above its anchor,
 * and one inside a scrolling table is not clipped by it. It closes on
 * scroll rather than following, since the coordinates are the anchor's at
 * open time.
 *
 * The open tooltip is the wrapper's `aria-describedby` target. Callers
 * must not also set `title`, or the browser's tooltip appears underneath.
 */
export function Tooltip({
  content,
  children,
  className = "inline-flex",
}: TooltipProps) {
  const id = useId();
  const anchorRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  const show = useCallback(() => setOpen(true), []);
  const hide = useCallback(() => setOpen(false), []);

  // Measure after render and before paint, so the first frame is placed.
  // Written to the element directly: a position is a measurement of the
  // DOM, not state, and a state round-trip would render twice per hover.
  useLayoutEffect(() => {
    if (!open) return;
    const el = tipRef.current;
    const anchor = anchorRef.current?.getBoundingClientRect();
    const tip = el?.getBoundingClientRect();
    if (!el || !anchor || !tip) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const centred = anchor.left + anchor.width / 2 - tip.width / 2;
    const left = Math.max(EDGE, Math.min(centred, vw - tip.width - EDGE));
    const below = anchor.bottom + GAP;
    const top =
      below + tip.height > vh - EDGE && anchor.top - GAP - tip.height >= EDGE
        ? anchor.top - GAP - tip.height
        : below;
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.visibility = "visible";
  }, [open, content]);

  useEffect(() => {
    if (!open) return;
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [open, hide]);

  const empty = content === null || content === undefined || content === "";
  return (
    <span
      ref={anchorRef}
      className={className}
      onPointerEnter={show}
      onPointerLeave={hide}
      onFocus={show}
      onBlur={hide}
      aria-describedby={open && !empty ? id : undefined}
    >
      {children}
      {open &&
        !empty &&
        createPortal(
          <div
            ref={tipRef}
            id={id}
            role="tooltip"
            style={{ position: "fixed", left: 0, top: 0, visibility: "hidden" }}
            className="pointer-events-none z-[100] w-max max-w-64 rounded-md bg-gray-900 px-2 py-1 text-left text-xs font-normal tracking-normal whitespace-normal text-gray-100 normal-case shadow-lg dark:bg-gray-700"
          >
            {content}
          </div>,
          document.body,
        )}
    </span>
  );
}
