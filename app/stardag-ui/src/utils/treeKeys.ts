/**
 * The keyboard model of a flat ARIA tree (the JSON tree, the diff view):
 * arrow keys move and fold, Home/End jump, Enter or Space toggles (or
 * draws a "show more" row's page), `c` copies the focused value and `p`
 * its path.
 */

import { isContainer, isMoreRow, moreOf, type TreeNode } from "./jsonTree";

export interface TreeKeyContext {
  /** The visible rows, in display order (see `visibleRows`). */
  rows: number[];
  nodes: readonly TreeNode[];
  expanded: ReadonlySet<string>;
  focusRow: (row: number) => void;
  toggle: (index: number) => void;
  more: (row: number, focus: boolean) => void;
  copy: (index: number, what: "value" | "path") => void;
}

/** Handle a keydown on the tree element; a no-op unless it came from a row. */
export function treeKeyDown(
  event: React.KeyboardEvent<HTMLElement>,
  { rows, nodes, expanded, focusRow, toggle, more, copy }: TreeKeyContext,
): void {
  const target = event.target as HTMLElement;
  if (target.getAttribute("role") !== "treeitem") return;
  // Leave the browser's own shortcuts (Ctrl+C on a selection, say) alone.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const index = Number(target.dataset.row);
  const row = rows.indexOf(index);
  if (row === -1) return;
  let handled = true;
  if (isMoreRow(index)) {
    const { parent } = moreOf(index);
    switch (event.key) {
      case "ArrowDown":
        if (row + 1 < rows.length) focusRow(rows[row + 1]);
        break;
      case "ArrowUp":
        if (row > 0) focusRow(rows[row - 1]);
        break;
      case "Home":
        focusRow(rows[0]);
        break;
      case "End":
        focusRow(rows[rows.length - 1]);
        break;
      case "ArrowLeft":
        if (parent > 0) focusRow(parent);
        break;
      case "Enter":
      case " ":
        more(index, true);
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
    return;
  }
  const node = nodes[index];
  const open = isContainer(node) && expanded.has(node.id);
  switch (event.key) {
    case "ArrowDown":
      if (row + 1 < rows.length) focusRow(rows[row + 1]);
      break;
    case "ArrowUp":
      if (row > 0) focusRow(rows[row - 1]);
      break;
    case "Home":
      focusRow(rows[0]);
      break;
    case "End":
      focusRow(rows[rows.length - 1]);
      break;
    case "ArrowRight":
      // Into the first drawn child: the next row, which may be a
      // "show earlier" row when the page has moved past the first.
      if (isContainer(node) && !open) toggle(index);
      else if (open && node.size > 0 && row + 1 < rows.length) focusRow(rows[row + 1]);
      break;
    case "ArrowLeft":
      if (open) toggle(index);
      else if (node.parent > 0) focusRow(node.parent);
      break;
    case "Enter":
    case " ":
      if (isContainer(node)) toggle(index);
      break;
    case "c":
      copy(index, "value");
      break;
    case "p":
      copy(index, "path");
      break;
    default:
      handled = false;
  }
  if (handled) event.preventDefault();
}
