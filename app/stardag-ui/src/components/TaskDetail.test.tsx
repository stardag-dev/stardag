import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../types/task";

let mockWorkspaceRole: "owner" | "admin" | "member" | null = "admin";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1", slug: "default", name: "default" },
    activeWorkspaceRole: mockWorkspaceRole,
  }),
}));

vi.mock("../api/tasks", () => ({
  cancelTask: vi.fn(),
  retryTask: vi.fn(),
  fetchTaskArtifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
  fetchTaskEvents: vi.fn().mockResolvedValue([]),
}));

import { cancelTask, retryTask } from "../api/tasks";
import { ModalExecutionCallRef, ModalExecutionDetails, TaskDetail } from "./TaskDetail";

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

// ---- Claim holder ----

const VIEWED_BUILD = "99999999-aaaa-bbbb-cccc-dddddddddddd";
const HOLDER_BUILD = "11111111-2222-3333-4444-555555555555";
const HOUR = 3600 * 1000;

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "pk-1",
    task_id: "tid-grind-beans",
    environment_id: "env-1",
    task_namespace: "",
    task_name: "GrindBeans",
    task_data: {},
    version: null,
    output_uri: null,
    created_at: new Date(Date.now() - 5 * HOUR).toISOString(),
    status: "running",
    started_at: new Date(Date.now() - 4 * HOUR).toISOString(),
    completed_at: null,
    error_message: null,
    artifact_count: 0,
    latest_status: "running",
    latest_status_at: new Date(Date.now() - 4 * HOUR).toISOString(),
    latest_status_build_id: HOLDER_BUILD,
    ...overrides,
  };
}

describe("TaskDetail claim holder", () => {
  beforeEach(() => {
    mockWorkspaceRole = "admin";
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("says in words that another build holds the claim, and for how long", async () => {
    const onStatusBuildClick = vi.fn();
    const user = userEvent.setup();
    render(
      <TaskDetail
        task={makeTask()}
        buildId={VIEWED_BUILD}
        onClose={() => {}}
        onStatusBuildClick={onStatusBuildClick}
      />,
    );

    expect(
      screen.getByText("Execution claim held by another build"),
    ).toBeInTheDocument();
    expect(screen.getByText(/for 4h 00m/)).toBeInTheDocument();
    expect(screen.getByText(/not the build you are viewing/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: HOLDER_BUILD.slice(0, 8) }));
    expect(onStatusBuildClick).toHaveBeenCalledWith(HOLDER_BUILD);
  });

  it("addresses the release to the holding build, not the one on screen", async () => {
    vi.mocked(cancelTask).mockResolvedValue(undefined);
    const onTaskCancelled = vi.fn();
    const user = userEvent.setup();
    render(
      <TaskDetail
        task={makeTask()}
        buildId={VIEWED_BUILD}
        onClose={() => {}}
        onTaskCancelled={onTaskCancelled}
      />,
    );

    // The plain Cancel button stands down: it would address the wrong build.
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Release claim" }));
    expect(
      await screen.findByText("Release this task's execution claim"),
    ).toBeInTheDocument();
    await user.click(
      screen.getAllByRole("button", { name: "Release claim" }).slice(-1)[0],
    );

    await waitFor(() =>
      expect(cancelTask).toHaveBeenCalledWith(HOLDER_BUILD, "tid-grind-beans", "env-1"),
    );
    expect(onTaskCancelled).toHaveBeenCalled();
  });

  it("offers a reset as well for a suspended task, and retries under the holder", async () => {
    vi.mocked(retryTask).mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TaskDetail
        task={makeTask({ status: "suspended", latest_status: "suspended" })}
        buildId={VIEWED_BUILD}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reset to pending" }));
    await user.click(
      screen.getAllByRole("button", { name: "Reset to pending" }).slice(-1)[0],
    );
    await waitFor(() =>
      expect(retryTask).toHaveBeenCalledWith(HOLDER_BUILD, "tid-grind-beans", "env-1"),
    );
  });

  it("hides the remedies from non-admin members but keeps the diagnosis", async () => {
    mockWorkspaceRole = "member";
    render(<TaskDetail task={makeTask()} buildId={VIEWED_BUILD} onClose={() => {}} />);

    expect(
      await screen.findByText("Execution claim held by another build"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Release claim" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Releasing a claim requires the workspace admin role."),
    ).toBeInTheDocument();
  });

  it("says nothing about claims for a task that holds none", async () => {
    render(
      <TaskDetail
        task={makeTask({ status: "completed", latest_status: "completed" })}
        buildId={VIEWED_BUILD}
        onClose={() => {}}
      />,
    );
    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    expect(screen.queryByText(/execution claim/i)).not.toBeInTheDocument();
  });

  it("reports the holder without cross-build wording when no build is in view", async () => {
    // The task explorer renders this panel with no build context.
    render(<TaskDetail task={makeTask()} onClose={() => {}} />);
    expect(await screen.findByText("Holding an execution claim")).toBeInTheDocument();
    expect(screen.queryByText(/not the build you are viewing/)).not.toBeInTheDocument();
  });
});
