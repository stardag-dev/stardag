import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ApiStack } from "../lib/api-stack";
import { FoundationStack } from "../lib/foundation-stack";
import { StardagStack } from "../lib/stardag-stack";

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
