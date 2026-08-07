import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Checkbox } from "./Checkbox";

describe("Checkbox", () => {
  it("exposes the label as the accessible name when visually hidden", () => {
    render(<Checkbox checked={false} onChange={vi.fn()} label="Select build Alpha" />);
    expect(
      screen.getByRole("checkbox", { name: "Select build Alpha" }),
    ).toBeInTheDocument();
    // Hidden label: not rendered as visible text.
    expect(screen.queryByText("Select build Alpha")).not.toBeInTheDocument();
  });

  it("renders a visible label when asked", () => {
    render(
      <Checkbox
        checked
        onChange={vi.fn()}
        label="Release execution claims"
        labelHidden={false}
      />,
    );
    const box = screen.getByRole("checkbox", { name: "Release execution claims" });
    expect(box).toBeChecked();
    expect(screen.getByText("Release execution claims")).toBeInTheDocument();
  });

  it("sets the indeterminate DOM property only while unchecked", () => {
    const { rerender } = render(
      <Checkbox checked={false} indeterminate onChange={vi.fn()} label="Select all" />,
    );
    const box = screen.getByRole("checkbox", {
      name: "Select all",
    }) as HTMLInputElement;
    expect(box.indeterminate).toBe(true);

    rerender(<Checkbox checked indeterminate onChange={vi.fn()} label="Select all" />);
    expect(box.indeterminate).toBe(false);
  });

  it("reports the new checked state on toggle", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Checkbox checked={false} onChange={onChange} label="Select all" />);

    await user.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
