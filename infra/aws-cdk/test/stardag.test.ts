import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ApiStack } from "../lib/api-stack";
import { FoundationStack } from "../lib/foundation-stack";
import { StardagStack } from "../lib/stardag-stack";
import { FrontendStack } from "../lib/frontend-stack";
import { StardagApi } from "../lib/constructs/api";

// Mock config for testing
const mockConfig = {
  awsAccountId: "123456789012",
  awsRegion: "us-east-1",
  awsProfile: "test",
  domainName: "example.com",
  apiSubdomain: "api",
  uiSubdomain: "app",
  apiDomain: "api.example.com",
  uiDomain: "app.example.com",
  googleClientId: "test-client-id.apps.googleusercontent.com",
  googleClientSecret: "test-client-secret",
  sesEnabled: false, // Opt-in feature, disabled by default
  allowSelfSignUp: false, // Secure default: no open self-registration
};

describe("StardagStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new StardagStack(app, "TestStack", {
      env: { account: mockConfig.awsAccountId, region: mockConfig.awsRegion },
      config: mockConfig,
    });
    template = Template.fromStack(stack);
  });

  // Phase 2: Networking & Database
  describe("Networking & Database", () => {
    test("creates VPC", () => {
      template.hasResourceProperties("AWS::EC2::VPC", {
        EnableDnsHostnames: true,
        EnableDnsSupport: true,
      });
    });

    test("creates NAT Gateway", () => {
      template.resourceCountIs("AWS::EC2::NatGateway", 1);
    });

    test("creates Aurora Serverless cluster", () => {
      template.hasResourceProperties("AWS::RDS::DBCluster", {
        Engine: "aurora-postgresql",
        DatabaseName: "stardag",
        ServerlessV2ScalingConfiguration: {
          MinCapacity: 0.5,
          MaxCapacity: 4,
        },
      });
    });

    test("creates secrets for database credentials", () => {
      template.hasResourceProperties("AWS::SecretsManager::Secret", {
        Name: "stardag/db/admin",
      });
      template.hasResourceProperties("AWS::SecretsManager::Secret", {
        Name: "stardag/db/service",
      });
    });
  });

  // Phase 3: Authentication
  describe("Authentication (Cognito)", () => {
    test("creates User Pool", () => {
      template.hasResourceProperties("AWS::Cognito::UserPool", {
        UserPoolName: "stardag-users",
        UsernameAttributes: ["email"],
        AutoVerifiedAttributes: ["email"],
      });
    });

    test("disables native self-signup by default (allowSelfSignUp=false)", () => {
      // selfSignUpEnabled:false synthesizes AllowAdminCreateUserOnly:true.
      // mockConfig has allowSelfSignUp:false, the secure default.
      template.hasResourceProperties("AWS::Cognito::UserPool", {
        AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
      });
    });

    test("creates User Pool Domain", () => {
      template.hasResourceProperties("AWS::Cognito::UserPoolDomain", {
        Domain: "stardag",
      });
    });

    test("creates User Pool Client with OAuth settings", () => {
      template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
        AllowedOAuthFlows: ["code"],
        AllowedOAuthScopes: ["openid", "email", "profile"],
        SupportedIdentityProviders: ["COGNITO", "Google"],
      });
    });

    test("creates Google Identity Provider", () => {
      template.hasResourceProperties("AWS::Cognito::UserPoolIdentityProvider", {
        ProviderName: "Google",
        ProviderType: "Google",
      });
    });
  });

  // Phase 4: API
  describe("API (ECS Fargate)", () => {
    test("creates ECR Repository", () => {
      template.hasResourceProperties("AWS::ECR::Repository", {
        RepositoryName: "stardag-api",
      });
    });

    test("creates ECS Cluster", () => {
      template.hasResourceProperties("AWS::ECS::Cluster", {
        ClusterName: "stardag",
      });
    });

    test("creates ECS Service", () => {
      template.resourceCountIs("AWS::ECS::Service", 1);
    });

    test("creates Application Load Balancer", () => {
      template.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 1);
    });

    test("creates Fargate Task Definition", () => {
      template.hasResourceProperties("AWS::ECS::TaskDefinition", {
        Cpu: "256",
        Memory: "512",
        NetworkMode: "awsvpc",
        RequiresCompatibilities: ["FARGATE"],
      });
    });
  });

  // Phase 5: Frontend
  describe("Frontend (S3 + CloudFront)", () => {
    test("creates S3 bucket with versioning", () => {
      template.hasResourceProperties("AWS::S3::Bucket", {
        VersioningConfiguration: {
          Status: "Enabled",
        },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });

    test("creates CloudFront distribution", () => {
      template.resourceCountIs("AWS::CloudFront::Distribution", 1);
    });

    test("creates CloudFront Origin Access Control", () => {
      template.hasResourceProperties("AWS::CloudFront::OriginAccessControl", {
        OriginAccessControlConfig: {
          OriginAccessControlOriginType: "s3",
          SigningBehavior: "always",
          SigningProtocol: "sigv4",
        },
      });
    });
  });

  // Phase 6: DNS & SSL
  describe("DNS & SSL (skipped for example.com)", () => {
    // Note: DNS resources (Route 53, ACM Certificates) are only created
    // when a real domain is configured (not example.com).
    // This is because Route 53 HostedZone.fromLookup requires AWS
    // credentials at synth time. DNS resources are tested via
    // `AWS_PROFILE=stardag npx cdk synth` with real credentials.

    test("does not create Route 53 records for example.com domain", () => {
      // Route 53 records are NOT created for test domain
      template.resourceCountIs("AWS::Route53::RecordSet", 0);
    });

    test("does not create ACM certificates for example.com domain", () => {
      // ACM certificates are NOT created for test domain
      template.resourceCountIs("AWS::CertificateManager::Certificate", 0);
    });
  });

  // Stack outputs
  describe("Stack Outputs", () => {
    test("outputs API and UI URLs", () => {
      template.hasOutput("ApiUrl", {
        Value: "https://api.example.com",
      });
      template.hasOutput("UiUrl", {
        Value: "https://app.example.com",
      });
    });
  });
});

