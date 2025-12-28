import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { StardagConfig } from "./config";
import { StardagVpc } from "./constructs/vpc";
import { StardagDatabase } from "./constructs/database";
import { StardagCognito } from "./constructs/cognito";
import { StardagApi } from "./constructs/api";
import { StardagFrontend } from "./constructs/frontend";
import { StardagDns } from "./constructs/dns";

export interface StardagStackProps extends cdk.StackProps {
  config: StardagConfig;
}

export class StardagStack extends cdk.Stack {
  public readonly vpc: StardagVpc;
  public readonly database: StardagDatabase;
  public readonly cognito: StardagCognito;
  public readonly api: StardagApi;
  public readonly frontend: StardagFrontend;
  public readonly dns?: StardagDns;

  constructor(scope: Construct, id: string, props: StardagStackProps) {
    super(scope, id, props);

    const { config } = props;

    // =============================================================
    // Phase 2: Networking & Database
    // =============================================================

    // VPC with public/private subnets
    this.vpc = new StardagVpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1, // Cost optimization for MVP
    });

    // Aurora Serverless v2 PostgreSQL
    this.database = new StardagDatabase(this, "Database", {
      vpc: this.vpc.vpc,
      databaseName: "stardag",
      minCapacity: 0.5, // Minimum ACU
      maxCapacity: 4, // Scale up to 4 ACU under load
    });

    // =============================================================
    // Phase 3: Authentication - Cognito
    // =============================================================

    this.cognito = new StardagCognito(this, "Cognito", {
      uiDomain: config.uiDomain,
      githubClientId: config.githubClientId,
      githubClientSecret: config.githubClientSecret,
      googleClientId: config.googleClientId,
      googleClientSecret: config.googleClientSecret,
      domainPrefix: "stardag", // Must be globally unique
    });

    // =============================================================
    // Phase 6: DNS & SSL (before API/Frontend to get certificates)
    // =============================================================

    // Only create DNS resources if a real domain is configured
    if (config.domainName && !config.domainName.includes("example.com")) {
      this.dns = new StardagDns(this, "Dns", {
        domainName: config.domainName,
        apiSubdomain: config.apiSubdomain,
        uiSubdomain: config.uiSubdomain,
      });
    }

    // =============================================================
    // Phase 4: API - ECS Fargate
    // =============================================================

    this.api = new StardagApi(this, "Api", {
      vpc: this.vpc.vpc,
      dbClusterEndpoint: this.database.cluster.clusterEndpoint.hostname,
      dbPort: this.database.cluster.clusterEndpoint.port,
      dbName: "stardag",
      dbServiceSecret: this.database.serviceSecret,
      dbAdminSecret: this.database.adminSecret,
      dbSecurityGroup: this.database.securityGroup,
      oidcIssuerUrl: this.cognito.issuerUrl,
      oidcAudience: this.cognito.userPoolClient.userPoolClientId,
      apiDomain: config.apiDomain,
      uiDomain: config.uiDomain,
      cpu: 256, // 0.25 vCPU
      memoryLimitMiB: 512,
      desiredCount: 1,
      certificate: this.dns?.apiCertificate,
    });

    // =============================================================
    // Phase 5: Frontend - S3 + CloudFront
    // =============================================================

    this.frontend = new StardagFrontend(this, "Frontend", {
      uiDomain: config.uiDomain,
      certificate: this.dns?.uiCertificate,
    });

    // =============================================================
    // DNS Records (after API and Frontend are created)
    // =============================================================

    if (this.dns) {
      // Create Route 53 A records pointing to ALB and CloudFront
      this.dns.createApiRecord(this.api.service.loadBalancer);
      this.dns.createUiRecord(this.frontend.distribution);
    }

    // =============================================================
    // Stack Outputs
    // =============================================================
    new cdk.CfnOutput(this, "ApiUrl", {
      value: `https://${config.apiDomain}`,
      description: "API endpoint URL",
    });

    new cdk.CfnOutput(this, "UiUrl", {
      value: `https://${config.uiDomain}`,
      description: "UI application URL",
    });
  }
}
