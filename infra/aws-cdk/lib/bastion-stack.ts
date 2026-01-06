import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { StardagBastion } from "./constructs/bastion";

export interface BastionStackProps extends cdk.StackProps {
  /**
   * VPC to deploy the bastion in
   */
  vpc: ec2.IVpc;

  /**
   * Database endpoint for connection string
   */
  dbEndpoint: string;

  /**
   * Database security group ID (for manual setup instructions)
   */
  dbSecurityGroupId: string;
}

/**
 * Bastion Stack - Optional EC2 bastion host for database access
 *
 * This stack is deployed separately and can be destroyed when not needed
 * to save costs. Uses SSM Session Manager for secure access (no SSH keys).
 *
 * After deployment, you must add an ingress rule to the DB security group
 * to allow the bastion to connect. This is output by the stack.
 */
export class BastionStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BastionStackProps) {
    super(scope, id, props);

    const { vpc, dbEndpoint, dbSecurityGroupId } = props;

    // Create bastion host
    const bastion = new StardagBastion(this, "Bastion", {
      vpc,
    });

    // Output connection instructions
    new cdk.CfnOutput(this, "Step1_AllowDBAccess", {
      value: `aws ec2 authorize-security-group-ingress --group-id ${dbSecurityGroupId} --protocol tcp --port 5432 --source-group ${bastion.securityGroup.securityGroupId}`,
      description: "Run this command to allow bastion to access database",
    });

    new cdk.CfnOutput(this, "Step2_Connect", {
      value: `aws ssm start-session --target ${bastion.instance.instanceId}`,
      description: "Connect to bastion via SSM",
    });

    new cdk.CfnOutput(this, "Step3_AccessDB", {
      value: `psql -h ${dbEndpoint} -U stardag_admin -d stardag`,
      description: "Run this command on the bastion to access the database",
    });

    new cdk.CfnOutput(this, "Cleanup_RevokeDBAccess", {
      value: `aws ec2 revoke-security-group-ingress --group-id ${dbSecurityGroupId} --protocol tcp --port 5432 --source-group ${bastion.securityGroup.securityGroupId}`,
      description: "Run this before destroying the stack to clean up the SG rule",
    });
  }
}
