import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
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
