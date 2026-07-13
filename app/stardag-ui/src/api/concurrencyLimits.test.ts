import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAccessTokenGetter, setCurrentWorkspaceId } from "./client";
import {
  deleteConcurrencyLimit,
  evictConcurrencyLimitHolder,
  fetchConcurrencyLimitHolders,
  fetchConcurrencyLimits,
  upsertConcurrencyLimit,
} from "./concurrencyLimits";

describe("concurrency limits API client", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setAccessTokenGetter(async () => "token");
    setCurrentWorkspaceId("ws-1");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessTokenGetter(async () => null);
    setCurrentWorkspaceId(null);
  });

  function requestedUrl(): string {
    return fetchMock.mock.calls[0][0] as string;
  }

  it("fetches and unwraps the limits list", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ limits: [{ key: "gpu", max_concurrent: 2 }] }), {
        status: 200,
      }),
    );

    const limits = await fetchConcurrencyLimits("env-1");
    expect(limits).toEqual([{ key: "gpu", max_concurrent: 2 }]);
    expect(requestedUrl()).toContain("/api/v1/concurrency-limits?");
    expect(requestedUrl()).toContain("environment_id=env-1");
  });

  it("upserts a limit via PUT with a JSON body", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ key: "gpu", max_concurrent: 3 }), {
        status: 200,
      }),
    );

    const result = await upsertConcurrencyLimit("gpu", 3, "env-1");
    expect(result).toEqual({ key: "gpu", max_concurrent: 3 });
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/v1/concurrency-limits/gpu?");
    expect(options.method).toBe("PUT");
    expect(JSON.parse(options.body as string)).toEqual({ max_concurrent: 3 });
  });

  it("URL-encodes the limit key", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteConcurrencyLimit("db/write slots", "env-1");
    expect(requestedUrl()).toContain("/concurrency-limits/db%2Fwrite%20slots?");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("fetches holders with a limit param", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ key: "gpu", holders: [], total: 0 }), {
        status: 200,
      }),
    );

    const result = await fetchConcurrencyLimitHolders("gpu", "env-1", 5);
    expect(result.total).toBe(0);
    expect(requestedUrl()).toContain("/concurrency-limits/gpu/holders?");
    expect(requestedUrl()).toContain("limit=5");
    expect(requestedUrl()).toContain("environment_id=env-1");
  });

  it("evicts a holder via POST", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ task_id: "t-1", status: "failed" }), {
        status: 200,
      }),
    );

    const result = await evictConcurrencyLimitHolder("gpu", "t-1", "env-1");
    expect(result).toEqual({ task_id: "t-1", status: "failed" });
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/concurrency-limits/gpu/holders/t-1/evict?");
    expect(options.method).toBe("POST");
  });

  it("throws on non-OK responses", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("nope", { status: 404, statusText: "Not Found" }),
    );
    await expect(evictConcurrencyLimitHolder("gpu", "t-1", "env-1")).rejects.toThrow(
      "Failed to evict holder: Not Found",
    );
  });
});
