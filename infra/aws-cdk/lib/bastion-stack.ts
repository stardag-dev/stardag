import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { StardagBastion } from "./constructs/bastion";
import { FoundationStack } from "./foundation-stack";

export interface BastionStackProps extends cdk.StackProps {
  foundation: FoundationStack;
}

/**
 * Bastion Stack - Optional EC2 bastion host for database access
 *
 * This stack is deployed separately and can be destroyed when not needed
 * to save costs. Uses SSM Session Manager for secure access (no SSH keys).
 *
 * Deploy: npx cdk deploy StardagBastion
 * Destroy: npx cdk destroy StardagBastion
 *
 * Usage:
 *   1. Deploy: npx cdk deploy StardagBastion
 *   2. Connect: aws ssm start-session --target <instance-id>
 *   3. Access DB: psql -h <db-endpoint> -U stardag_admin -d stardag
 *   4. Destroy when done: npx cdk destroy StardagBastion
 */
export class BastionStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BastionStackProps) {
    super(scope, id, props);

    const { foundation } = props;

    // Create bastion host
    const bastion = new StardagBastion(this, "Bastion", {
      vpc: foundation.vpc,
    });

    // Allow bastion to connect to database
    foundation.dbSecurityGroup.addIngressRule(
      bastion.securityGroup,
      ec2.Port.tcp(5432),
      "Allow bastion host to connect to database",
    );

    // Output connection instructions
    new cdk.CfnOutput(this, "ConnectionInstructions", {
      value: `aws ssm start-session --target ${bastion.instance.instanceId}`,
      description: "Command to connect to bastion host",
    });

    new cdk.CfnOutput(this, "DatabaseConnection", {
      value: `psql -h ${foundation.dbClusterEndpoint} -U stardag_admin -d stardag`,
      description: "Command to connect to database from bastion",
    });
  }
}
