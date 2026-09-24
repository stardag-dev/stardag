import { act, fireEvent, screen } from "@testing-library/react";

/**
 * Hover `element` and return the text of the tooltip it opens (see
 * `ui/Tooltip`), then leave it again so the next call starts clean.
 */
export function tooltipOf(element: Element): string {
  act(() => {
    fireEvent.pointerEnter(element);
  });
  const text = screen.getByRole("tooltip").textContent ?? "";
  act(() => {
    fireEvent.pointerLeave(element);
  });
  return text;
}
