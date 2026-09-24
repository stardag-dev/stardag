import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Deployment } from "../types/task";

vi.mock("../api/registry", () => ({
  fetchDeployments: vi.fn(),
  fetchDeployment: vi.fn(),
}));

import { fetchDeployment, fetchDeployments } from "../api/registry";
import { useDeployments } from "./useDeployments";

function deployment(id: string, generation: number): Deployment {
  return {
    id,
    kind: "modal",
    app_name: "app",
    code_id: "c",
    image_id: null,
    modal_app_id: null,
    generation,
    deployed_at: "2026-09-24T00:00:00Z",
    activated_at: "2026-09-24T00:00:00Z",
    is_current: false,
  };
}

beforeEach(() => {
  vi.mocked(fetchDeployments).mockReset();
  vi.mocked(fetchDeployment).mockReset();
});

describe("useDeployments", () => {
  it("looks up a wanted deployment the list's first page does not hold", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([deployment("d-new", 600)]);
    vi.mocked(fetchDeployment).mockResolvedValue(deployment("d-old", 3));
    const { result } = renderHook(() =>
      useDeployments("env-1", ["d-new", "d-old", null]),
    );
    await waitFor(() => expect(result.current.byId.get("d-old")?.generation).toBe(3));
    expect(fetchDeployment).toHaveBeenCalledTimes(1);
    expect(fetchDeployment).toHaveBeenCalledWith("d-old", "env-1");
    expect(result.current.byId.get("d-new")?.generation).toBe(600);
  });

  it("does not retry a lookup that failed on every render", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    vi.mocked(fetchDeployment).mockRejectedValue(new Error("404"));
    const { result, rerender } = renderHook(() => useDeployments("env-1", ["d-gone"]));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledTimes(1));
    rerender();
    rerender();
    expect(fetchDeployment).toHaveBeenCalledTimes(1);
    expect(result.current.byId.has("d-gone")).toBe(false);
  });
});
