import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  BuildFrontier,
  BuildStatus,
  BuildTickSummary,
  FrontierExternalBlocker,
} from "../types/task";
import { schedulingPanelForm } from "../utils/claims";
import { BuildSchedulingPanel } from "./BuildSchedulingPanel";

let mockWorkspaceRole: "owner" | "admin" | "member" | null = "admin";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1", slug: "default", name: "default" },
    activeWorkspaceRole: mockWorkspaceRole,
  }),
}));

vi.mock("../api/tasks", () => ({
  fetchBuildFrontier: vi.fn(),
  fetchBuildTickSummaries: vi.fn(),
  cancelTask: vi.fn(),
  retryTask: vi.fn(),
}));

import {
  cancelTask,
  fetchBuildFrontier,
  fetchBuildTickSummaries,
  retryTask,
} from "../api/tasks";

// Obviously fictional ids: the build on screen, and the build that owns
// the blocker.
const VIEWED_BUILD = "99999999-aaaa-bbbb-cccc-dddddddddddd";
const OWNER_BUILD = "11111111-2222-3333-4444-555555555555";

const HOUR = 3600 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function makeBlocker(
  overrides: Partial<FrontierExternalBlocker> = {},
): FrontierExternalBlocker {
  return {
    task_id: "tid-espresso-downstream",
    blocking_task_id: "tid-grind-beans",
    blocking_task_namespace: "",
    blocking_task_name: "GrindBeans",
    blocking_status: "running",
    blocking_status_at: ago(3 * HOUR),
    blocking_status_build_id: OWNER_BUILD,
    blocking_in_build: false,
    ...overrides,
  };
}

function makeFrontier(overrides: Partial<BuildFrontier> = {}): BuildFrontier {
  return {
    build_id: VIEWED_BUILD,
    build_status: "running",
    needs_tick: false,
    root_task_ids: ["tid-espresso-downstream"],
    roots: [],
    status_counts: { completed: 2, suspended: 1 },
    actionable: [],
    running: [],
    blocked_by_external: [],
    blocked_by_external_truncated: false,
    reactive_app_name: "kettle-scheduler",
    ...overrides,
  };
}

function makeSummary(overrides: Partial<BuildTickSummary> = {}): BuildTickSummary {
  return {
    id: "tick-1",
    build_id: VIEWED_BUILD,
    outcome: "lingered_out",
    summary: { spawned: 0, claim_denied: 3, iterations: 4 },
    created_at: ago(60 * 1000),
    ...overrides,
  };
}

function renderPanel(
  props: { buildStatus?: BuildStatus; onNavigate?: () => void } = {},
) {
  return render(
    <BuildSchedulingPanel
      buildId={VIEWED_BUILD}
      environmentId="env-1"
      buildStatus={props.buildStatus ?? "running"}
      onNavigateToBuild={props.onNavigate}
    />,
  );
}

describe("schedulingPanelForm", () => {
  it("flags a running build with nothing actionable and nothing running", () => {
    expect(schedulingPanelForm(makeFrontier(), "running")).toBe("stalled");
  });

  it("stays quiet while a build is progressing", () => {
    const progressing = makeFrontier({
      actionable: [{ task_id: "tid-a", latest_status: "pending" }],
    });
    // Reactive builds keep a one-line strip; non-reactive ones render nothing.
    expect(schedulingPanelForm(progressing, "running")).toBe("collapsed");
    expect(
      schedulingPanelForm({ ...progressing, reactive_app_name: null }, "running"),
    ).toBe("hidden");
  });

  it("does not flag a build with no tasks at all", () => {
    expect(schedulingPanelForm(makeFrontier({ status_counts: {} }), "pending")).toBe(
      "collapsed",
    );
  });

  it("never flags a completed or cancelled build", () => {
    expect(schedulingPanelForm(makeFrontier(), "completed")).toBe("collapsed");
    expect(schedulingPanelForm(makeFrontier(), "cancelled")).toBe("collapsed");
  });

  it("flags a failed build only when the failure has the stuck-build shape", () => {
    // Build failed, yet nothing in it failed: the spurious-failure signature.
    expect(
      schedulingPanelForm(
        makeFrontier({ status_counts: { completed: 2, suspended: 1 } }),
        "failed",
      ),
    ).toBe("stalled");
    // An ordinary DAG failure explains itself in the task table.
    expect(
      schedulingPanelForm(
        makeFrontier({ status_counts: { completed: 2, failed: 1 } }),
        "failed",
      ),
    ).toBe("collapsed");
    // ...unless a blocker was reported anyway.
    expect(
      schedulingPanelForm(
        makeFrontier({
          status_counts: { completed: 2, failed: 1 },
          blocked_by_external: [makeBlocker()],
        }),
        "failed",
      ),
    ).toBe("stalled");
  });
});

