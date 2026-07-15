import { describe, expect, it } from "vitest";
import {
  modalAppUrl,
  modalEnvironmentUrl,
  modalFunctionCallUrl,
  modalFunctionUrl,
} from "./modalLinks";

const fullMetadata = {
  kind: "modal",
  app_name: "my-app",
  workspace: "my-workspace",
  environment: "staging",
  function_name: "worker_default",
  app_id: "ap-123",
  function_id: "fu-456",
};

function withoutKey(key: keyof typeof fullMetadata): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...fullMetadata };
  delete copy[key];
  return copy;
}

describe("modalAppUrl", () => {
  it("prefers the stable app-id URL when app_id is present", () => {
    // app-id path form → survives an app stop/redeploy.
    expect(modalAppUrl(fullMetadata)).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
  });

  it("falls back to the deployed-name URL when app_id is missing", () => {
    // Resolves only while the app version is live, but survives without app_id.
    expect(modalAppUrl(withoutKey("app_id"))).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app",
    );
  });

  it("still builds the stable URL when app_name is missing", () => {
    // app_id alone is enough to address the app page.
    expect(modalAppUrl(withoutKey("app_name"))).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
  });

  it("falls back to the 'main' environment when absent", () => {
    expect(modalAppUrl(withoutKey("environment"))).toBe(
      "https://modal.com/apps/my-workspace/main/ap-123",
    );
  });

  it("accepts metadata without an explicit kind", () => {
    expect(modalAppUrl(withoutKey("kind"))).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
  });

  it("returns null for a non-modal kind", () => {
    expect(modalAppUrl({ ...fullMetadata, kind: "k8s" })).toBeNull();
  });

  it("returns null when workspace is missing or empty", () => {
    expect(modalAppUrl(withoutKey("workspace"))).toBeNull();
    expect(modalAppUrl({ ...fullMetadata, workspace: "" })).toBeNull();
  });

  it("returns null when neither app_id nor app_name is present", () => {
    expect(modalAppUrl({ kind: "modal", workspace: "my-workspace" })).toBeNull();
  });

  it("returns null for null/undefined metadata", () => {
    expect(modalAppUrl(null)).toBeNull();
    expect(modalAppUrl(undefined)).toBeNull();
  });

  it("URL-encodes the app-id path segment", () => {
    expect(
      modalAppUrl({
        kind: "modal",
        app_id: "ap 1/2",
        workspace: "ws#1",
        environment: "env 2",
      }),
    ).toBe("https://modal.com/apps/ws%231/env%202/ap%201%2F2");
  });

  it("URL-encodes the deployed-name path segments", () => {
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

describe("modalEnvironmentUrl", () => {
  it("builds the workspace+environment apps URL", () => {
    expect(modalEnvironmentUrl(fullMetadata)).toBe(
      "https://modal.com/apps/my-workspace/staging",
    );
  });

  it("returns null when workspace is missing", () => {
    expect(modalEnvironmentUrl(withoutKey("workspace"))).toBeNull();
  });

  it("returns null when environment is missing (no 'main' fallback here)", () => {
    // Unlike modalAppUrl, the breadcrumb env segment only links when an
    // environment was actually recorded.
    expect(modalEnvironmentUrl(withoutKey("environment"))).toBeNull();
  });

  it("returns null for a non-modal kind", () => {
    expect(modalEnvironmentUrl({ ...fullMetadata, kind: "k8s" })).toBeNull();
  });

  it("URL-encodes both segments", () => {
    expect(
      modalEnvironmentUrl({
        kind: "modal",
        workspace: "ws#1",
        environment: "env 2/a",
      }),
    ).toBe("https://modal.com/apps/ws%231/env%202%2Fa");
  });
});

describe("modalFunctionUrl", () => {
  it("builds the stable app-id URL scoped to the function", () => {
    expect(modalFunctionUrl(fullMetadata)).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123" +
        "?activeTab=functions&functionId=fu-456",
    );
  });

  it("falls back to the deployed-name app URL when app_id is missing", () => {
    expect(modalFunctionUrl(withoutKey("app_id"))).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app" +
        "?activeTab=functions&functionId=fu-456",
    );
  });

  it("returns null when function_id is missing", () => {
    expect(modalFunctionUrl(withoutKey("function_id"))).toBeNull();
  });

  it("returns null when no app URL can be built (no workspace)", () => {
    expect(modalFunctionUrl(withoutKey("workspace"))).toBeNull();
  });

  it("returns null for a non-modal kind", () => {
    // modalAppUrl bails on non-modal kinds, so no app URL → null.
    expect(modalFunctionUrl({ ...fullMetadata, kind: "k8s" })).toBeNull();
  });

  it("URL-encodes the function id query param", () => {
    expect(
      modalFunctionUrl({
        kind: "modal",
        workspace: "ws#1",
        environment: "env 2",
        app_id: "ap-1",
        function_id: "fu 3&4",
      }),
    ).toBe(
      "https://modal.com/apps/ws%231/env%202/ap-1" +
        "?activeTab=functions&functionId=fu%203%264",
    );
  });
});

describe("modalFunctionCallUrl", () => {
  it("builds the stable app-id URL when all ids are present", () => {
    // app-id path form → survives an app stop/redeploy.
    expect(modalFunctionCallUrl(fullMetadata, "fc-789")).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123" +
        "?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-789",
    );
  });

  it("falls back to the 'main' environment in the stable URL", () => {
    expect(modalFunctionCallUrl(withoutKey("environment"), "fc-789")).toBe(
      "https://modal.com/apps/my-workspace/main/ap-123" +
        "?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-789",
    );
  });

  it("falls back to the deployed-name form when app_id is missing", () => {
    // Resolves only while the app version is live, but survives without app_id.
    expect(modalFunctionCallUrl(withoutKey("app_id"), "fc-789")).toBe(
      "https://modal.com/apps/my-workspace/staging/deployed/my-app" +
        "?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-789",
    );
  });

  it("falls back to the stable app-id page when function_id is missing", () => {
    // No function_id → can't address the call, but app_id still yields the
    // stop/redeploy-proof app page rather than the deployed-name form.
    expect(modalFunctionCallUrl(withoutKey("function_id"), "fc-789")).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
  });

  it("falls back to the deployed-name page when neither function_id nor app_id is present", () => {
    expect(
      modalFunctionCallUrl(
        {
          kind: "modal",
          app_name: "my-app",
          workspace: "my-workspace",
          environment: "staging",
        },
        "fc-789",
      ),
    ).toBe("https://modal.com/apps/my-workspace/staging/deployed/my-app");
  });

  it("falls back to the stable app-id page when the call ref is missing", () => {
    expect(modalFunctionCallUrl(fullMetadata, null)).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
    expect(modalFunctionCallUrl(fullMetadata, "")).toBe(
      "https://modal.com/apps/my-workspace/staging/ap-123",
    );
  });

  it("returns null when nothing usable can be built", () => {
    expect(modalFunctionCallUrl(null, "fc-789")).toBeNull();
    expect(modalFunctionCallUrl(undefined, "fc-789")).toBeNull();
    // Only ids, no workspace/app_name → neither the call URL nor the app
    // page can be built.
    expect(
      modalFunctionCallUrl(
        { kind: "modal", app_id: "ap-123", function_id: "fu-456" },
        "fc-789",
      ),
    ).toBeNull();
  });

  it("returns null for a non-modal kind", () => {
    expect(modalFunctionCallUrl({ ...fullMetadata, kind: "k8s" }, "fc-789")).toBeNull();
  });

  it("URL-encodes ids and the call ref", () => {
    expect(
      modalFunctionCallUrl(
        {
          kind: "modal",
          workspace: "ws#1",
          environment: "env 2",
          app_id: "ap 1/2",
          function_id: "fu 3&4",
        },
        "fc a&b",
      ),
    ).toBe(
      "https://modal.com/apps/ws%231/env%202/ap%201%2F2" +
        "?activeTab=functions&functionId=fu%203%264&functionSection=calls&fcId=fc%20a%26b",
    );
  });
});