// ---------------------------------------------------------------------------
// ApiStack — split-stack production path. Covers the optional JWT private
// key secret wiring (STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME).
// ---------------------------------------------------------------------------

function synthApiStack(jwtSecretName: string | undefined): Template {
  const original = process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME;
  if (jwtSecretName === undefined) {
    delete process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME;
  } else {
    process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME = jwtSecretName;
  }
  try {
    const app = new cdk.App();
    const env = {
      account: mockConfig.awsAccountId,
      region: mockConfig.awsRegion,
    };
    const foundation = new FoundationStack(app, "Foundation", {
      env,
      config: mockConfig,
    });
    const apiStack = new ApiStack(app, "Api", {
      env,
      config: mockConfig,
      foundation,
    });
    return Template.fromStack(apiStack);
  } finally {
    if (original === undefined) {
      delete process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME;
    } else {
      process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME = original;
    }
  }
}

describe("ApiStack JWT_PRIVATE_KEY secret wiring", () => {
  describe("when STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME is unset", () => {
    let template: Template;

    beforeAll(() => {
      template = synthApiStack(undefined);
    });

    test("does not mount JWT_PRIVATE_KEY in the container secrets block", () => {
      // No container in the API task def references JWT_PRIVATE_KEY.
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const apiTaskDef = Object.values(taskDefs).find(
        (td) =>
          td.Properties.ContainerDefinitions?.some(
            (c: { Name?: string }) => c.Name === "Api",
          ),
      );
      expect(apiTaskDef).toBeDefined();
      const secrets = apiTaskDef!.Properties.ContainerDefinitions[0].Secrets ?? [];
      const names = secrets.map((s: { Name: string }) => s.Name);
      expect(names).not.toContain("JWT_PRIVATE_KEY");
    });
  });

  describe("when STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME is set", () => {
    let template: Template;
    const secretName = "stardag/api/jwt-private-key";

    beforeAll(() => {
      template = synthApiStack(secretName);
    });

    test("mounts JWT_PRIVATE_KEY from Secrets Manager :private_key field", () => {
      // ``ValueFrom`` is a CFN ``Fn::Join`` that builds the secret ARN at
      // deploy time (``arn:aws:secretsmanager:<region>:<account>:secret:<name>:private_key::``).
      // Match by serialising the template and asserting the assembled
      // string substring is present — robust against the nested join form.
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const apiTaskDef = Object.values(taskDefs).find(
        (td) =>
          td.Properties.ContainerDefinitions?.some(
            (c: { Name?: string }) => c.Name === "Api",
          ),
      );
      expect(apiTaskDef).toBeDefined();
      const containers = apiTaskDef!.Properties.ContainerDefinitions as Array<{
        Name: string;
        Secrets?: Array<{ Name: string; ValueFrom: unknown }>;
      }>;
      const apiContainer = containers.find((c) => c.Name === "Api")!;
      const jwtSecret = apiContainer.Secrets?.find((s) => s.Name === "JWT_PRIVATE_KEY");
      expect(jwtSecret).toBeDefined();
      // ValueFrom is { "Fn::Join": ["", [...parts...]] }; flatten to a
      // string of all string parts and assert the secret name + private_key
      // suffix appear.
      const valueFromStr = JSON.stringify(jwtSecret!.ValueFrom);
      expect(valueFromStr).toContain(secretName);
      expect(valueFromStr).toContain(":private_key::");
    });

    test("execution role IAM policy allows GetSecretValue on the JWT secret", () => {
      // The execution role's policy must include GetSecretValue (the
      // ECS agent uses the execution role at task launch to fetch
      // secrets — granting only the task role would fail to launch).
      // Each ``AWS::IAM::Policy`` resource is attached to one or more
      // roles; find any policy whose statements grant GetSecretValue on
      // a resource ARN containing our secret name.
      const policies = template.findResources("AWS::IAM::Policy");
      let found = false;
      for (const p of Object.values(policies)) {
        const stmts = p.Properties.PolicyDocument.Statement as Array<{
          Action: string | string[];
          Effect: string;
          Resource: unknown;
        }>;
        for (const stmt of stmts) {
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          if (
            stmt.Effect === "Allow" &&
            actions.includes("secretsmanager:GetSecretValue") &&
            JSON.stringify(stmt.Resource).includes(secretName)
          ) {
            // Confirm at least one attached role has "ExecutionRole" in
            // its logical ID — that's the role ECS uses at task start.
            const roles = (p.Properties.Roles ?? []) as Array<{
              Ref?: string;
            }>;
            const execRoleAttached = roles.some(
              (r) => r.Ref?.includes("ExecutionRole"),
            );
            if (execRoleAttached) {
              found = true;
              break;
            }
          }
        }
        if (found) break;
      }
      expect(found).toBe(true);
    });
  });

  describe("when STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME is whitespace", () => {
    test("treats whitespace as unset (no JWT_PRIVATE_KEY in template)", () => {
      const template = synthApiStack("   ");
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const apiTaskDef = Object.values(taskDefs).find(
        (td) =>
          td.Properties.ContainerDefinitions?.some(
            (c: { Name?: string }) => c.Name === "Api",
          ),
      );
      const secrets = apiTaskDef?.Properties.ContainerDefinitions[0].Secrets ?? [];
      const names = secrets.map((s: { Name: string }) => s.Name);
      expect(names).not.toContain("JWT_PRIVATE_KEY");
    });
  });
});

