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
import { intEnv } from "./env";
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

    // Sizing knobs that ops may want to tune at deploy time without
    // editing this file. Defaults match the OSS-friendly free-tier shape;
    // production overrides come from the environment. Each helper throws
    // on malformed input rather than silently emitting NaN into the
    // rendered template.
    const apiCpu = intEnv("STARDAG_API_CPU", 256);
    const apiMemoryMiB = intEnv("STARDAG_API_MEMORY_MIB", 512);
    const apiDesiredCount = intEnv("STARDAG_API_DESIRED_COUNT", 1);
    // Autoscale floor defaults to desiredCount so ops who only set
    // DESIRED_COUNT get the historical behaviour. Override with
    // STARDAG_API_AUTOSCALE_MIN to let the service scale below the
    // initial desired count.
    const apiAutoscaleMin = intEnv("STARDAG_API_AUTOSCALE_MIN", apiDesiredCount);
    const apiAutoscaleMax = intEnv("STARDAG_API_AUTOSCALE_MAX", 4);
    // Optional JWT signing key, fetched from Secrets Manager. When set, the
    // API uses a stable RSA keypair across deploys so cached internal
    // tokens (UI sessions) survive container rollover. When unset (default
    // for OSS / fresh deploys), the API generates an ephemeral keypair on
    // every container start — fine for single-deploy use but invalidates
    // every existing UI session on every redeploy.
    // The secret is expected to be a JSON document with a "private_key"
    // field containing a PEM-encoded RSA private key.
    const jwtPrivateKeySecretName = process.env.STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME;
    if (apiAutoscaleMax < apiAutoscaleMin) {
      throw new Error(
        `STARDAG_API_AUTOSCALE_MAX (${apiAutoscaleMax}) must be >= ` +
          `STARDAG_API_AUTOSCALE_MIN (${apiAutoscaleMin})`,
      );
    }
    const apiGunicornWorkers = process.env.STARDAG_API_GUNICORN_WORKERS;
    if (apiGunicornWorkers !== undefined) {
      const trimmed = apiGunicornWorkers.trim();
      const parsed = Number.parseInt(trimmed, 10);
      if (
        trimmed.length === 0 ||
        !Number.isInteger(parsed) ||
        parsed < 1 ||
        parsed.toString() !== trimmed
      ) {
        throw new Error(
          `STARDAG_API_GUNICORN_WORKERS=${JSON.stringify(apiGunicornWorkers)} ` +
            `must be a positive integer`,
        );
      }
    }

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
      cpu: apiCpu,
      memoryLimitMiB: apiMemoryMiB,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Grant task role access to secrets
    foundation.dbServiceSecret.grantRead(taskDefinition.taskRole);
    foundation.dbAdminSecret.grantRead(taskDefinition.taskRole);

    // Look up the optional JWT private key secret so its ARN is in the
    // task definition. The secret value must already exist in Secrets
    // Manager when the task starts; ECS fetches it at container launch.
    const jwtPrivateKeySecret = jwtPrivateKeySecretName
      ? secretsmanager.Secret.fromSecretNameV2(
          this,
          "JwtPrivateKeySecret",
          jwtPrivateKeySecretName,
        )
      : undefined;
    if (jwtPrivateKeySecret) {
      jwtPrivateKeySecret.grantRead(taskDefinition.taskRole);
    }

    // Grant task role permission to send emails via SES
    if (foundation.ses) {
      foundation.ses.grantSendEmail(taskDefinition.taskRole);
    }

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
        // OIDC config - Cognito uses different JWKS path than Keycloak
        OIDC_ISSUER_URL: foundation.cognitoIssuerUrl,
        OIDC_EXTERNAL_ISSUER_URL: foundation.cognitoIssuerUrl,
        OIDC_AUDIENCE: foundation.cognitoClientId,
        // SDK client ID (same Cognito client used for both UI and SDK)
        OIDC_SDK_CLIENT_ID: foundation.cognitoClientId,
        // Cognito JWKS URL format: {issuer}/.well-known/jwks.json
        // (different from Keycloak which uses /protocol/openid-connect/certs)
        OIDC_JWKS_URL: `${foundation.cognitoIssuerUrl}/.well-known/jwks.json`,
        // CORS
        STARDAG_API_CORS_ORIGINS: `https://${config.uiDomain},http://localhost:3000,http://localhost:5173`,
        // Email configuration (SES)
        EMAIL_ENABLED: foundation.ses ? "true" : "false",
        EMAIL_FROM_ADDRESS: `noreply@${config.domainName}`,
        EMAIL_FROM_NAME: "Stardag",
        EMAIL_SES_REGION: this.region,
        EMAIL_APP_URL: `https://${config.uiDomain}`,
        // SaaS guardrails (rate limits and entity creation limits)
        LIMITS_MAX_TASK_DATA_BYTES: "102400",
        LIMITS_MAX_ASSET_BODY_BYTES: "1048576",
        LIMITS_MAX_REQUESTS_PER_MINUTE: "300",
        LIMITS_MAX_BUILDS_PER_WORKSPACE_24H: "200",
        LIMITS_MAX_TASKS_PER_WORKSPACE_24H: "10000",
        LIMITS_MAX_EVENTS_PER_WORKSPACE_24H: "50000",
        LIMITS_MAX_ASSETS_PER_WORKSPACE_24H: "1000",
        LIMITS_MAX_DEPENDENCY_IDS_PER_TASK: "500",
        LIMITS_MAX_ASSETS_PER_TASK: "10",
        // Worker count is read by the Dockerfile CMD; keep undefined here
        // to fall through to the image's default (sized for 1 vCPU).
        ...(apiGunicornWorkers ? { GUNICORN_WORKERS: apiGunicornWorkers } : {}),
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
        // JWT_PRIVATE_KEY mounted from Secrets Manager when configured.
        // Picked up by config.JWTSettings.private_key (env_prefix=JWT_).
        // The whole RSA-2048 PEM lives under the "private_key" JSON key in
        // the secret value. See cloud/docs for one-time provisioning.
        ...(jwtPrivateKeySecret
          ? {
              JWT_PRIVATE_KEY: ecs.Secret.fromSecretsManager(
                jwtPrivateKeySecret,
                "private_key",
              ),
            }
          : {}),
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
        desiredCount: apiDesiredCount,
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
      minCapacity: apiAutoscaleMin,
      maxCapacity: apiAutoscaleMax,
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
