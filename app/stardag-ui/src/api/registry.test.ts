import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({ fetchWithAuth: vi.fn() }));
vi.mock("./config", () => ({ API_V2: "https://api.test/api/v2" }));

import { fetchWithAuth } from "./client";
import {
  completeBuild,
  fetchBuilds,
  fetchPlanGraph,
  fetchTask,
  RegistryError,
} from "./registry";

const mocked = vi.mocked(fetchWithAuth);

function respond(status: number, body: unknown) {
  mocked.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

beforeEach(() => mocked.mockReset());

describe("registry API", () => {
  it("calls only /api/v2, with the environment id", async () => {
    respond(200, { builds: [], total: 0, next_cursor: null });
    await fetchBuilds("env-1", {
      status: "running",
      reactiveAppName: "app",
      idleForSeconds: 3600,
      limit: 50,
      cursor: "c1",
    });
    const url = new URL(mocked.mock.calls[0][0] as string);
    expect(url.pathname).toBe("/api/v2/builds");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      environment_id: "env-1",
      status: "running",
      reactive_app_name: "app",
      idle_for_seconds: "3600",
      limit: "50",
      cursor: "c1",
    });
  });

  it("sends force on complete", async () => {
    respond(200, { id: "b" });
    await completeBuild("b", "env-1", true);
    const init = mocked.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ force: true });
  });

  it("surfaces a v2 refusal's code and detail", async () => {
    respond(409, {
      detail: { code: "plan_incomplete", detail: "3 members outstanding" },
    });
    const error = await completeBuild("b", "env-1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RegistryError);
    expect(error).toMatchObject({
      status: 409,
      code: "plan_incomplete",
      message: "3 members outstanding",
    });
  });

  it("reads a 404 on the assumed plan-graph route as not served", async () => {
    respond(404, { detail: "Not Found" });
    await expect(fetchPlanGraph("p", "env-1")).resolves.toBeNull();
  });

  it("does not hide a 404 on a served route", async () => {
    respond(404, { detail: { code: "task_not_found", detail: "no such task" } });
    await expect(fetchTask("t", "env-1")).rejects.toMatchObject({
      code: "task_not_found",
    });
  });
});