// ---------------------------------------------------------------------------
// ApiStack — optional explicit container image (prebuilt public release
// image, e.g. ghcr.io/stardag-dev/stardag-server:X.Y.Z).
// ---------------------------------------------------------------------------

function synthApiStackWithImage(apiImageUri?: string): Template {
  const app = new cdk.App();
  const env = {
    account: mockConfig.awsAccountId,
    region: mockConfig.awsRegion,
  };
  const foundation = new FoundationStack(app, "Foundation", {
    env,
    config: mockConfig,
  });
  const apiStack = new ApiStack(app, "Api", {
    env,
    config: mockConfig,
    foundation,
    apiImageUri,
  });
  return Template.fromStack(apiStack);
}

function getApiContainerImage(template: Template): unknown {
  const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
  const apiTaskDef = Object.values(taskDefs).find(
    (td) =>
      td.Properties.ContainerDefinitions?.some(
        (c: { Name?: string }) => c.Name === "Api",
      ),
  );
  expect(apiTaskDef).toBeDefined();
  return apiTaskDef!.Properties.ContainerDefinitions[0].Image;
}

describe("ApiStack explicit image URI (apiImageUri)", () => {
  test("defaults to the Foundation ECR repository image", () => {
    const image = getApiContainerImage(synthApiStackWithImage(undefined));
    // The ECR image URI is assembled from the imported repository name
    // (an Fn::Join over the ECR ARN export) — assert it is NOT a plain
    // public registry string and references the ECR repository ARN export.
    expect(typeof image).not.toBe("string");
    expect(JSON.stringify(image)).toContain("dkr.ecr");
  });

  test("uses the literal image URI when provided", () => {
    const uri = "ghcr.io/stardag-dev/stardag-server:0.1.0";
    const image = getApiContainerImage(synthApiStackWithImage(uri));
    expect(image).toBe(uri);
  });

  test("treats a whitespace-only value as unset", () => {
    const image = getApiContainerImage(synthApiStackWithImage("   "));
    expect(typeof image).not.toBe("string");
  });
});

