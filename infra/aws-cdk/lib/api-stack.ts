import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecs_patterns from "aws-cdk-lib/aws-ecs-patterns";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53_targets from "aws-cdk-lib/aws-route53-targets";
import { Construct } from "constructs";
import { StardagConfig } from "./config";
import { FoundationStack } from "./foundation-stack";

export interface ApiStackProps extends cdk.StackProps {
  config: StardagConfig;
  foundation: FoundationStack;
}

/**
 * API Stack - ECS Fargate service with ALB
 *
 * Depends on FoundationStack for:
 * - VPC
 * - Database credentials and security group
 * - Cognito (OIDC issuer)
 * - ECR Repository
 * - DNS/SSL certificates (optional)
 */
export class ApiStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs_patterns.ApplicationLoadBalancedFargateService;
  public readonly securityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const { config, foundation } = props;

    // =============================================================
    // ECS Cluster
    // =============================================================
    this.cluster = new ecs.Cluster(this, "Cluster", {
      vpc: foundation.vpc,
      clusterName: "stardag",
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // =============================================================
    // Security Group for API
    // =============================================================
    this.securityGroup = new ec2.SecurityGroup(this, "SecurityGroup", {
      vpc: foundation.vpc,
      description: "Security group for Stardag API",
      allowAllOutbound: true,
    });

    // Allow API to connect to database
    // Use CfnSecurityGroupIngress to avoid circular dependency between stacks
    new ec2.CfnSecurityGroupIngress(this, "DbIngress", {
      ipProtocol: "tcp",
      fromPort: foundation.dbPort,
      toPort: foundation.dbPort,
      groupId: foundation.dbSecurityGroup.securityGroupId,
      sourceSecurityGroupId: this.securityGroup.securityGroupId,
      description: "Allow API to connect to database",
    });

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
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Grant task role access to secrets
    foundation.dbServiceSecret.grantRead(taskDefinition.taskRole);
    foundation.dbAdminSecret.grantRead(taskDefinition.taskRole);

    // Add container
    taskDefinition.addContainer("Api", {
      // Use image from ECR repository
      image: ecs.ContainerImage.fromEcrRepository(foundation.ecrRepository, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "api",
      }),
      environment: {
        // Database config (credentials from secrets)
        STARDAG_API_DATABASE_HOST: foundation.dbClusterEndpoint,
        STARDAG_API_DATABASE_PORT: foundation.dbPort.toString(),
        STARDAG_API_DATABASE_NAME: foundation.dbName,
        // OIDC config
        OIDC_ISSUER_URL: foundation.cognitoIssuerUrl,
        OIDC_EXTERNAL_ISSUER_URL: foundation.cognitoIssuerUrl,
        OIDC_AUDIENCE: foundation.cognitoClientId,
        // CORS
        STARDAG_API_CORS_ORIGINS: `https://${config.uiDomain},http://localhost:3000,http://localhost:5173`,
      },
      secrets: {
        // Inject database credentials from Secrets Manager
        // Note: Using admin credentials since service user doesn't exist yet
        // TODO: Create service user via migration and switch to dbServiceSecret
        STARDAG_API_DATABASE_USER: ecs.Secret.fromSecretsManager(
          foundation.dbAdminSecret,
          "username",
        ),
        STARDAG_API_DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(
          foundation.dbAdminSecret,
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
        // Use Python since curl is not installed in python:3.11-slim
        command: [
          "CMD-SHELL",
          "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // =============================================================
    // Fargate Service with ALB
    // =============================================================
    const certificate = foundation.dns?.apiCertificate;

    this.service = new ecs_patterns.ApplicationLoadBalancedFargateService(
      this,
      "Service",
      {
        cluster: this.cluster,
        taskDefinition,
        desiredCount: 1,
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
        minHealthyPercent: 100,
        maxHealthyPercent: 200,

        // Circuit breaker for faster failure detection
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
    // DNS Record (if DNS is configured)
    // =============================================================
    if (foundation.dns) {
      new route53.ARecord(this, "ApiARecord", {
        zone: foundation.dns.hostedZone,
        recordName: config.apiDomain,
        target: route53.RecordTarget.fromAlias(
          new route53_targets.LoadBalancerTarget(this.service.loadBalancer),
        ),
        comment: "Stardag API",
      });
    }

    // =============================================================
    // Outputs
    // =============================================================
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

    new cdk.CfnOutput(this, "ApiUrl", {
      value: certificate
        ? `https://${config.apiDomain}`
        : `http://${this.service.loadBalancer.loadBalancerDnsName}`,
      description: "API endpoint URL",
      exportName: "StardagApiUrl",
    });
  }
}
