import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BuildExecutorChips, ExecutorBadge } from "./ExecutorBadge";

describe("ExecutorBadge", () => {
  it("renders the Modal chip for the modal executor", () => {
    render(<ExecutorBadge executor="modal" />);
    expect(screen.getByText("⚡ Modal")).toBeInTheDocument();
  });

  it("renders the raw executor name for unknown executor kinds", () => {
    render(<ExecutorBadge executor="k8s" />);
    expect(screen.getByText("k8s")).toBeInTheDocument();
  });

  it("renders nothing without an executor", () => {
    const { container } = render(<ExecutorBadge executor={null} />);
    expect(container).toBeEmptyDOMElement();
    const { container: undefinedContainer } = render(<ExecutorBadge />);
    expect(undefinedContainer).toBeEmptyDOMElement();
  });

  it("shows the call ref in the tooltip and copies it on click", async () => {
    // userEvent.setup() installs a working clipboard stub in jsdom.
    const user = userEvent.setup();
    render(<ExecutorBadge executor="modal" executorRef="fc-abc123" />);

    const badge = screen.getByRole("button");
    expect(badge).toHaveAttribute("title", "Call ref: fc-abc123 (click to copy)");
    await user.click(badge);
    expect(await window.navigator.clipboard.readText()).toBe("fc-abc123");
    expect(badge).toHaveAttribute("title", "Copied!");
  });

  it("is not clickable without a call ref", () => {
    render(<ExecutorBadge executor="modal" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("BuildExecutorChips", () => {
  const metadata = {
    kind: "modal",
    app_name: "my-app",
    workspace: "my-workspace",
    environment: "staging",
    reactive: true,
  };

  it("renders a Modal app chip linking to the app page and a reactive badge", () => {
    render(<BuildExecutorChips metadata={metadata} />);
    const link = screen.getByRole("link", { name: "Modal: my-app" });
    expect(link).toHaveAttribute(
      "href",
      "https://modal.com/apps/my-workspace/staging/deployed/my-app",
    );
    expect(screen.getByText("reactive")).toBeInTheDocument();
  });

  it("renders a plain chip (no dead link) when the app URL cannot be built", () => {
    render(<BuildExecutorChips metadata={{ kind: "modal", app_name: "my-app" }} />);
    expect(screen.getByText("Modal: my-app")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("omits the reactive badge when reactive is false", () => {
    render(<BuildExecutorChips metadata={{ ...metadata, reactive: false }} />);
    expect(screen.queryByText("reactive")).not.toBeInTheDocument();
  });

  it("renders nothing without metadata", () => {
    const { container } = render(<BuildExecutorChips metadata={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
