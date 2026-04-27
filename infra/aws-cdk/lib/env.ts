/**
 * Helpers for parsing CDK sizing knobs from environment variables.
 *
 * The naive `Number(process.env.X ?? default)` pattern silently produces
 * `NaN` on malformed input (e.g. `STARDAG_API_CPU=1o24`); that NaN then
 * propagates into FargateTaskDefinition / Aurora capacity / desiredCount
 * without complaint, and CDK synth emits `"NaN"` strings. These helpers
 * fail loudly so a deploy-time typo doesn't render the rendered template
 * useless.
 */

export function numEnv(key: string, defaultValue: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw === "") return defaultValue;
  const n = Number(raw);
  if (!Number.isFinite(n)) {
    throw new Error(
      `Environment variable ${key}=${JSON.stringify(raw)} is not a finite number`,
    );
  }
  return n;
}

export function intEnv(key: string, defaultValue: number): number {
  const n = numEnv(key, defaultValue);
  if (!Number.isInteger(n)) {
    throw new Error(
      `Environment variable ${key}=${process.env[key]} is not an integer`,
    );
  }
  return n;
}
