import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import { StardagConfig } from "./config";
import { StardagVpc } from "./constructs/vpc";
import { StardagDatabase } from "./constructs/database";
import { StardagCognito } from "./constructs/cognito";
import { StardagDns } from "./constructs/dns";

export interface FoundationStackProps extends cdk.StackProps {
  config: StardagConfig;
}

/**
 * Foundation Stack - Core infrastructure that must be deployed first
 *
 * Contains:
 * - VPC with public/private/isolated subnets
 * - Aurora Serverless PostgreSQL database
 * - Cognito User Pool for authentication
 * - ECR Repository for API container images
 * - DNS & SSL certificates (if custom domain configured)
 */
export class FoundationStack extends cdk.Stack {
  // Networking
  public readonly vpc: ec2.IVpc;

  // Database
  public readonly dbClusterEndpoint: string;
  public readonly dbPort: number;
  public readonly dbName: string;
  public readonly dbServiceSecret: secretsmanager.ISecret;
  public readonly dbAdminSecret: secretsmanager.ISecret;
  public readonly dbSecurityGroup: ec2.ISecurityGroup;

  // Authentication
  public readonly cognitoIssuerUrl: string;
  public readonly cognitoClientId: string;
  public readonly cognitoDomain: string;

  // Container Registry
  public readonly ecrRepository: ecr.IRepository;

  // DNS (optional, only if custom domain configured)
  public readonly dns?: StardagDns;

  constructor(scope: Construct, id: string, props: FoundationStackProps) {
    super(scope, id, props);

    const { config } = props;

    // =============================================================
    // VPC
    // =============================================================
    const vpcConstruct = new StardagVpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
    });
    this.vpc = vpcConstruct.vpc;

    // =============================================================
    // Database
    // =============================================================
    const database = new StardagDatabase(this, "Database", {
      vpc: this.vpc,
      databaseName: "stardag",
      minCapacity: 0.5,
      maxCapacity: 4,
    });

    this.dbClusterEndpoint = database.cluster.clusterEndpoint.hostname;
    this.dbPort = database.cluster.clusterEndpoint.port;
    this.dbName = "stardag";
    this.dbServiceSecret = database.serviceSecret;
    this.dbAdminSecret = database.adminSecret;
    this.dbSecurityGroup = database.securityGroup;

    // =============================================================
    // Cognito
    // =============================================================
    const cognito = new StardagCognito(this, "Cognito", {
      uiDomain: config.uiDomain,
      githubClientId: config.githubClientId,
      githubClientSecret: config.githubClientSecret,
      googleClientId: config.googleClientId,
      googleClientSecret: config.googleClientSecret,
      domainPrefix: "stardag",
    });

    this.cognitoIssuerUrl = cognito.issuerUrl;
    this.cognitoClientId = cognito.userPoolClient.userPoolClientId;
    this.cognitoDomain = `stardag.auth.${this.region}.amazoncognito.com`;

    // =============================================================
    // ECR Repository
    // =============================================================
    const repository = new ecr.Repository(this, "ApiRepository", {
      repositoryName: "stardag-api",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        {
          maxImageCount: 10,
          description: "Keep only 10 most recent images",
        },
      ],
    });
    this.ecrRepository = repository;

    // =============================================================
    // DNS & Certificates (only for real domains)
    // =============================================================
    if (config.domainName && !config.domainName.includes("example.com")) {
      this.dns = new StardagDns(this, "Dns", {
        domainName: config.domainName,
        apiSubdomain: config.apiSubdomain,
        uiSubdomain: config.uiSubdomain,
      });
    }

    // =============================================================
    // Outputs (Note: Cognito construct already exports its own outputs)
    // =============================================================
    new cdk.CfnOutput(this, "EcrRepositoryUri", {
      value: repository.repositoryUri,
      description: "ECR Repository URI - push API image here",
      exportName: "StardagEcrRepositoryUri",
    });

    new cdk.CfnOutput(this, "DatabaseEndpoint", {
      value: this.dbClusterEndpoint,
      description: "Database cluster endpoint",
      exportName: "StardagDatabaseEndpoint",
    });
  }
}
