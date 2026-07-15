import type { ExecutorMetadata } from "../types/task";

// Modal dashboard deep links.
//
// ALL Modal URL patterns are centralized in THIS file — if Modal ever
// changes its dashboard URL format, this is the only place to update.
// Modal gives NO forward-compatibility guarantee for these URLs, so every
// deep link here is best-effort resilience, not a contract.
//
// Patterns:
//   app page (stable / stop+redeploy-proof — addresses the app by its id):
//     https://modal.com/apps/{workspace}/{environment}/{app_id}
//   app page (deployed-name form — only resolves while that app version is
//   live, but survives when we lack the app id):
//     https://modal.com/apps/{workspace}/{environment}/deployed/{app_name}
//   function call (stable / stop+redeploy-proof — addresses the app by its
//   id, so it keeps resolving after the app version is stopped/redeployed):
//     https://modal.com/apps/{workspace}/{environment}/{app_id}?activeTab=functions&functionId={function_id}&functionSection=calls&fcId={fc}
//   function call (deployed-name form — only resolves while that app
//   version is live, but survives when we lack the app id):
//     https://modal.com/apps/{workspace}/{environment}/deployed/{app_name}?activeTab=functions&functionId={function_id}&functionSection=calls&fcId={fc}
//
// The environment segment falls back to Modal's default environment name
// ("main") when the metadata doesn't record one. Builders return null when
// a required field is missing so callers never render dead links (graceful
// degradation for old servers / partial metadata — `app_id`/`function_id`
// are absent on data recorded before the SDK started capturing them).

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

// True unless the metadata explicitly records a non-modal executor kind.
// `kind` may be absent on older SDKs, so we only bail when it's present and
// something other than "modal".
export function isModalMetadata(metadata: ExecutorMetadata): boolean {
  return metadata.kind === undefined || metadata.kind === "modal";
}

// URL of the Modal app's dashboard page, or null if the metadata doesn't
// identify a Modal app (missing workspace + app id/name, or an explicitly
// non-modal kind).
//
// Prefers the stable app-id form (survives an app stop/redeploy) when
// `app_id` is present, and degrades to the deployed-name form (resolves
// only while that app version is live) for older data that lacks it.
export function modalAppUrl(
  metadata: ExecutorMetadata | null | undefined,
): string | null {
  if (!metadata) return null;
  if (!isModalMetadata(metadata)) return null;
  const workspace = nonEmptyString(metadata.workspace);
  if (!workspace) return null;
  const environment = nonEmptyString(metadata.environment) ?? "main";
  const appId = nonEmptyString(metadata.app_id);
  const appName = nonEmptyString(metadata.app_name);
  const base = `https://modal.com/apps/${encodeURIComponent(
    workspace,
  )}/${encodeURIComponent(environment)}`;
  if (appId) {
    return `${base}/${encodeURIComponent(appId)}`;
  }
  if (appName) {
    return `${base}/deployed/${encodeURIComponent(appName)}`;
  }
  return null;
}

// URL of the Modal workspace+environment apps page, or null when either the
// workspace or the environment is missing (or the executor is non-modal).
// Unlike modalAppUrl this does NOT fall back to the "main" environment: the
// breadcrumb segment only links when an environment was actually recorded.
//
// There is deliberately no workspace-only builder: a bare
// `modal.com/apps/{workspace}` resolves to whichever environment was "latest
// active", which is misleading — the coarsest addressable level is the
// workspace+environment pair.
export function modalEnvironmentUrl(
  metadata: ExecutorMetadata | null | undefined,
): string | null {
  if (!metadata || !isModalMetadata(metadata)) return null;
  const workspace = nonEmptyString(metadata.workspace);
  const environment = nonEmptyString(metadata.environment);
  if (!workspace || !environment) return null;
  return `https://modal.com/apps/${encodeURIComponent(workspace)}/${encodeURIComponent(
    environment,
  )}`;
}

// URL scoped to a specific Modal function within its app dashboard, or null
// when there's no function_id or no resolvable app URL. Reuses modalAppUrl
// for the app-addressing part (stable app-id form when available, else the
// deployed-name form), then narrows to the function via query params.
export function modalFunctionUrl(
  metadata: ExecutorMetadata | null | undefined,
): string | null {
  if (!metadata) return null;
  const functionId = nonEmptyString(metadata.function_id);
  if (!functionId) return null;
  const appUrl = modalAppUrl(metadata);
  if (!appUrl) return null;
  return `${appUrl}?activeTab=functions&functionId=${encodeURIComponent(functionId)}`;
}

// Shared query string for the function-call deep links (both the app-id and
// the deployed-name forms use the same params).
function functionCallQuery(functionId: string, fcId: string): string {
  return (
    `?activeTab=functions&functionId=${encodeURIComponent(functionId)}` +
    `&functionSection=calls&fcId=${encodeURIComponent(fcId)}`
  );
}

// URL pointing at a specific function call in the Modal dashboard, or null
// when nothing usable can be built.
//
// Prefers the most resilient form we have enough identifiers for:
//   1. workspace + app_id + function_id + fcId → stable app-id URL
//      (survives an app stop/redeploy).
//   2. workspace + app_name + function_id + fcId → deployed-name URL
//      (resolves only while that app version is live).
//   3. otherwise → the plain app-page link (stable app-id form when we
//      have app_id but no function_id, else the deployed-name form, else
//      null), so old/partial data still gets the most resilient link the
//      identifiers allow rather than a dead one.
export function modalFunctionCallUrl(
  metadata: ExecutorMetadata | null | undefined,
  functionCallId: string | null | undefined,
): string | null {
  if (!metadata || !isModalMetadata(metadata)) return null;

  const workspace = nonEmptyString(metadata.workspace);
  const environment = nonEmptyString(metadata.environment) ?? "main";
  const appId = nonEmptyString(metadata.app_id);
  const appName = nonEmptyString(metadata.app_name);
  const functionId = nonEmptyString(metadata.function_id);
  const fcId = nonEmptyString(functionCallId);

  if (workspace && functionId && fcId) {
    const base = `https://modal.com/apps/${encodeURIComponent(
      workspace,
    )}/${encodeURIComponent(environment)}`;
    if (appId) {
      return `${base}/${encodeURIComponent(appId)}${functionCallQuery(
        functionId,
        fcId,
      )}`;
    }
    if (appName) {
      return `${base}/deployed/${encodeURIComponent(appName)}${functionCallQuery(
        functionId,
        fcId,
      )}`;
    }
  }

  // Not enough to address the call itself — fall back to the app page (or
  // null). Never append the old `?functionCallId=` param: that URL doesn't
  // resolve in the Modal dashboard.
  return modalAppUrl(metadata);
}