// ---------------------------------------------------------------------------
// ApiStack — runtime UI OIDC configuration. Prebuilt UI dists resolve their
// client id and Cognito logout domain at runtime from GET /api/v1/auth/config,
// which serves OIDC_UI_CLIENT_ID and OIDC_COGNITO_DOMAIN. Without these env
// vars the API would serve its defaults (client id "stardag-ui", no Cognito
// domain) and Cognito authorize/logout would break for runtime-config UIs.
// ---------------------------------------------------------------------------

describe("ApiStack runtime-UI OIDC environment", () => {
  let environment: Array<{ Name: string; Value: unknown }>;

  beforeAll(() => {
    const template = synthApiStackWithImage(undefined);
    const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
    const apiTaskDef = Object.values(taskDefs).find(
      (td) =>
        td.Properties.ContainerDefinitions?.some(
          (c: { Name?: string }) => c.Name === "Api",
        ),
    );
    expect(apiTaskDef).toBeDefined();
    environment = apiTaskDef!.Properties.ContainerDefinitions[0].Environment;
  });

  function envValue(name: string): unknown {
    const entry = environment.find((e) => e.Name === name);
    expect(entry).toBeDefined();
    return entry!.Value;
  }

  test("sets OIDC_UI_CLIENT_ID to the same Cognito client as the SDK", () => {
    // The Foundation stack creates a single user-pool client shared by UI
    // and SDK; both env vars must reference the same (imported) client id.
    expect(envValue("OIDC_UI_CLIENT_ID")).toEqual(envValue("OIDC_SDK_CLIENT_ID"));
  });

  test("sets OIDC_COGNITO_DOMAIN to the bare hosted-UI host (no scheme)", () => {
    // The UI builds https://{cognito_domain}/logout, so the value must be
    // the bare host.
    expect(envValue("OIDC_COGNITO_DOMAIN")).toBe(
      `stardag.auth.${mockConfig.awsRegion}.amazoncognito.com`,
    );
  });
});

// ---------------------------------------------------------------------------
// StardagApi construct — imageUri normalization. The reusable construct must
// be safe on its own (independent of ApiStack's env/context normalization):
// a whitespace-only imageUri prop is treated as unset, and surrounding
// whitespace on a real URI is trimmed.
// ---------------------------------------------------------------------------

