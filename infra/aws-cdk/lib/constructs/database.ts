import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface StardagDatabaseProps {
  /**
   * VPC to deploy the database in
   */
  vpc: ec2.IVpc;

  /**
   * Database name
   * @default "stardag"
   */
  databaseName?: string;

  /**
   * Minimum ACU for Aurora Serverless v2
   * @default 0.5 (minimum possible)
   */
  minCapacity?: number;

  /**
   * Maximum ACU for Aurora Serverless v2
   * @default 4
   */
  maxCapacity?: number;
}

export class StardagDatabase extends Construct {
  public readonly cluster: rds.DatabaseCluster;
  public readonly adminSecret: secretsmanager.ISecret;
  public readonly serviceSecret: secretsmanager.ISecret;
  public readonly securityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: StardagDatabaseProps) {
    super(scope, id);

    const { vpc, databaseName = "stardag", minCapacity = 0.5, maxCapacity = 4 } = props;

    // Security group for the database
    this.securityGroup = new ec2.SecurityGroup(this, "DbSecurityGroup", {
      vpc,
      description: "Security group for Stardag Aurora database",
      allowAllOutbound: false,
    });

    // Create the admin credentials secret (used for migrations)
    this.adminSecret = new secretsmanager.Secret(this, "AdminSecret", {
      secretName: "stardag/db/admin",
      description: "Stardag database admin credentials",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: "stardag_admin" }),
        generateStringKey: "password",
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    // Create the service credentials secret (used by the API at runtime)
    this.serviceSecret = new secretsmanager.Secret(this, "ServiceSecret", {
      secretName: "stardag/db/service",
      description: "Stardag database service credentials",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: "stardag_service" }),
        generateStringKey: "password",
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    // Create Aurora Serverless v2 cluster
    this.cluster = new rds.DatabaseCluster(this, "Cluster", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_4,
      }),

      credentials: rds.Credentials.fromSecret(this.adminSecret),
      defaultDatabaseName: databaseName,

      serverlessV2MinCapacity: minCapacity,
      serverlessV2MaxCapacity: maxCapacity,

      writer: rds.ClusterInstance.serverlessV2("Writer", {
        publiclyAccessible: false,
      }),

      vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
      },
      securityGroups: [this.securityGroup],

      // Backup and maintenance
      backup: {
        retention: cdk.Duration.days(7),
      },

      // Deletion protection (disable for dev if needed)
      deletionProtection: false, // Set to true for production
      removalPolicy: cdk.RemovalPolicy.SNAPSHOT,

      // Storage encryption
      storageEncrypted: true,
    });

    // Outputs
    new cdk.CfnOutput(this, "ClusterEndpoint", {
      value: this.cluster.clusterEndpoint.hostname,
      description: "Aurora cluster endpoint",
    });

    new cdk.CfnOutput(this, "ClusterPort", {
      value: this.cluster.clusterEndpoint.port.toString(),
      description: "Aurora cluster port",
    });

    new cdk.CfnOutput(this, "AdminSecretArn", {
      value: this.adminSecret.secretArn,
      description: "Admin credentials secret ARN (for migrations)",
    });

    new cdk.CfnOutput(this, "ServiceSecretArn", {
      value: this.serviceSecret.secretArn,
      description: "Service credentials secret ARN (for API runtime)",
    });
  }

  /**
   * Allow inbound connections from a security group
   */
  allowFrom(securityGroup: ec2.ISecurityGroup, description: string): void {
    this.securityGroup.addIngressRule(securityGroup, ec2.Port.tcp(5432), description);
  }
}
