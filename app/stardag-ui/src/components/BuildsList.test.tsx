import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import type { Build, BulkCancelBuildsResponse } from "../types/task";
import { BuildsList } from "./BuildsList";

let mockEnvironmentId = "env-1";
let mockWorkspaceRole: "owner" | "admin" | "member" | null = "admin";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: {
      id: mockEnvironmentId,
      slug: "default",
      name: "default",
    },
    activeWorkspaceRole: mockWorkspaceRole,
  }),
}));

vi.mock("../api/tasks", () => ({
  fetchBuilds: vi.fn(),
  bulkCancelBuilds: vi.fn(),
}));

import { bulkCancelBuilds, fetchBuilds } from "../api/tasks";

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function makeBuild(overrides: Partial<Build> & Pick<Build, "id" | "name">): Build {
  return {
    environment_id: "env-1",
    user_id: null,
    description: null,
    commit_hash: null,
    root_task_ids: [],
    created_at: ago(10 * DAY),
    status: "running",
    started_at: ago(10 * DAY),
    completed_at: null,
    status_triggered_by_user: null,
    // Deliberately older than every `last_activity_at` below: the table
    // must never surface this lifecycle timestamp as activity.
    last_active_at: ago(6 * DAY),
    last_activity_at: ago(2 * HOUR),
    ...overrides,
  };
}

// Three obviously fictional builds: one stale, one busy reactive build,
// one already finished.
const staleBuild = makeBuild({
  id: "b-stale",
  name: "nightly-refresh",
  description: "Refresh the demo warehouse",
  commit_hash: "abcdef1234567",
  last_activity_at: ago(3 * DAY),
});
const busyBuild = makeBuild({
  id: "b-busy",
  name: "hourly-ingest",
  reactive_app_name: "demo-scheduler",
  last_activity_at: ago(5 * 60 * 1000),
});
const doneBuild = makeBuild({
  id: "b-done",
  name: "feature-sandbox",
  status: "completed",
  completed_at: ago(2 * HOUR),
  last_activity_at: ago(2 * HOUR),
});

const ALL_BUILDS = [staleBuild, busyBuild, doneBuild];

function buildsResponse(builds: Build[], total = builds.length) {
  return { builds, total, page: 1, page_size: 20 };
}

function bulkResponse(
  overrides: Partial<BulkCancelBuildsResponse> = {},
): BulkCancelBuildsResponse {
  return {
    dry_run: false,
    builds: [],
    build_count: 0,
    task_count: 0,
    skipped: {},
    truncated: false,
    ...overrides,
  };
}

const onSelectBuild = vi.fn();

function renderList() {
  return render(
    <BreadcrumbProvider>
      <BuildsList onSelectBuild={onSelectBuild} />
    </BreadcrumbProvider>,
  );
}

/** The row whose Build cell carries `name`. */
function rowFor(name: string): HTMLElement {
  return screen.getByRole("button", { name }).closest("tr") as HTMLElement;
}

