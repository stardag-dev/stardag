import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ModalExecutionDetails } from "./TaskDetail";

describe("ModalExecutionDetails", () => {
  const fullMetadata = {
    kind: "modal",
    app_name: "my-app",
    workspace: "my-workspace",
    environment: "staging",
    function_name: "worker_default",
    app_id: "ap-123",
    function_id: "fu-456",
  };

  it("lists every captured identifier verbatim once expanded", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionDetails metadata={fullMetadata} executorRef="fc-789" />);

    // Collapsed by default.
    expect(screen.queryByText("ap-123")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /more details/i }));

    for (const value of [
      "modal",
      "my-app",
      "my-workspace",
      "staging",
      "worker_default",
      "ap-123",
      "fu-456",
      "fc-789", // function-call ref
    ]) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }
  });

  it("renders only the fields that are present", async () => {
    const user = userEvent.setup();
    render(
      <ModalExecutionDetails
        metadata={{ kind: "modal", app_name: "my-app" }}
        executorRef={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /more details/i }));

    expect(screen.getByText("my-app")).toBeInTheDocument();
    // Absent metadata fields and the missing call ref must not render.
    expect(screen.queryByText("Workspace:")).not.toBeInTheDocument();
    expect(screen.queryByText("App ID:")).not.toBeInTheDocument();
    expect(screen.queryByText("Function call ID:")).not.toBeInTheDocument();
  });

  it("renders nothing when no identifiers are present", () => {
    const { container } = render(
      <ModalExecutionDetails metadata={null} executorRef={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the function call id even without any metadata", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionDetails metadata={null} executorRef="fc-789" />);
    await user.click(screen.getByRole("button", { name: /more details/i }));
    expect(screen.getByText("fc-789")).toBeInTheDocument();
  });

  it("copies an identifier to the clipboard", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionDetails metadata={fullMetadata} executorRef="fc-789" />);
    await user.click(screen.getByRole("button", { name: /more details/i }));

    // Copy the Function call ID (last copy button in the disclosure list).
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    await user.click(copyButtons[copyButtons.length - 1]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });
});