describe("BuildSchedulingPanel", () => {
  beforeEach(() => {
    mockWorkspaceRole = "admin";
    vi.mocked(fetchBuildFrontier).mockResolvedValue(makeFrontier());
    vi.mocked(fetchBuildTickSummaries).mockResolvedValue({
      build_id: VIEWED_BUILD,
      summaries: [makeSummary()],
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing for a healthy non-reactive build", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        reactive_app_name: null,
        actionable: [{ task_id: "tid-a", latest_status: "pending" }],
      }),
    );
    const { container } = renderPanel();

    await waitFor(() => expect(fetchBuildFrontier).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    // No point paying for tick history nobody will read.
    expect(fetchBuildTickSummaries).not.toHaveBeenCalled();
  });

  it("never claims 'no blockers' while the build is progressing", async () => {
    // An empty `blocked_by_external` means "not blocked, OR not stalled" —
    // the server only looks while a build is stalled. A progressing build
    // must say the second thing, not the first.
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        actionable: [{ task_id: "tid-a", latest_status: "pending" }],
        running: [{ task_id: "tid-b", latest_status: "running" }],
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText("1 actionable · 1 running")).toBeInTheDocument();
    expect(
      screen.queryByText(/Why is this build not progressing/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/no upstream/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Scheduling" }));
    expect(
      await screen.findByText(/only looked for while a build is stalled/i),
    ).toBeInTheDocument();
    // Tick history is fetched on demand, not on every poll of a healthy build.
    await waitFor(() => expect(fetchBuildTickSummaries).toHaveBeenCalledTimes(1));
  });

  it("names the blocker, how long it has been held, and links to the owning build", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({ blocked_by_external: [makeBlocker()] }),
    );
    const onNavigate = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onNavigate });

    expect(await screen.findByText("Not progressing")).toBeInTheDocument();
    expect(screen.getByText("GrindBeans")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("3h 00m")).toBeInTheDocument();
    // The blocked task is identified by a short id with the whole thing in
    // a title — the full id is 60 characters of noise on a summary line.
    expect(
      screen.getByTitle("This build's task tid-espresso-downstream"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: OWNER_BUILD.slice(0, 8) }));
    expect(onNavigate).toHaveBeenCalledWith(OWNER_BUILD);
  });

  it("stays compact until asked to explain itself", async () => {
    // The panel sits above the DAG and the task table. Everything that is
    // explanation rather than answer has to be behind a disclosure, or the
    // build view is unusable on the builds this panel exists to diagnose.
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({ blocked_by_external: [makeBlocker()] }),
    );
    const user = userEvent.setup();
    renderPanel();

    // Visible: the headline and the blocker itself, with its remedy.
    expect(await screen.findByText("Not progressing")).toBeInTheDocument();
    expect(
      screen.getByText(/1 task blocked by 1 upstream held outside this build/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Release claim on GrindBeans" }),
    ).toBeInTheDocument();

    // Hidden: the prose, the status breakdown, and the tick trail — which
    // is not even fetched until someone asks for it.
    expect(screen.queryByText(/Nothing in this build is actionable/)).toBeNull();
    expect(screen.queryByText("completed")).toBeNull();
    expect(screen.queryByText("Recent scheduler ticks")).toBeNull();
    expect(fetchBuildTickSummaries).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /3 tasks · details/ }));

    expect(
      await screen.findByText(/Nothing in this build is actionable/),
    ).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Recent scheduler ticks")).toBeInTheDocument();
    await waitFor(() => expect(fetchBuildTickSummaries).toHaveBeenCalledTimes(1));
  });

  it("suppresses task statuses that nothing is in", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({ status_counts: { completed: 2, failed: 0, skipped: 0 } }),
    );
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole("button", { name: /2 tasks · details/ }));
    expect(await screen.findByText("completed")).toBeInTheDocument();
    // "failed 0" is not information about this build.
    expect(screen.queryByText("failed")).toBeNull();
    expect(screen.queryByText("skipped")).toBeNull();
  });

  // The copy explains what happens next from the blocker's *status*, not from
  // which build owns it or whether it is in this build's plan — the chip
  // reports that separately, and it stays reachable (closure runs once, so a
  // dynamic edge written later is outside the plan).
  it.each([
    ["running", /holds the execution claim/i],
    ["cancelled", /resets it and runs it/i],
    ["suspended", /yielded dynamic dependencies/i],
    ["failed", /result, not a revocation/i],
    ["skipped", /result, not a revocation/i],
  ] as const)("explains a %s blocker by what happens next", async (status, copy) => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        blocked_by_external: [
          makeBlocker({ blocking_status: status, blocking_in_build: true }),
        ],
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    expect(screen.queryByText("outside this build")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Explain why GrindBeans is blocking" }),
    );
    expect(await screen.findByText(copy)).toBeInTheDocument();
    // Deleted with the out-of-plan handling: a build cannot be gated by an
    // upstream it never registered any more.
    expect(screen.queryByText(/never registered the blocking task/i)).toBeNull();
  });

  it("still flags a blocker outside this build's plan", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        blocked_by_external: [makeBlocker({ blocking_in_build: false })],
      }),
    );
    renderPanel();
    // Only reachable for a build registered before plan closure; reported
    // because it changes what a reader should expect, not what a tick does.
    expect(await screen.findByText("outside this build")).toBeInTheDocument();
  });

  it("says so when the blocker list was truncated", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        blocked_by_external: [makeBlocker()],
        blocked_by_external_truncated: true,
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /3 tasks · details/ }));
    expect(
      await screen.findByText(/More blockers were found than are listed here/),
    ).toBeInTheDocument();
  });

  it("renders tick counters it has never heard of rather than dropping them", async () => {
    vi.mocked(fetchBuildTickSummaries).mockResolvedValue({
      build_id: VIEWED_BUILD,
      summaries: [
        makeSummary({
          summary: {
            claim_denied: 3,
            // A counter added by a newer SDK than this UI.
            external_blockers_seen: 7,
          },
        }),
      ],
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /3 tasks · details/ }));

    expect(await screen.findByText("lingered out")).toBeInTheDocument();
    // Known key: rendered with its friendly label.
    expect(screen.getByText("claim denied")).toBeInTheDocument();
    // Unknown key: rendered anyway, underscores relaxed.
    expect(screen.getByText("external blockers seen")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("collapses a run of identical ticks, and drops the counters that stayed at zero", async () => {
    // What a stalled build actually produces: the same tick, over and over.
    vi.mocked(fetchBuildTickSummaries).mockResolvedValue({
      build_id: VIEWED_BUILD,
      summaries: [
        makeSummary({ id: "t1", summary: { spawned: 0, claim_denied: 3 } }),
        makeSummary({ id: "t2", summary: { spawned: 0, claim_denied: 3 } }),
        makeSummary({ id: "t3", summary: { spawned: 0, claim_denied: 3 } }),
        // A different outcome breaks the run rather than merging into it.
        makeSummary({ id: "t4", outcome: "lease_held", summary: { spawned: 0 } }),
      ],
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /3 tasks · details/ }));

    expect(await screen.findByText("×3")).toBeInTheDocument();
    expect(screen.getByText("claim denied")).toBeInTheDocument();
    // "spawned 0" is the tick saying nothing happened; it is not a finding.
    expect(screen.queryByText("spawned")).toBeNull();
    // The run that follows is still its own row.
    expect(screen.getByText("lease held")).toBeInTheDocument();
    expect(screen.getByText("Nothing happened on this tick.")).toBeInTheDocument();
  });

  it("degrades gracefully when the server has no tick-summaries endpoint", async () => {
    vi.mocked(fetchBuildTickSummaries).mockResolvedValue(null);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /3 tasks · details/ }));

    expect(
      await screen.findByText(/does not record tick history/i),
    ).toBeInTheDocument();
    // The rest of the panel is unaffected.
    expect(screen.getByText("Not progressing")).toBeInTheDocument();
  });

  it("releases a claim under the owning build after confirmation, then refetches", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({ blocked_by_external: [makeBlocker()] }),
    );
    vi.mocked(cancelTask).mockResolvedValue("cancelled");
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "Release claim on GrindBeans" }),
    );
    // The dialog names the build the action is addressed to.
    expect(
      await screen.findByText("Release this task's execution claim"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/a different build from the one you are viewing/),
    ).toBeInTheDocument();
    expect(cancelTask).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Release claim" }));
    await waitFor(() =>
      // Addressed to the OWNING build, not the one on screen.
      expect(cancelTask).toHaveBeenCalledWith(OWNER_BUILD, "tid-grind-beans", "env-1"),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      /Released the claim on GrindBeans/,
    );
    await waitFor(() => expect(fetchBuildFrontier).toHaveBeenCalledTimes(2));
  });

  it("offers a reset, not a release, for a blocker that is not running", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        blocked_by_external: [makeBlocker({ blocking_status: "failed" })],
      }),
    );
    vi.mocked(retryTask).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPanel();

    expect(
      await screen.findByRole("button", { name: "Reset to pending on GrindBeans" }),
    ).toBeInTheDocument();
    // A failed task holds no claim, so there is nothing to release.
    expect(
      screen.queryByRole("button", { name: "Release claim on GrindBeans" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Reset to pending on GrindBeans" }),
    );
    await user.click(screen.getByRole("button", { name: "Reset to pending" }));
    await waitFor(() =>
      expect(retryTask).toHaveBeenCalledWith(OWNER_BUILD, "tid-grind-beans", "env-1"),
    );
  });

  it("offers no remedy when the owning build was never recorded", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({
        blocked_by_external: [makeBlocker({ blocking_status_build_id: null })],
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText("no owning build")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Release claim on/ }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Explain why GrindBeans is blocking" }),
    );
    expect(
      await screen.findByText(/there is nothing to address a cancel or retry to/),
    ).toBeInTheDocument();
  });

  it("hides cross-build remedies from non-admin members", async () => {
    mockWorkspaceRole = "member";
    vi.mocked(fetchBuildFrontier).mockResolvedValue(
      makeFrontier({ blocked_by_external: [makeBlocker()] }),
    );
    renderPanel();

    const user = userEvent.setup();
    // The diagnosis is for everyone...
    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    // ...the destructive cross-build remedy is not.
    expect(
      screen.queryByRole("button", { name: /Release claim on/ }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Explain why GrindBeans is blocking" }),
    );
    expect(
      await screen.findByText(/requires the\s+workspace admin role/),
    ).toBeInTheDocument();
  });

  it("reports a frontier read failure instead of failing silently", async () => {
    vi.mocked(fetchBuildFrontier).mockRejectedValue(new Error("Build not found"));
    renderPanel();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /Could not read this build.s scheduler state.*Build not found/,
    );
  });

  it("says what is pending when a stalled build still has a wake-up queued", async () => {
    vi.mocked(fetchBuildFrontier).mockResolvedValue(makeFrontier({ needs_tick: true }));
    const user = userEvent.setup();
    renderPanel();

    // The headline says it without being expanded — it is the difference
    // between "wait" and "intervene".
    expect(await screen.findByText("wake-up pending")).toBeInTheDocument();
    expect(
      screen.getByText(/Nothing runnable — a scheduler wake-up is still pending/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /3 tasks · details/ }));
    expect(
      await screen.findByText(
        /A scheduler wake-up is pending, so the next tick may still/,
      ),
    ).toBeInTheDocument();
  });

  it("says nothing will happen when a stalled reactive build has no wake-up queued", async () => {
    const user = userEvent.setup();
    renderPanel();
    expect(
      await screen.findByText(/no wake-up pending — needs intervention/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /3 tasks · details/ }));
    expect(
      await screen.findByText(/Nothing is going to happen without intervention/),
    ).toBeInTheDocument();
  });
});
