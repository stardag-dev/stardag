import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ModalExecutionBreadcrumb, ModalExecutionDetails } from "./TaskDetail";

const fullMetadata = {
  kind: "modal",
  app_name: "my-app",
  workspace: "my-workspace",
  environment: "staging",
  function_name: "worker_default",
  app_id: "ap-123",
  function_id: "fu-456",
};

describe("ModalExecutionBreadcrumb", () => {
  it("renders four top-level segments in hierarchy order, each a Modal link", () => {
    render(<ModalExecutionBreadcrumb metadata={fullMetadata} executorRef="fc-789" />);

    // workspace + environment collapse into ONE link (to the environment
    // page); then app, function, call ref.
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      "my-workspace / staging",
      "my-app",
      "worker_default",
      "fc-789",
    ]);
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "https://modal.com/apps/my-workspace/staging",
      "https://modal.com/apps/my-workspace/staging/ap-123",
      "https://modal.com/apps/my-workspace/staging/ap-123?activeTab=functions&functionId=fu-456",
      "https://modal.com/apps/my-workspace/staging/ap-123?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-789",
    ]);
    // Every link opens in a new tab safely.
    for (const a of links) {
      expect(a).toHaveAttribute("target", "_blank");
      expect(a).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  it("renders just the environment (plain text) when workspace is missing", () => {
    render(
      <ModalExecutionBreadcrumb
        metadata={{ kind: "modal", environment: "staging", app_id: "ap-123" }}
        executorRef={null}
      />,
    );
    // Without a workspace neither the env URL nor the app URL can be built,
    // so the env segment is plain text (not a link) and shows only the
    // environment half (no phantom workspace).
    expect(screen.getByText("staging")).toBeInTheDocument();
    expect(screen.queryByText("my-workspace")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders just the workspace (plain text, no link) when environment is missing", () => {
    render(
      <ModalExecutionBreadcrumb
        metadata={{ kind: "modal", workspace: "my-workspace" }}
        executorRef={null}
      />,
    );
    // A workspace-only URL is misleading, so it is never emitted.
    expect(screen.getByText("my-workspace")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("omits absent segments (trail starts at the first level present)", () => {
    render(
      <ModalExecutionBreadcrumb
        metadata={{ kind: "modal", workspace: "my-workspace", app_name: "my-app" }}
        executorRef={null}
      />,
    );
    // workspace present but no environment → workspace is plain text (no
    // link); only the app segment links.
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["my-app"]);
    expect(screen.getByText("my-workspace")).toBeInTheDocument();
    // Absent environment / function / call-ref must not appear.
    expect(screen.queryByText("staging")).not.toBeInTheDocument();
    expect(screen.queryByText("worker_default")).not.toBeInTheDocument();
  });

  it("renders a segment as plain text when its URL can't be built", () => {
    // function_name present but no function_id/app → modalFunctionUrl is null.
    render(
      <ModalExecutionBreadcrumb
        metadata={{ kind: "modal", function_name: "worker_default" }}
        executorRef={null}
      />,
    );
    expect(screen.getByText("worker_default")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("gives the last (call-ref) segment a copy button that copies the fc id", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionBreadcrumb metadata={fullMetadata} executorRef="fc-789" />);

    // Only the call-ref segment carries a copy control.
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    expect(copyButtons).toHaveLength(1);
    await user.click(copyButtons[0]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });

  it("links the call ref only to a genuine call-level URL", () => {
    // function_id present → the call ref is a real deep link to the call.
    render(<ModalExecutionBreadcrumb metadata={fullMetadata} executorRef="fc-789" />);
    expect(screen.getByRole("link", { name: "fc-789" })).toHaveAttribute(
      "href",
      "https://modal.com/apps/my-workspace/staging/ap-123" +
        "?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-789",
    );
  });

  it("renders the call ref as plain text (never linking to the app page) when function_id is missing", async () => {
    const user = userEvent.setup();
    // app_name/app_id resolvable but NO function_id: modalFunctionCallUrl
    // would fall back to the app page, which must not become a clickable call
    // ref. It renders as plain text but keeps its copy button.
    render(
      <ModalExecutionBreadcrumb
        metadata={{
          kind: "modal",
          workspace: "my-workspace",
          environment: "staging",
          app_name: "my-app",
          app_id: "ap-123",
        }}
        executorRef="fc-789"
      />,
    );
    // The call ref is not a link (only the ws/env and app segments are).
    expect(screen.queryByRole("link", { name: "fc-789" })).not.toBeInTheDocument();
    expect(screen.getByText("fc-789")).toBeInTheDocument();
    // Copy button still present and copies the raw fc id.
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    expect(copyButtons).toHaveLength(1);
    await user.click(copyButtons[0]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });

  it("renders nothing for an explicitly non-modal kind", () => {
    const { container } = render(
      <ModalExecutionBreadcrumb
        metadata={{ kind: "k8s", workspace: "my-workspace", app_name: "my-app" }}
        executorRef="ref-1"
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there is nothing to show", () => {
    const { container } = render(
      <ModalExecutionBreadcrumb metadata={null} executorRef={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ModalExecutionDetails", () => {
  it("lists every captured identifier verbatim once expanded", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionDetails metadata={fullMetadata} executorRef="fc-789" />);

    // Collapsed by default.
    expect(screen.queryByText("ap-123")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /more details/i }));

    // Every captured value appears; the redundant `kind` is not shown.
    for (const value of [
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
    expect(screen.queryByText("modal")).not.toBeInTheDocument();
    expect(screen.queryByText("Kind")).not.toBeInTheDocument();
  });

  it("groups names then ids with a divider only when both groups are present", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <ModalExecutionDetails metadata={fullMetadata} executorRef="fc-789" />,
    );
    await user.click(screen.getByRole("button", { name: /more details/i }));

    // Row labels reflect the name/id split.
    for (const label of [
      "Workspace",
      "Environment",
      "App",
      "Function",
      "App ID",
      "Function ID",
      "Call ref",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // Divider between the two groups.
    expect(container.querySelector("hr")).toBeInTheDocument();
  });

  it("renders only the fields that are present, without a divider for one group", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <ModalExecutionDetails
        metadata={{ kind: "modal", app_name: "my-app" }}
        executorRef={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /more details/i }));

    expect(screen.getByText("my-app")).toBeInTheDocument();
    // The one present field renders with its row label.
    expect(screen.getByText("App")).toBeInTheDocument();
    // Absent metadata fields and the missing call ref must not render.
    expect(screen.queryByText("Workspace")).not.toBeInTheDocument();
    expect(screen.queryByText("App ID")).not.toBeInTheDocument();
    expect(screen.queryByText("Call ref")).not.toBeInTheDocument();
    // Only the names group is present → no divider.
    expect(container.querySelector("hr")).not.toBeInTheDocument();
  });

  it("renders nothing when no identifiers are present", () => {
    const { container } = render(
      <ModalExecutionDetails metadata={null} executorRef={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for an explicitly non-modal kind", () => {
    // The block's labels ("App name", "Function ID", …) are Modal-specific,
    // so it must not surface identifiers from a non-modal executor as Modal
    // fields.
    const { container } = render(
      <ModalExecutionDetails
        metadata={{ kind: "k8s", app_name: "some-pod", function_id: "fu-x" }}
        executorRef="ref-1"
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders for kind-less metadata (legacy, treated as modal)", async () => {
    const user = userEvent.setup();
    render(
      <ModalExecutionDetails metadata={{ app_name: "my-app" }} executorRef={null} />,
    );
    await user.click(screen.getByRole("button", { name: /more details/i }));
    expect(screen.getByText("my-app")).toBeInTheDocument();
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