function synthStardagApiConstruct(imageUri?: string): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "ConstructTest", {
    env: { account: mockConfig.awsAccountId, region: mockConfig.awsRegion },
  });
  const vpc = new ec2.Vpc(stack, "Vpc", { maxAzs: 2 });
  new StardagApi(stack, "Api", {
    vpc,
    dbClusterEndpoint: "db.example.internal",
    dbPort: 5432,
    dbName: "stardag",
    dbServiceSecret: new secretsmanager.Secret(stack, "ServiceSecret"),
    dbAdminSecret: new secretsmanager.Secret(stack, "AdminSecret"),
    dbSecurityGroup: new ec2.SecurityGroup(stack, "DbSg", { vpc }),
    oidcIssuerUrl: "https://issuer.example.com",
    oidcAudience: "test-audience",
    apiDomain: "api.example.com",
    uiDomain: "app.example.com",
    imageUri,
  });
  return Template.fromStack(stack);
}

describe("StardagApi construct imageUri normalization", () => {
  test("treats a whitespace-only imageUri prop as unset", () => {
    const image = getApiContainerImage(synthStardagApiConstruct("   "));
    // Falls back to the construct's own ECR repository (a token, not a
    // plain string), rather than fromRegistry("   ").
    expect(typeof image).not.toBe("string");
    expect(JSON.stringify(image)).toContain("dkr.ecr");
  });

  test("trims surrounding whitespace from a real imageUri", () => {
    const uri = "ghcr.io/stardag-dev/stardag-server:0.1.0";
    const image = getApiContainerImage(synthStardagApiConstruct(` ${uri}\n`));
    expect(image).toBe(uri);
  });
});

// ---------------------------------------------------------------------------
// FrontendStack — optional same-origin API proxy (uiApiProxy). Required for
// prebuilt UI dists, which resolve their config at runtime from
// /api/v1/auth/config on their own origin.
// ---------------------------------------------------------------------------

// A config with a non-example.com domain so FoundationStack creates the
// DNS construct (HostedZone.fromLookup returns a dummy zone during tests).
const dnsConfig = {
  ...mockConfig,
  domainName: "stardag-cdk-test.dev",
  apiDomain: "api.stardag-cdk-test.dev",
  uiDomain: "app.stardag-cdk-test.dev",
};

function synthFrontendStack(config: typeof mockConfig, apiProxy: boolean): Template {
  const app = new cdk.App();
  const env = {
    account: mockConfig.awsAccountId,
    region: mockConfig.awsRegion,
  };
  const foundation = new FoundationStack(app, "Foundation", { env, config });
  const frontend = new FrontendStack(app, "Frontend", {
    env,
    config,
    foundation,
    apiProxy,
  });
  return Template.fromStack(frontend);
}

function getDistributionConfig(template: Template): any {
  const distributions = template.findResources("AWS::CloudFront::Distribution");
  const values = Object.values(distributions);
  expect(values).toHaveLength(1);
  return values[0].Properties.DistributionConfig;
}

