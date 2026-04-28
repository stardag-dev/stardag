import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchWithAuth,
  setAccessTokenGetter,
  setCurrentWorkspaceId,
  setSessionExpiredHandler,
  type GetAccessToken,
} from "./client";

const nullTokenGetter: GetAccessToken = async () => null;

describe("fetchWithAuth — 401 retry semantics", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setCurrentWorkspaceId("ws-1");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessTokenGetter(nullTokenGetter);
    setSessionExpiredHandler(null);
    setCurrentWorkspaceId(null);
  });

  it("retries once with a refreshed token after a 401", async () => {
    let call = 0;
    const impl: GetAccessToken = async (_workspaceId, opts) => {
      call += 1;
      if (opts?.forceRefresh) return "fresh-token";
      return "stale-token";
    };
    const tokenGetter = vi.fn(impl);
    setAccessTokenGetter(tokenGetter);

    fetchMock
      .mockResolvedValueOnce(new Response("unauthorized", { status: 401 }))
      .mockResolvedValueOnce(new Response('{"ok":true}', { status: 200 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(200);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, secondCall] = fetchMock.mock.calls;
    const headers = (secondCall[1] as RequestInit).headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer fresh-token");

    // Token getter was called twice: once normally, once with forceRefresh.
    expect(tokenGetter).toHaveBeenCalledTimes(2);
    expect(tokenGetter.mock.calls[0][1]).toEqual({ forceRefresh: false });
    expect(tokenGetter.mock.calls[1][1]).toEqual({ forceRefresh: true });
    expect(call).toBe(2);
  });

  it("returns the original response without retry when status is not 401", async () => {
    const tokenGetter: GetAccessToken = async () => "tok";
    setAccessTokenGetter(tokenGetter);
    fetchMock.mockResolvedValueOnce(new Response("forbidden", { status: 403 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry when no token was attached on attempt 1", async () => {
    setAccessTokenGetter(nullTokenGetter);
    fetchMock.mockResolvedValueOnce(new Response("unauthorized", { status: 401 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("calls the session-expired handler when the retry also returns 401", async () => {
    const impl: GetAccessToken = async (_workspaceId, opts) =>
      opts?.forceRefresh ? "fresh-token" : "stale-token";
    const tokenGetter = vi.fn(impl);
    setAccessTokenGetter(tokenGetter);
    const handler = vi.fn();
    setSessionExpiredHandler(handler);

    fetchMock
      .mockResolvedValueOnce(new Response("unauthorized", { status: 401 }))
      .mockResolvedValueOnce(new Response("still unauthorized", { status: 401 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(401);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("calls the session-expired handler when the refresh returns the same token", async () => {
    // Cognito silent renew can return the same token if it's not yet
    // expired client-side; in that case retrying with the same bearer
    // would just produce another 401 — short-circuit and signal expired.
    const impl: GetAccessToken = async () => "same-token";
    const tokenGetter = vi.fn(impl);
    setAccessTokenGetter(tokenGetter);
    const handler = vi.fn();
    setSessionExpiredHandler(handler);

    fetchMock.mockResolvedValueOnce(new Response("unauthorized", { status: 401 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("calls the session-expired handler when refresh returns null", async () => {
    const impl: GetAccessToken = async (_workspaceId, opts) =>
      opts?.forceRefresh ? null : "stale-token";
    const tokenGetter = vi.fn(impl);
    setAccessTokenGetter(tokenGetter);
    const handler = vi.fn();
    setSessionExpiredHandler(handler);

    fetchMock.mockResolvedValueOnce(new Response("unauthorized", { status: 401 }));

    const resp = await fetchWithAuth("/api/v1/builds");
    expect(resp.status).toBe(401);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
