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

  // Whether the Cognito user pool allows self-service sign-up. Off by
  // default: a self-hosted instance is typically reachable from the public
  // internet, and open sign-up there means anyone can obtain an account.
  // Opt in (COGNITO_ALLOW_SELF_SIGNUP=true) for a deployment that
  // deliberately offers open registration (e.g. a hosted trial).
  //
  // NOTE: this governs Cognito *native* self-registration only. It does not
  // by itself stop account creation via a federated IdP (e.g. Google) — see
  // infra/aws-cdk/README.md ("Restricting who can sign up").
  allowSelfSignUp: boolean;
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
    allowSelfSignUp: optionalBoolEnv("COGNITO_ALLOW_SELF_SIGNUP", false),
  };
}

// Export a singleton config instance
export const config = loadConfig();