describe("BuildsList", () => {
  beforeEach(() => {
    mockEnvironmentId = "env-1";
    mockWorkspaceRole = "admin";
    vi.mocked(fetchBuilds).mockResolvedValue(buildsResponse(ALL_BUILDS));
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows last_activity_at as the activity signal, never last_active_at", async () => {
    renderList();

    expect(await screen.findByText("nightly-refresh")).toBeInTheDocument();
    // last_activity_at of each build renders...
    expect(screen.getByText("3d ago")).toBeInTheDocument();
    expect(screen.getByText("5m ago")).toBeInTheDocument();
    // ...and the (older) lifecycle column never does.
    expect(screen.queryByText("6d ago")).not.toBeInTheDocument();

    // The absolute time is available on hover.
    expect(screen.getByText("3d ago")).toHaveAttribute(
      "title",
      expect.stringContaining("Last activity"),
    );
    // A running build quiet for over a day is flagged as such.
    expect(screen.getByText("3d ago").getAttribute("title")).toContain(
      "nothing has happened for over a day",
    );
  });

  it("renders the short commit chip and the reactive app chip", async () => {
    renderList();
    expect(await screen.findByText("abcdef1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "demo-scheduler" })).toBeInTheDocument();
  });

  it("sends the status filter to the server", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.selectOptions(
      screen.getByLabelText("Filter by build status"),
      "running",
    );

    await waitFor(() =>
      expect(fetchBuilds).toHaveBeenCalledWith(
        expect.objectContaining({ status: "running", environment_id: "env-1" }),
      ),
    );
  });

  it("filters by reactive app when its chip is clicked", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(screen.getByRole("button", { name: "demo-scheduler" }));

    // Debounced, so the request lands shortly after the click.
    await waitFor(() =>
      expect(fetchBuilds).toHaveBeenCalledWith(
        expect.objectContaining({ reactive_app_name: "demo-scheduler" }),
      ),
    );
    // The filter is visible, not hidden state.
    expect(screen.getByLabelText("Filter by reactive app")).toHaveValue(
      "demo-scheduler",
    );
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("asks the server for idle builds instead of filtering locally", async () => {
    // The API filters, counts, paginates and orders by idleness with the
    // same predicate the cleanup sweep uses, so the component must send
    // the threshold and render whatever comes back verbatim.
    vi.mocked(fetchBuilds).mockResolvedValue(buildsResponse([staleBuild], 1));
    const user = userEvent.setup();
    renderList();
    await screen.findByLabelText("Filter by time since last activity");

    await user.selectOptions(
      screen.getByLabelText("Filter by time since last activity"),
      String(DAY / 1000),
    );

    await waitFor(() =>
      expect(fetchBuilds).toHaveBeenCalledWith(
        expect.objectContaining({
          idle_for_seconds: 86400,
          page: 1,
          page_size: 20,
          environment_id: "env-1",
        }),
      ),
    );
    expect(
      await screen.findByText(/1 build idle ≥ 24h, stalest first/),
    ).toBeInTheDocument();
    // No over-fetching and no local caveat: `total` is exact and
    // pagination is server-side for this filter.
    expect(fetchBuilds).not.toHaveBeenCalledWith(
      expect.objectContaining({ page_size: 100 }),
    );
    expect(screen.queryByText(/older matches may be missing/)).not.toBeInTheDocument();
  });

  it("does not re-sort or re-filter the page the server returned", async () => {
    // The server orders stalest-first on an index-backed proxy for last
    // activity, so the rendered column is not strictly monotonic. The
    // component must render the server's order as given rather than
    // imposing a local ordering on one page of a server-side set — and it
    // must not drop a row whose `last_activity_at` looks too recent.
    vi.mocked(fetchBuilds).mockResolvedValue(
      buildsResponse([busyBuild, staleBuild], 2),
    );
    const user = userEvent.setup();
    renderList();
    await screen.findByLabelText("Filter by time since last activity");

    await user.selectOptions(
      screen.getByLabelText("Filter by time since last activity"),
      String(DAY / 1000),
    );

    await waitFor(() =>
      expect(
        screen.getByText("2 builds idle ≥ 24h, stalest first"),
      ).toBeInTheDocument(),
    );
    const names = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector("td:nth-child(3) button")?.textContent);
    expect(names).toEqual(["hourly-ingest", "nightly-refresh"]);
  });

  it("makes the status + idleness combination the API rejects unreachable", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    const statusSelect = screen.getByLabelText("Filter by build status");
    const idleSelect = screen.getByLabelText("Filter by time since last activity");

    // With an idle filter set, only "All statuses" and "Running" remain
    // selectable — the rest are the 422.
    await user.selectOptions(idleSelect, String(DAY / 1000));
    expect(
      within(statusSelect).getByRole("option", { name: /^Running/ }),
    ).toBeEnabled();
    expect(
      within(statusSelect).getByRole("option", { name: /^Failed/ }),
    ).toBeDisabled();
    await user.selectOptions(statusSelect, "running");
    await waitFor(() =>
      expect(fetchBuilds).toHaveBeenCalledWith(
        expect.objectContaining({ status: "running", idle_for_seconds: 86400 }),
      ),
    );

    // ...and symmetrically: an incompatible status disables the idle
    // filter rather than silently dropping it.
    await user.selectOptions(idleSelect, "0");
    await user.selectOptions(statusSelect, "failed");
    expect(idleSelect).toBeDisabled();
    expect(idleSelect).toHaveValue("0");
  });

  it("selects rows, supports select-all, and reports an indeterminate header", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    const selectAll = screen.getByRole("checkbox", {
      name: "Select all builds on this page",
    }) as HTMLInputElement;
    await user.click(selectAll);

    expect(
      screen.getByRole("checkbox", { name: "Select build nightly-refresh" }),
    ).toBeChecked();
    expect(screen.getByText("3 builds selected")).toBeInTheDocument();
    expect(selectAll.indeterminate).toBe(false);

    // Deselecting one row leaves the header mixed.
    await user.click(
      screen.getByRole("checkbox", { name: "Select build hourly-ingest" }),
    );
    expect(screen.getByText("2 builds selected")).toBeInTheDocument();
    expect(selectAll).not.toBeChecked();
    expect(selectAll.indeterminate).toBe(true);
  });

  it("does not navigate when the selection checkbox is clicked", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(
      screen.getByRole("checkbox", { name: "Select build nightly-refresh" }),
    );
    expect(onSelectBuild).not.toHaveBeenCalled();
    expect(screen.getByText("1 build selected")).toBeInTheDocument();

    // Clicking the cell around the checkbox is equally inert.
    const cell = screen
      .getByRole("checkbox", { name: "Select build nightly-refresh" })
      .closest("td") as HTMLElement;
    await user.click(cell);
    expect(onSelectBuild).not.toHaveBeenCalled();
  });

  it("opens the build from the row and from the keyboard-focusable name button", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    // The name is a real button, so it is reachable and activatable by
    // keyboard — the row itself is only a mouse convenience.
    const nameButton = screen.getByRole("button", { name: "nightly-refresh" });
    nameButton.focus();
    expect(nameButton).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onSelectBuild).toHaveBeenCalledWith("b-stale");

    onSelectBuild.mockClear();
    await user.click(within(rowFor("feature-sandbox")).getByText("feature-sandbox"));
    expect(onSelectBuild).toHaveBeenCalledWith("b-done");
  });

  it("clears the selection when the page changes", async () => {
    vi.mocked(fetchBuilds).mockResolvedValue(buildsResponse(ALL_BUILDS, 45));
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(
      screen.getByRole("checkbox", { name: "Select build nightly-refresh" }),
    );
    expect(screen.getByText("1 build selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(screen.queryByText("1 build selected")).not.toBeInTheDocument(),
    );
  });

  it("dry-runs a bulk cancel, shows the consequences, then applies", async () => {
    vi.mocked(bulkCancelBuilds).mockImplementation(async (request) =>
      request.dry_run
        ? bulkResponse({
            dry_run: true,
            builds: [
              {
                build_id: "b-stale",
                name: "nightly-refresh",
                last_activity_at: staleBuild.last_activity_at ?? null,
                reactive_app_name: null,
                cascaded_task_ids: ["t-1", "t-2"],
              },
              {
                build_id: "b-busy",
                name: "hourly-ingest",
                last_activity_at: busyBuild.last_activity_at ?? null,
                reactive_app_name: "demo-scheduler",
                cascaded_task_ids: ["t-3"],
              },
            ],
            build_count: 2,
            task_count: 3,
            skipped: { "b-done": "not_running" },
          })
        : bulkResponse({ build_count: 2, task_count: 3, skipped: {} }),
    );

    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(
      screen.getByRole("checkbox", { name: "Select all builds on this page" }),
    );
    await user.click(screen.getByRole("button", { name: "Cancel builds…" }));

    // The dry run runs first and reports exactly what will happen.
    expect(
      await screen.findByText("Will cancel 2 builds and release 3 task claims."),
    ).toBeInTheDocument();
    expect(bulkCancelBuilds).toHaveBeenCalledWith(
      expect.objectContaining({
        dry_run: true,
        cascade: true,
        include_reactive: false,
        build_ids: ["b-stale", "b-busy", "b-done"],
      }),
      "env-1",
    );
    // Per-build skip reason is surfaced, not swallowed — named, with the
    // reason spelled out rather than left as an opaque enum value.
    const skippedBlock = screen.getByText("Skipped (1)").parentElement as HTMLElement;
    expect(skippedBlock).toHaveTextContent(
      "feature-sandbox — Already finished — only running builds can be cancelled",
    );

    // Nothing has been written yet.
    expect(bulkCancelBuilds).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Cancel 2 builds" }));

    await waitFor(() =>
      expect(bulkCancelBuilds).toHaveBeenCalledWith(
        expect.objectContaining({ dry_run: false, cascade: true }),
        "env-1",
      ),
    );
    expect(
      await screen.findByText(/Cancelled 2 builds and released 3 task claims\./),
    ).toBeInTheDocument();
    // Selection cleared and the list refetched after the mutation.
    expect(screen.queryByText("2 builds selected")).not.toBeInTheDocument();
  });

  it("re-runs the dry run when cascade is switched off", async () => {
    vi.mocked(bulkCancelBuilds).mockImplementation(async (request) =>
      bulkResponse({
        dry_run: true,
        build_count: 1,
        task_count: request.cascade ? 4 : 0,
      }),
    );
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(
      screen.getByRole("checkbox", { name: "Select build nightly-refresh" }),
    );
    await user.click(screen.getByRole("button", { name: "Cancel builds…" }));
    expect(
      await screen.findByText("Will cancel 1 build and release 4 task claims."),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("checkbox", {
        name: "Release the execution claims these builds' tasks hold",
      }),
    );
    expect(
      await screen.findByText(
        "Will cancel 1 build and release no task claims (cascade is off).",
      ),
    ).toBeInTheDocument();
  });

  it("surfaces truncation and a dry-run failure in the dialog", async () => {
    vi.mocked(bulkCancelBuilds).mockRejectedValue(
      new Error("Provide build_ids and/or idle_for_seconds."),
    );
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    await user.click(
      screen.getByRole("checkbox", { name: "Select build nightly-refresh" }),
    );
    await user.click(screen.getByRole("button", { name: "Cancel builds…" }));

    expect(
      await screen.findByText("Provide build_ids and/or idle_for_seconds."),
    ).toBeInTheDocument();
    // Nothing to confirm when the preview failed.
    expect(screen.getByRole("button", { name: "Cancel 0 builds" })).toBeDisabled();
  });

  it("sweeps by idleness server-side once an idle filter is chosen", async () => {
    vi.mocked(bulkCancelBuilds).mockResolvedValue(
      bulkResponse({ dry_run: true, build_count: 12, task_count: 30, truncated: true }),
    );
    const user = userEvent.setup();
    renderList();
    await screen.findByText("nightly-refresh");

    // Disabled until the threshold is explicit and visible in the filters.
    expect(
      screen.getByRole("button", { name: "Clean up idle builds…" }),
    ).toBeDisabled();

    await user.selectOptions(
      screen.getByLabelText("Filter by time since last activity"),
      String(DAY / 1000),
    );
    const sweep = screen.getByRole("button", { name: "Clean up idle builds…" });
    expect(sweep).toBeEnabled();
    await user.click(sweep);

    expect(
      await screen.findByText("Will cancel 12 builds and release 30 task claims."),
    ).toBeInTheDocument();
    expect(bulkCancelBuilds).toHaveBeenCalledWith(
      expect.objectContaining({ dry_run: true, idle_for_seconds: 86400 }),
      "env-1",
    );
    expect(
      screen.getByText(/More builds match than one call may cancel/),
    ).toBeInTheDocument();
  });

  it("hides selection and bulk cleanup from non-admin members", async () => {
    mockWorkspaceRole = "member";
    renderList();
    await screen.findByText("nightly-refresh");

    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Clean up idle builds…" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Bulk cleanup requires the workspace admin role"),
    ).toBeInTheDocument();
    // Filters and navigation still work for everyone.
    expect(screen.getByLabelText("Filter by build status")).toBeInTheDocument();
  });

  it("distinguishes an empty environment from an empty filter result", async () => {
    vi.mocked(fetchBuilds).mockResolvedValue(buildsResponse([]));
    const user = userEvent.setup();
    renderList();

    expect(await screen.findByText("No builds yet")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by build status"), "failed");
    expect(
      await screen.findByText("No builds match these filters"),
    ).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    expect(await screen.findByText("No builds yet")).toBeInTheDocument();
  });

  it("shows a load error with a retry that refetches", async () => {
    vi.mocked(fetchBuilds).mockRejectedValueOnce(new Error("Failed to fetch builds"));
    const user = userEvent.setup();
    renderList();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Failed to fetch builds",
    );

    vi.mocked(fetchBuilds).mockResolvedValue(buildsResponse(ALL_BUILDS));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("nightly-refresh")).toBeInTheDocument();
  });
});
