import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Modal } from "./Modal";

/**
 * Two dialogs open at once is a real arrangement, not a mistake: the
 * scheduling dialog offers a remedy and the remedy asks to be confirmed.
 * Each `Modal` used to behave as if it were the only one, which broke
 * both of the things it manages document-wide.
 */
describe("Modal, stacked", () => {
  function Nested({
    onOuterClose,
    onInnerClose,
    innerOpen,
  }: {
    onOuterClose: () => void;
    onInnerClose: () => void;
    innerOpen: boolean;
  }) {
    return (
      <>
        <Modal isOpen onClose={onOuterClose} title="Outer">
          <p>outer body</p>
        </Modal>
        <Modal isOpen={innerOpen} onClose={onInnerClose} title="Inner">
          <p>inner body</p>
        </Modal>
      </>
    );
  }

  it("closes only the topmost dialog on Escape", async () => {
    const onOuterClose = vi.fn();
    const onInnerClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Nested onOuterClose={onOuterClose} onInnerClose={onInnerClose} innerOpen />,
    );

    await user.keyboard("{Escape}");

    expect(onInnerClose).toHaveBeenCalledTimes(1);
    // One keypress used to run every open dialog's close handler, so
    // dismissing a confirmation also shut the dialog it belonged to.
    expect(onOuterClose).not.toHaveBeenCalled();
  });

  it("keeps the page locked while an outer dialog is still open", () => {
    const { rerender } = render(
      <Nested onOuterClose={vi.fn()} onInnerClose={vi.fn()} innerOpen />,
    );
    expect(document.body.style.overflow).toBe("hidden");

    // The inner dialog closes; the outer one has not.
    rerender(
      <Nested onOuterClose={vi.fn()} onInnerClose={vi.fn()} innerOpen={false} />,
    );

    // Its unmount cleanup used to restore scrolling for the whole page,
    // leaving the still-open dialog sitting over a scrollable document.
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("unlocks the page once the last dialog closes", () => {
    const { rerender } = render(
      <Modal isOpen onClose={vi.fn()} title="Only">
        <p>body</p>
      </Modal>,
    );
    expect(document.body.style.overflow).toBe("hidden");

    rerender(
      <Modal isOpen={false} onClose={vi.fn()} title="Only">
        <p>body</p>
      </Modal>,
    );
    expect(document.body.style.overflow).toBe("");
  });

  it("still closes a lone dialog on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Modal isOpen onClose={onClose} title="Only">
        <p>body</p>
      </Modal>,
    );

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByText("body")).toBeInTheDocument();
  });
});
