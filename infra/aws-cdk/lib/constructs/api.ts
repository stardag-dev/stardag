import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecs_patterns from "aws-cdk-lib/aws-ecs-patterns";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { Construct } from "constructs";

export interface StardagApiProps {
  /**
   * VPC to deploy the API in
   */
  vpc: ec2.IVpc;

  /**
   * Database cluster endpoint
   */
  dbClusterEndpoint: string;

  /**
   * Database port
   */
  dbPort: number;

  /**
   * Database name
   */
  dbName: string;

  /**
   * Secret containing service database credentials
   */
  dbServiceSecret: secretsmanager.ISecret;

  /**
   * Secret containing admin database credentials (for migrations)
   */
  dbAdminSecret: secretsmanager.ISecret;

  /**
   * Security group of the database (to allow connections)
   */
  dbSecurityGroup: ec2.ISecurityGroup;

  /**
   * OIDC Issuer URL (Cognito)
   */
  oidcIssuerUrl: string;

  /**
   * OIDC Audience (Client ID)
   */
  oidcAudience: string;

  /**
   * API domain (e.g., api.stardag.com)
   */
  apiDomain: string;

  /**
   * UI domain (for CORS)
   */
  uiDomain: string;

  /**
   * CPU units for the container
   * @default 256 (0.25 vCPU)
   */
  cpu?: number;

  /**
   * Memory in MB for the container
   * @default 512
   */
  memoryLimitMiB?: number;

  /**
   * Desired count of tasks
   * @default 1
   */
  desiredCount?: number;

  /**
   * ACM Certificate for HTTPS (optional)
   * If provided, enables HTTPS on the ALB
   */
  certificate?: acm.ICertificate;
}

export class StardagApi extends Construct {
  public readonly repository: ecr.Repository;
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs_patterns.ApplicationLoadBalancedFargateService;
  public readonly securityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: StardagApiProps) {
    super(scope, id);

    const {
      vpc,
      dbClusterEndpoint,
      dbPort,
      dbName,
      dbServiceSecret,
      dbAdminSecret,
      dbSecurityGroup,
      oidcIssuerUrl,
      oidcAudience,
      apiDomain,
      uiDomain,
      cpu = 256,
      memoryLimitMiB = 512,
      desiredCount = 1,
      certificate,
    } = props;

    // =============================================================
    // ECR Repository
    // =============================================================
    this.repository = new ecr.Repository(this, "Repository", {
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

    // =============================================================
    // ECS Cluster
    // =============================================================
    this.cluster = new ecs.Cluster(this, "Cluster", {
      vpc,
      clusterName: "stardag",
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // =============================================================
    // Security Group for API
    // =============================================================
    this.securityGroup = new ec2.SecurityGroup(this, "SecurityGroup", {
      vpc,
      description: "Security group for Stardag API",
      allowAllOutbound: true,
    });

    // Allow API to connect to database
    dbSecurityGroup.addIngressRule(
      this.securityGroup,
      ec2.Port.tcp(dbPort),
      "Allow API to connect to database",
    );

    // =============================================================
    // CloudWatch Log Group
    // =============================================================
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: "/stardag/api",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // =============================================================
    // Task Definition
    // =============================================================
    const taskDefinition = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu,
      memoryLimitMiB,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Grant task role access to secrets
    dbServiceSecret.grantRead(taskDefinition.taskRole);
    dbAdminSecret.grantRead(taskDefinition.taskRole);

    // Add container
    const container = taskDefinition.addContainer("Api", {
      // Use image from ECR repository
      image: ecs.ContainerImage.fromEcrRepository(this.repository, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "api",
      }),
      environment: {
        // Database config (credentials from secrets)
        STARDAG_API_DATABASE_HOST: dbClusterEndpoint,
        STARDAG_API_DATABASE_PORT: dbPort.toString(),
        STARDAG_API_DATABASE_NAME: dbName,
        // OIDC config
        OIDC_ISSUER_URL: oidcIssuerUrl,
        OIDC_EXTERNAL_ISSUER_URL: oidcIssuerUrl,
        OIDC_AUDIENCE: oidcAudience,
        // CORS
        STARDAG_API_CORS_ORIGINS: `https://${uiDomain},http://localhost:3000,http://localhost:5173`,
      },
      secrets: {
        // Inject database credentials from Secrets Manager
        STARDAG_API_DATABASE_USER: ecs.Secret.fromSecretsManager(
          dbServiceSecret,
          "username",
        ),
        STARDAG_API_DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(
          dbServiceSecret,
          "password",
        ),
      },
      portMappings: [
        {
          containerPort: 8000,
          protocol: ecs.Protocol.TCP,
        },
      ],
      healthCheck: {
        command: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // =============================================================
    // Fargate Service with ALB
    // =============================================================
    this.service = new ecs_patterns.ApplicationLoadBalancedFargateService(
      this,
      "Service",
      {
        cluster: this.cluster,
        taskDefinition,
        desiredCount,
        serviceName: "stardag-api",

        // Networking
        assignPublicIp: false,
        taskSubnets: {
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
        securityGroups: [this.securityGroup],

        // Load balancer settings
        publicLoadBalancer: true,
        ...(certificate
          ? {
              // HTTPS configuration
              certificate,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              redirectHTTP: true,
            }
          : {
              // HTTP only (no certificate)
              listenerPort: 80,
            }),

        // Health check
        healthCheckGracePeriod: cdk.Duration.seconds(120),

        // Deployment configuration
        minHealthyPercent: 100, // Keep all tasks running during deployments
        maxHealthyPercent: 200, // Allow double tasks temporarily

        // Disable circuit breaker rollback so initial deployment completes
        // even with placeholder image failing health checks
        circuitBreaker: {
          enable: true,
          rollback: false,
        },
      },
    );

    // Configure ALB health check
    this.service.targetGroup.configureHealthCheck({
      path: "/health",
      healthyHttpCodes: "200",
      interval: cdk.Duration.seconds(30),
      timeout: cdk.Duration.seconds(5),
      healthyThresholdCount: 2,
      unhealthyThresholdCount: 3,
    });

    // =============================================================
    // Auto Scaling
    // =============================================================
    const scaling = this.service.service.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: 4,
    });

    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // =============================================================
    // Outputs
    // =============================================================
    new cdk.CfnOutput(this, "RepositoryUri", {
      value: this.repository.repositoryUri,
      description: "ECR Repository URI (for docker push)",
      exportName: "StardagApiRepositoryUri",
    });

    new cdk.CfnOutput(this, "ClusterName", {
      value: this.cluster.clusterName,
      description: "ECS Cluster name",
      exportName: "StardagApiClusterName",
    });

    new cdk.CfnOutput(this, "ServiceName", {
      value: this.service.service.serviceName,
      description: "ECS Service name",
      exportName: "StardagApiServiceName",
    });

    new cdk.CfnOutput(this, "LoadBalancerDns", {
      value: this.service.loadBalancer.loadBalancerDnsName,
      description: "ALB DNS name (temporary, before custom domain)",
      exportName: "StardagApiLoadBalancerDns",
    });
  }
}
