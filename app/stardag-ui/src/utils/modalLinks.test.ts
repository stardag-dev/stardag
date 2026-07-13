import { describe, expect, it } from "vitest";
import { modalAppUrl, modalFunctionCallUrl } from "./modalLinks";

const fullMetadata = {
  kind: "modal",
  app_name: "my-app",
  workspace: "my-workspace",
  environment: "staging",
  function_name: "worker_default",
};

function withoutKey(key: keyof typeof fullMetadata): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...fullMetadata };
  delete copy[key];
  return copy;
}

describe("modalAppUrl", () => {
  it("builds the deployed-app URL from full metadata", () => {
    expect(modalAppUrl(fullMetadata)).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app",
    );
  });

  it("falls back to the 'main' environment when absent", () => {
    expect(modalAppUrl(withoutKey("environment"))).toBe(
      "https://modal.com/apps/my-workspace/main/deployed/my-app",
    );
  });

  it("accepts metadata without an explicit kind", () => {
    expect(modalAppUrl(withoutKey("kind"))).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app",
    );
  });

  it("returns null for a non-modal kind", () => {
    expect(modalAppUrl({ ...fullMetadata, kind: "k8s" })).toBeNull();
  });

  it("returns null when workspace is missing or empty", () => {
    expect(modalAppUrl(withoutKey("workspace"))).toBeNull();
    expect(modalAppUrl({ ...fullMetadata, workspace: "" })).toBeNull();
  });

  it("returns null when app_name is missing", () => {
    expect(modalAppUrl(withoutKey("app_name"))).toBeNull();
  });

  it("returns null for null/undefined metadata", () => {
    expect(modalAppUrl(null)).toBeNull();
    expect(modalAppUrl(undefined)).toBeNull();
  });

  it("URL-encodes path segments", () => {
    expect(
      modalAppUrl({
        kind: "modal",
        app_name: "my app/x",
        workspace: "ws#1",
        environment: "env 2",
      }),
    ).toBe("https://modal.com/apps/ws%231/env%202/deployed/my%20app%2Fx");
  });
});

describe("modalFunctionCallUrl", () => {
  it("appends the functionCallId query param to the app URL", () => {
    expect(modalFunctionCallUrl(fullMetadata, "fc-abc123")).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app?functionCallId=fc-abc123",
    );
  });

  it("returns null without a call ref", () => {
    expect(modalFunctionCallUrl(fullMetadata, null)).toBeNull();
    expect(modalFunctionCallUrl(fullMetadata, undefined)).toBeNull();
    expect(modalFunctionCallUrl(fullMetadata, "")).toBeNull();
  });

  it("returns null when the app URL cannot be built", () => {
    expect(modalFunctionCallUrl(null, "fc-abc123")).toBeNull();
    expect(modalFunctionCallUrl({ kind: "modal" }, "fc-abc123")).toBeNull();
  });

  it("URL-encodes the call ref", () => {
    expect(modalFunctionCallUrl(fullMetadata, "fc a&b")).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app?functionCallId=fc%20a%26b",
    );
  });
});
