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
});
