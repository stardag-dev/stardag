import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import { Construct } from "constructs";

export interface StardagCognitoProps {
  /**
   * Domain name for the application (used for callbacks)
   */
  uiDomain: string;

  /**
   * GitHub OAuth credentials
   */
  githubClientId: string;
  githubClientSecret: string;

  /**
   * Optional: Google OAuth credentials
   */
  googleClientId?: string;
  googleClientSecret?: string;

  /**
   * Cognito domain prefix (must be globally unique)
   * @default "stardag"
   */
  domainPrefix?: string;
}

export class StardagCognito extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain: cognito.UserPoolDomain;
  public readonly githubIdp: cognito.UserPoolIdentityProviderOidc;

  constructor(scope: Construct, id: string, props: StardagCognitoProps) {
    super(scope, id);

    const {
      uiDomain,
      githubClientId,
      githubClientSecret,
      domainPrefix = "stardag",
    } = props;

    // =============================================================
    // User Pool
    // =============================================================
    this.userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: "stardag-users",

      // Self sign-up enabled (users can register)
      selfSignUpEnabled: true,

      // Sign-in options
      signInAliases: {
        email: true,
        username: false,
      },

      // Auto-verify email (required for password recovery)
      autoVerify: {
        email: true,
      },

      // Standard attributes
      standardAttributes: {
        email: {
          required: true,
          mutable: true,
        },
        fullname: {
          required: false,
          mutable: true,
        },
      },

      // Password policy
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },

      // Account recovery
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,

      // MFA (optional for users)
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: {
        sms: false,
        otp: true,
      },

      // Deletion protection
      deletionProtection: false, // Set to true for production

      // Remove users when stack is destroyed (for dev)
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // =============================================================
    // Cognito Domain (for Hosted UI)
    // =============================================================
    this.userPoolDomain = this.userPool.addDomain("Domain", {
      cognitoDomain: {
        domainPrefix: domainPrefix,
      },
    });

    // =============================================================
    // GitHub Identity Provider (OIDC)
    // =============================================================
    this.githubIdp = new cognito.UserPoolIdentityProviderOidc(this, "GitHubIdp", {
      userPool: this.userPool,
      name: "GitHub",

      // GitHub OAuth endpoints
      clientId: githubClientId,
      clientSecret: githubClientSecret,
      issuerUrl: "https://github.com", // Note: GitHub doesn't have standard OIDC discovery

      // GitHub uses custom endpoints (not standard OIDC discovery)
      endpoints: {
        authorization: "https://github.com/login/oauth/authorize",
        token: "https://github.com/login/oauth/access_token",
        userInfo: "https://api.github.com/user",
        jwksUri: "https://token.actions.githubusercontent.com/.well-known/jwks", // Not used for OAuth but required
      },

      // Scopes to request from GitHub
      scopes: ["openid", "user:email", "read:user"],

      // Attribute mapping from GitHub to Cognito
      attributeMapping: {
        email: cognito.ProviderAttribute.other("email"),
        fullname: cognito.ProviderAttribute.other("name"),
        profilePicture: cognito.ProviderAttribute.other("avatar_url"),
        preferredUsername: cognito.ProviderAttribute.other("login"),
      },
    });

    // =============================================================
    // User Pool Client (for UI app)
    // =============================================================
    this.userPoolClient = this.userPool.addClient("UiClient", {
      userPoolClientName: "stardag-ui",

      // Generate secret (false for public clients like SPAs)
      generateSecret: false,

      // Auth flows
      authFlows: {
        userPassword: true,
        userSrp: true,
      },

      // OAuth settings
      oAuth: {
        flows: {
          authorizationCodeGrant: true,
          implicitCodeGrant: false,
        },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: [
          `https://${uiDomain}/callback`,
          // Local development
          "http://localhost:3000/callback",
          "http://localhost:5173/callback",
        ],
        logoutUrls: [
          `https://${uiDomain}`,
          // Local development
          "http://localhost:3000",
          "http://localhost:5173",
        ],
      },

      // Token validity
      accessTokenValidity: cdk.Duration.hours(24),
      idTokenValidity: cdk.Duration.hours(24),
      refreshTokenValidity: cdk.Duration.days(30),

      // Prevent user existence errors
      preventUserExistenceErrors: true,

      // Supported identity providers
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
        cognito.UserPoolClientIdentityProvider.custom("GitHub"),
      ],
    });

    // Ensure IdP is created before the client references it
    this.userPoolClient.node.addDependency(this.githubIdp);

    // =============================================================
    // Outputs
    // =============================================================
    const region = cdk.Stack.of(this).region;

    new cdk.CfnOutput(this, "UserPoolId", {
      value: this.userPool.userPoolId,
      description: "Cognito User Pool ID",
      exportName: "StardagCognitoUserPoolId",
    });

    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: this.userPoolClient.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName: "StardagCognitoClientId",
    });

    new cdk.CfnOutput(this, "CognitoDomain", {
      value: `${domainPrefix}.auth.${region}.amazoncognito.com`,
      description: "Cognito Hosted UI domain",
      exportName: "StardagCognitoDomain",
    });

    new cdk.CfnOutput(this, "OidcIssuerUrl", {
      value: `https://cognito-idp.${region}.amazonaws.com/${this.userPool.userPoolId}`,
      description: "OIDC Issuer URL (for API JWT validation)",
      exportName: "StardagOidcIssuerUrl",
    });

    new cdk.CfnOutput(this, "GitHubCallbackUrl", {
      value: `https://${domainPrefix}.auth.${region}.amazoncognito.com/oauth2/idpresponse`,
      description: "GitHub OAuth callback URL (update in GitHub OAuth App settings)",
      exportName: "StardagGitHubCallbackUrl",
    });
  }

  /**
   * Get the OIDC issuer URL for this User Pool
   */
  get issuerUrl(): string {
    const region = cdk.Stack.of(this).region;
    return `https://cognito-idp.${region}.amazonaws.com/${this.userPool.userPoolId}`;
  }

  /**
   * Get the hosted UI sign-in URL
   */
  getSignInUrl(redirectUri: string): string {
    const region = cdk.Stack.of(this).region;
    const domain = this.userPoolDomain.domainName;
    const clientId = this.userPoolClient.userPoolClientId;
    return `https://${domain}.auth.${region}.amazoncognito.com/login?client_id=${clientId}&response_type=code&scope=openid+email+profile&redirect_uri=${encodeURIComponent(
      redirectUri,
    )}`;
  }
}
