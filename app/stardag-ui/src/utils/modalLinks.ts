import type { ExecutorMetadata } from "../types/task";

// Modal dashboard deep links.
//
// ALL Modal URL patterns are centralized in THIS file — if Modal ever
// changes its dashboard URL format, this is the only place to update.
//
// Patterns (matching the "View Deployment" URL the Modal CLI prints on
// `modal deploy`):
//   app page:      https://modal.com/apps/{workspace}/{environment}/deployed/{app_name}
//   function call: app page + ?functionCallId={function_call_id}
//
// The environment segment falls back to Modal's default environment name
// ("main") when the metadata doesn't record one. Both builders return
// null when a required field is missing so callers never render dead
// links (graceful degradation for old servers / partial metadata).

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

// URL of the deployed Modal app's dashboard page, or null if the
// metadata doesn't identify a Modal app (missing workspace/app_name, or
// an explicitly non-modal kind).
export function modalAppUrl(
  metadata: ExecutorMetadata | null | undefined,
): string | null {
  if (!metadata) return null;
  // `kind` may be absent (older SDKs); only bail when it's explicitly
  // something other than "modal".
  if (metadata.kind !== undefined && metadata.kind !== "modal") return null;
  const workspace = nonEmptyString(metadata.workspace);
  const appName = nonEmptyString(metadata.app_name);
  if (!workspace || !appName) return null;
  const environment = nonEmptyString(metadata.environment) ?? "main";
  return `https://modal.com/apps/${encodeURIComponent(workspace)}/${encodeURIComponent(
    environment,
  )}/deployed/${encodeURIComponent(appName)}`;
}

// URL of a specific function call on the app's dashboard page, or null
// when either the app page can't be built or the call ref is missing.
export function modalFunctionCallUrl(
  metadata: ExecutorMetadata | null | undefined,
  functionCallId: string | null | undefined,
): string | null {
  const appUrl = modalAppUrl(metadata);
  if (!appUrl || !functionCallId) return null;
  return `${appUrl}?functionCallId=${encodeURIComponent(functionCallId)}`;
}