describe("FrontendStack same-origin API proxy (apiProxy)", () => {
  describe("default (disabled)", () => {
    let template: Template;

    beforeAll(() => {
      template = synthFrontendStack(dnsConfig, false);
    });

    test("keeps SPA routing via 403/404 error responses", () => {
      const config = getDistributionConfig(template);
      const codes = (config.CustomErrorResponses ?? []).map(
        (r: { ErrorCode: number }) => r.ErrorCode,
      );
      expect(codes).toEqual(expect.arrayContaining([403, 404]));
    });

    test("has no additional cache behaviors and no CloudFront function", () => {
      const config = getDistributionConfig(template);
      expect(config.CacheBehaviors ?? []).toHaveLength(0);
      template.resourceCountIs("AWS::CloudFront::Function", 0);
    });
  });

  describe("enabled", () => {
    let template: Template;

    beforeAll(() => {
      template = synthFrontendStack(dnsConfig, true);
    });

    test("routes /api/*, /health and /.well-known/* to the API domain", () => {
      const config = getDistributionConfig(template);
      const behaviors = config.CacheBehaviors as Array<{
        PathPattern: string;
        TargetOriginId: string;
        CachePolicyId: string;
        AllowedMethods: string[];
      }>;
      const patterns = behaviors.map((b) => b.PathPattern);
      expect(patterns).toEqual(
        expect.arrayContaining(["/api/*", "/health", "/.well-known/*"]),
      );

      // All API behaviors share the custom-domain HTTPS origin
      const apiBehavior = behaviors.find((b) => b.PathPattern === "/api/*")!;
      const origins = config.Origins as Array<{
        Id: string;
        DomainName: string;
        CustomOriginConfig?: { OriginProtocolPolicy: string };
      }>;
      const apiOrigin = origins.find((o) => o.Id === apiBehavior.TargetOriginId)!;
      expect(apiOrigin.DomainName).toBe(dnsConfig.apiDomain);
      expect(apiOrigin.CustomOriginConfig?.OriginProtocolPolicy).toBe("https-only");

      // Caching disabled (managed policy id), all methods allowed
      expect(apiBehavior.CachePolicyId).toBe("4135ea2d-6df8-44a3-9df3-4b5a84be39ad");
      expect(apiBehavior.AllowedMethods).toEqual(
        expect.arrayContaining(["GET", "POST", "PUT", "DELETE"]),
      );
    });

    test("switches SPA routing to a viewer-request function (no error responses)", () => {
      const config = getDistributionConfig(template);
      expect(config.CustomErrorResponses ?? []).toHaveLength(0);
      template.resourceCountIs("AWS::CloudFront::Function", 1);
      const defaultAssociations = config.DefaultCacheBehavior
        .FunctionAssociations as Array<{
        EventType: string;
      }>;
      expect(defaultAssociations).toHaveLength(1);
      expect(defaultAssociations[0].EventType).toBe("viewer-request");
    });

    describe("SPA rewrite function behavior", () => {
      // Extract the inline CloudFront Function code from the template and
      // execute it directly. The function is plain (CloudFront JS 2.0
      // compatible) JavaScript, so evaluating it in Node exercises the
      // real rewrite logic rather than string-matching the source.
      let rewrite: (uri: string) => string;

      beforeAll(() => {
        const fns = template.findResources("AWS::CloudFront::Function");
        const values = Object.values(fns);
        expect(values).toHaveLength(1);
        const code = values[0].Properties.FunctionCode as string;
        // eslint-disable-next-line @typescript-eslint/no-implied-eval
        const handler = new Function(`${code}; return handler;`)() as (event: {
          request: { uri: string };
        }) => { uri: string };
        rewrite = (uri: string) => handler({ request: { uri } }).uri;
      });

      test("rewrites SPA routes (including dotted path params) to /index.html", () => {
        expect(rewrite("/")).toBe("/index.html");
        expect(rewrite("/workspaces/acme/builds")).toBe("/index.html");
        // A future dotted route param must reach the SPA, not surface a
        // raw S3 XML error (proxy mode has no distribution-wide error
        // responses to catch the miss).
        expect(rewrite("/tasks/v1.2.3")).toBe("/index.html");
        expect(rewrite("/builds/some.dotted.id/details")).toBe("/index.html");
      });

      test("serves static assets as-is", () => {
        expect(rewrite("/index.html")).toBe("/index.html");
        expect(rewrite("/assets/index-B3xQ9z.js")).toBe("/assets/index-B3xQ9z.js");
        expect(rewrite("/assets/index-C4yR1w.css")).toBe("/assets/index-C4yR1w.css");
        expect(rewrite("/favicon.svg")).toBe("/favicon.svg");
        expect(rewrite("/logo.svg")).toBe("/logo.svg");
        expect(rewrite("/robots.txt")).toBe("/robots.txt");
      });
    });
  });

  test("throws when enabled without DNS (example.com config)", () => {
    expect(() => synthFrontendStack(mockConfig, true)).toThrow(
      /uiApiProxy requires DNS/,
    );
  });
});
