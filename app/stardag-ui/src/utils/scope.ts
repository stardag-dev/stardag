/**
 * The server's synthetic per-build scope is exactly `build:<uuid>` — the
 * build's own id. Nothing else is synthetic: a real scope is
 * `<code_id>:<config_hash>`, and a code id may legitimately be the word
 * `build` (`STARDAG_CODE_ID=build` gives `build:ffffffffffffffff`), so a
 * prefix test would mislabel it. Mirrors `is_synthetic_scope` in the SDK.
 */
const SYNTHETIC_SCOPE_RE =
  /^build:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isSyntheticScope(scopeKey: string | null | undefined): boolean {
  return !!scopeKey && SYNTHETIC_SCOPE_RE.test(scopeKey);
}
