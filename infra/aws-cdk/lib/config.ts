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

function optionalBoolEnv(name: string, defaultValue: boolean): boolean {
  const value = process.env[name];
  if (value === undefined) return defaultValue;
  return value.toLowerCase() === "true" || value === "1";
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

  // Google OAuth
  googleClientId: string;
  googleClientSecret: string;

  // Optional features
  sesEnabled: boolean;
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

    // Google OAuth
    googleClientId: requireEnv("GOOGLE_CLIENT_ID"),
    googleClientSecret: requireEnv("GOOGLE_CLIENT_SECRET"),

    // Optional features (opt-in)
    sesEnabled: optionalBoolEnv("SES_ENABLED", false),
  };
}

// Export a singleton config instance
export const config = loadConfig();
