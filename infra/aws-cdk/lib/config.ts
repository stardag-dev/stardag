import * as dotenv from "dotenv";
import * as path from "path";

// Load environment variables from .env.deploy
dotenv.config({ path: path.join(__dirname, "..", ".env.deploy") });

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required environment variable: ${name}. ` +
        `Please ensure it's set in infra/aws-cdk/.env.deploy`,
    );
  }
  return value;
}

function optionalEnv(name: string, defaultValue: string): string {
  return process.env[name] ?? defaultValue;
}

export interface StardagConfig {
  // AWS
  awsAccountId: string;
  awsRegion: string;
  awsProfile: string;

  // Domain
  domainName: string;
  apiSubdomain: string;
  uiSubdomain: string;

  // Derived URLs
  apiDomain: string;
  uiDomain: string;

  // GitHub OAuth
  githubClientId: string;
  githubClientSecret: string;

  // Optional: Google OAuth
  googleClientId?: string;
  googleClientSecret?: string;
}

export function loadConfig(): StardagConfig {
  const domainName = requireEnv("DOMAIN_NAME");
  const apiSubdomain = optionalEnv("API_SUBDOMAIN", "api");
  const uiSubdomain = optionalEnv("UI_SUBDOMAIN", "app");

  return {
    // AWS
    awsAccountId: requireEnv("AWS_ACCOUNT_ID"),
    awsRegion: optionalEnv("AWS_REGION", "us-east-1"),
    awsProfile: optionalEnv("AWS_PROFILE", "default"),

    // Domain
    domainName,
    apiSubdomain,
    uiSubdomain,

    // Derived
    apiDomain: `${apiSubdomain}.${domainName}`,
    uiDomain: `${uiSubdomain}.${domainName}`,

    // GitHub OAuth
    githubClientId: requireEnv("GITHUB_CLIENT_ID"),
    githubClientSecret: requireEnv("GITHUB_CLIENT_SECRET"),

    // Optional: Google OAuth
    googleClientId: process.env.GOOGLE_CLIENT_ID,
    googleClientSecret: process.env.GOOGLE_CLIENT_SECRET,
  };
}

// Export a singleton config instance
export const config = loadConfig();
