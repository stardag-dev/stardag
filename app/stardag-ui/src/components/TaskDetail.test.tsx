import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ModalExecutionCallRef, ModalExecutionDetails } from "./TaskDetail";

const fullMetadata = {
  kind: "modal",
  app_name: "my-app",
  workspace: "my-workspace",
  environment: "staging",
  function_name: "worker_default",
  app_id: "ap-123",
  function_id: "fu-456",
};

// Deep-link URLs for fullMetadata (kept in one place for readability).
const ENV_URL = "https://modal.com/apps/my-workspace/staging";
const APP_URL = "https://modal.com/apps/my-workspace/staging/ap-123";
const FUNC_URL = `${APP_URL}?activeTab=functions&functionId=fu-456`;
const CALL_URL = `${FUNC_URL}&functionSection=calls&fcId=fc-789`;

describe("ModalExecutionCallRef", () => {
  it("links the call ref to a genuine call-level URL (new tab)", () => {
    render(<ModalExecutionCallRef metadata={fullMetadata} executorRef="fc-789" />);
    const link = screen.getByRole("link", { name: "fc-789" });
    expect(link).toHaveAttribute("href", CALL_URL);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("copies the raw fc id via its copy button", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionCallRef metadata={fullMetadata} executorRef="fc-789" />);
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    expect(copyButtons).toHaveLength(1);
    await user.click(copyButtons[0]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });

  it("renders the call ref as plain text (never the app page) when function_id is missing", async () => {
    const user = userEvent.setup();
    // app_name/app_id resolvable but NO function_id: modalFunctionCallUrl
    // would fall back to the app page, which must not become a clickable call
    // ref. It renders as plain text but keeps its copy button.
    render(
      <ModalExecutionCallRef
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
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("fc-789")).toBeInTheDocument();
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    expect(copyButtons).toHaveLength(1);
    await user.click(copyButtons[0]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });

  it("renders for kind-less metadata (legacy, treated as modal)", () => {
    render(
      <ModalExecutionCallRef metadata={{ app_name: "my-app" }} executorRef="fc-1" />,
    );
    expect(screen.getByText("fc-1")).toBeInTheDocument();
  });

  it("renders nothing for an explicitly non-modal kind", () => {
    const { container } = render(
      <ModalExecutionCallRef
        metadata={{ kind: "k8s", app_name: "my-app" }}
        executorRef="ref-1"
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there is no call ref", () => {
    const { container } = render(
      <ModalExecutionCallRef metadata={fullMetadata} executorRef={null} />,
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

  it("links each value to its Modal level; workspace stays plain text", async () => {
    const user = userEvent.setup();
    render(<ModalExecutionDetails metadata={fullMetadata} executorRef="fc-789" />);
    await user.click(screen.getByRole("button", { name: /more details/i }));

    // Workspace has no meaningful standalone URL → plain text, not a link.
    expect(screen.getByText("my-workspace").closest("a")).toBeNull();

    // Every other value is a best-effort deep link (new tab) to its level.
    const cases: [string, string][] = [
      ["staging", ENV_URL],
      ["my-app", APP_URL],
      ["worker_default", FUNC_URL],
      ["ap-123", APP_URL],
      ["fu-456", FUNC_URL],
      ["fc-789", CALL_URL],
    ];
    for (const [value, href] of cases) {
      const link = screen.getByRole("link", { name: value });
      expect(link).toHaveAttribute("href", href);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  it("falls back to plain text (still with copy) when a value's URL can't be built", async () => {
    const user = userEvent.setup();
    // Only app_name, no workspace → modalAppUrl is null, so App is plain text.
    render(
      <ModalExecutionDetails
        metadata={{ kind: "modal", app_name: "my-app" }}
        executorRef={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /more details/i }));

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("my-app").closest("a")).toBeNull();
    // Copy button is present regardless of whether the value links.
    expect(
      screen.getByRole("button", { name: /copy to clipboard/i }),
    ).toBeInTheDocument();
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
    // The block's labels ("App", "Function ID", …) are Modal-specific, so it
    // must not surface identifiers from a non-modal executor as Modal fields.
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

    // Copy the Call ref (last copy button in the disclosure list).
    const copyButtons = screen.getAllByRole("button", {
      name: /copy to clipboard/i,
    });
    await user.click(copyButtons[copyButtons.length - 1]);
    expect(await window.navigator.clipboard.readText()).toBe("fc-789");
  });
});
