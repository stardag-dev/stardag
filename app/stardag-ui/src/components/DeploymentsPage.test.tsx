import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Deployment } from "../types/task";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({ activeEnvironment: { id: "env-1" } }),
}));
vi.mock("../api/registry", () => ({ fetchDeployments: vi.fn() }));

import { fetchDeployments } from "../api/registry";
import { DeploymentsPage } from "./DeploymentsPage";
import { DEPLOYMENT_LIST_LIMIT } from "../hooks/useDeployments";

function deployment(overrides: Partial<Deployment>): Deployment {
  return {
    id: "d",
    kind: "modal",
    app_name: "etl",
    code_id: "code-1",
    image_id: null,
    modal_app_id: null,
    generation: 1,
    deployed_at: "2026-09-24T00:00:00Z",
    activated_at: "2026-09-24T00:00:01Z",
    is_current: false,
    ...overrides,
  };
}

describe("DeploymentsPage", () => {
  it("lists an app's generations with the current one marked", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "d1", generation: 1 }),
      deployment({
        id: "d2",
        generation: 2,
        code_id: "code-2",
        is_current: true,
        modal_app_id: "ap-123",
      }),
      deployment({ id: "d3", generation: 3, code_id: "code-3", activated_at: null }),
    ]);
    render(
      <BreadcrumbProvider>
        <DeploymentsPage />
      </BreadcrumbProvider>,
    );
    const card = (await screen.findByRole("heading", { name: "etl" })).closest(
      "section",
    )!;
    expect(within(card).getByText("current: generation 2")).toBeInTheDocument();
    expect(within(card).getByText("not activated")).toBeInTheDocument();
    expect(within(card).getByText("ap-123")).toBeInTheDocument();
    const rows = within(card).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent?.slice(0, 1))).toEqual(["3", "2", "1"]);
  });

  it("states the cap honestly instead of claiming 'newest' when it is hit", async () => {
    // The registry orders `GET /deployments` by (kind, app_name,
    // generation DESC) before applying the limit, so hitting the cap does
    // not mean "the newest N" — it can drop whole apps ordered later.
    vi.mocked(fetchDeployments).mockResolvedValue(
      Array.from({ length: DEPLOYMENT_LIST_LIMIT }, (_, i) =>
        deployment({ id: `d${i}`, generation: i + 1 }),
      ),
    );
    render(
      <BreadcrumbProvider>
        <DeploymentsPage />
      </BreadcrumbProvider>,
    );
    await screen.findByRole("heading", { name: "etl" });
    expect(screen.queryByText(/^Showing the newest/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        `Showing the first ${DEPLOYMENT_LIST_LIMIT} deployment rows, ordered by app — not necessarily the newest; some apps or older generations may not be listed.`,
      ),
    ).toBeInTheDocument();
  });
});
