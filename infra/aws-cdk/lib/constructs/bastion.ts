import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export interface StardagBastionProps {
  /**
   * VPC to deploy the bastion in
   */
  vpc: ec2.IVpc;

  /**
   * Instance type for the bastion
   * @default t3.micro
   */
  instanceType?: ec2.InstanceType;
}

/**
 * Bastion host for secure database access
 *
 * Uses AWS Systems Manager Session Manager for access - no SSH keys needed.
 * Connect via: aws ssm start-session --target <instance-id>
 */
export class StardagBastion extends Construct {
  public readonly instance: ec2.BastionHostLinux;
  public readonly securityGroup: ec2.ISecurityGroup;

  constructor(scope: Construct, id: string, props: StardagBastionProps) {
    super(scope, id);

    const { vpc, instanceType = ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO) } = props;

    // Create bastion host in public subnet
    // Uses Amazon Linux 2023 by default (via cdk.json feature flag)
    this.instance = new ec2.BastionHostLinux(this, "Host", {
      vpc,
      instanceType,
      subnetSelection: {
        subnetType: ec2.SubnetType.PUBLIC,
      },
      machineImage: ec2.MachineImage.latestAmazonLinux2023(),
      instanceName: "stardag-bastion",
    });

    this.securityGroup = this.instance.connections.securityGroups[0];

    // Add SSM managed policy for Session Manager access
    this.instance.instance.role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore"),
    );

    // Install PostgreSQL client via user data
    this.instance.instance.addUserData(
      "dnf install -y postgresql16",
      'echo "PostgreSQL client installed"',
    );

    // Output instance ID for easy access
    new cdk.CfnOutput(this, "InstanceId", {
      value: this.instance.instanceId,
      description: "Bastion instance ID - use with: aws ssm start-session --target <id>",
      exportName: "StardagBastionInstanceId",
    });
  }
}
