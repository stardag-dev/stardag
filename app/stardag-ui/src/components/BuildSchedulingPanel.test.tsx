import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BuildFrontier, Deployment, PlanDetail } from "../types/task";

vi.mock("../api/registry", () => ({
  fetchBuildPlans: vi.fn(),
  fetchBuildNotify: vi.fn(),
  fetchBuildTickSummaries: vi.fn(async () => ({ build_id: "b", summaries: [] })),
}));

import { fetchBuildNotify, fetchBuildPlans } from "../api/registry";
import { BuildSchedulingPanel } from "./BuildSchedulingPanel";

function deployment(generation: number, isCurrent: boolean): Deployment {
  return {
    id: `dep-${generation}`,
    kind: "modal",
    app_name: "etl",
    code_id: `code-${generation}`,
    image_id: null,
    modal_app_id: null,
    generation,
    deployed_at: "2026-09-24T00:00:00Z",
    activated_at: "2026-09-24T00:00:00Z",
    is_current: isCurrent,
  };
}

function plan(generation: number, overrides: Partial<PlanDetail> = {}): PlanDetail {
  return {
    id: `plan-${generation}`,
    build_id: "b",
    deployment_id: `dep-${generation}`,
    deployment: deployment(generation, false),
    settings_hash: `settings${generation}abcdef`,
    generation,
    created_at: "2026-09-24T00:00:00Z",
    activated_at: "2026-09-24T00:00:00Z",
    sealed_at: null,
    superseded_at: null,
    is_active: false,
    member_count: 3,
    root_count: 1,
    excluded_count: 0,
    member_counts: { pending: 3 },
    ...overrides,
  };
}

const frontier: BuildFrontier = {
  build_id: "b",
  plan_id: "plan-2",
  deployment_id: "dep-2",
  settings_hash: "settings2abcdef",
  sealed: true,
  plan_complete: false,
  build_status: "running",
  reactive_app_name: null,
  reactive_tick_kwargs: null,
  runnable: [],
  discovery_jobs: [],
  running: [],
  closure: null,
};

describe("BuildSchedulingPanel", () => {
  it("is 'Plans and scheduling', listing the build's plans with the active one marked", async () => {
    vi.mocked(fetchBuildPlans).mockResolvedValue([
      plan(2, {
        is_active: true,
        sealed_at: "2026-09-24T00:00:01Z",
        deployment: deployment(2, true),
        excluded_count: 1,
      }),
      plan(1, { superseded_at: "2026-09-24T00:00:02Z" }),
    ]);
    render(
      <BuildSchedulingPanel
        buildId="b"
        environmentId="env-1"
        buildStatus="running"
        frontier={frontier}
        frontierError={null}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    expect(
      screen.getByRole("heading", { name: "Plans and scheduling" }),
    ).toBeInTheDocument();

    const active = await screen.findByRole("listitem", {
      name: "Plan generation 2 (active)",
    });
    expect(within(active).getByText("active")).toBeInTheDocument();
    expect(within(active).getByText("etl gen 2 · modal")).toBeInTheDocument();
    expect(within(active).getByText("settings settings")).toBeInTheDocument();
    expect(within(active).getByText(/1 excluded/)).toBeInTheDocument();

    const old = screen.getByRole("listitem", { name: "Plan generation 1" });
    expect(
      within(old).getByText("etl gen 1 · modal (not current)"),
    ).toBeInTheDocument();
    expect(within(old).queryByText("active")).not.toBeInTheDocument();
    expect(vi.mocked(fetchBuildPlans)).toHaveBeenCalledWith("b", "env-1");
  });

  function renderPanel(
    props: Partial<Parameters<typeof BuildSchedulingPanel>[0]> = {},
  ) {
    return render(
      <BuildSchedulingPanel
        buildId="b"
        environmentId="env-1"
        buildStatus="running"
        frontier={frontier}
        frontierError={null}
        {...props}
      />,
    );
  }

  it("shows the active plan's members by status, as v1's chips", async () => {
    vi.mocked(fetchBuildPlans).mockResolvedValue([
      plan(2, {
        is_active: true,
        member_counts: { completed: 4, running: 1, pending: 0 },
        excluded_count: 2,
      }),
    ]);
    renderPanel({ frontier: { ...frontier, running: [] } });
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    const chips = await screen.findByLabelText("Active plan members by status");
    expect(chips.textContent).toBe("running1completed4excluded2");
  });

  it("shows a spinner while the plans and the frontier are read", () => {
    vi.mocked(fetchBuildPlans).mockReturnValue(new Promise(() => {}));
    renderPanel({ frontier: null });
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    const reading = screen.getAllByRole("status");
    expect(reading.map((r) => r.textContent)).toEqual([
      "Reading this build’s plans…",
      "Reading this build’s frontier…",
    ]);
  });

  it("does not say 'needs intervention' while a wake-up is queued", async () => {
    vi.mocked(fetchBuildPlans).mockResolvedValue([]);
    vi.mocked(fetchBuildNotify).mockResolvedValue({ build_id: "b", needs_tick: true });
    renderPanel({ frontier: { ...frontier, reactive_app_name: "etl" } });
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    expect(await screen.findByText(/a wake-up is queued/)).toBeInTheDocument();
    expect(screen.queryByText(/needs intervention/)).not.toBeInTheDocument();
    expect(vi.mocked(fetchBuildNotify)).toHaveBeenCalledWith("b", "env-1");
  });

  it("says 'needs intervention' when no wake-up is queued", async () => {
    vi.mocked(fetchBuildPlans).mockResolvedValue([]);
    vi.mocked(fetchBuildNotify).mockResolvedValue({ build_id: "b", needs_tick: false });
    renderPanel({ frontier: { ...frontier, reactive_app_name: "etl" } });
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    expect(await screen.findByText(/needs intervention/)).toBeInTheDocument();
  });

  it("never says the next tick completes a failed or cancelled build", async () => {
    vi.mocked(fetchBuildPlans).mockResolvedValue([]);
    renderPanel({
      buildStatus: "cancelled",
      frontier: { ...frontier, plan_complete: true, build_status: "cancelled" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Plans and scheduling" }));
    expect(
      await screen.findByText(/recorded as cancelled, so no tick acts on it/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/next tick completes/)).not.toBeInTheDocument();
  });
});
