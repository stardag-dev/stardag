// Server version API (unauthenticated endpoint)
import { API_V1 } from "./config";

export interface ServerVersion {
  /**
   * Version of the combined server (API + UI) release. Clean `X.Y.Z` for
   * release images, `X.Y.Z+N.g<sha>` for builds past a release tag, and
   * `dev` when running from source.
   */
  server_version: string;
  /** Installed stardag-api package version. */
  api_version: string;
}

export async function fetchServerVersion(): Promise<ServerVersion> {
  const response = await fetch(`${API_V1}/version`);
  if (!response.ok) {
    throw new Error(`Failed to fetch server version: ${response.statusText}`);
  }
  return response.json();
}
