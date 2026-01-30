import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

export interface StardagVpcProps {
  /**
   * Number of Availability Zones to use.
   * @default 2
   */
  maxAzs?: number;

  /**
   * Number of NAT Gateways.
   * @default 1 (cost optimization for MVP)
   */
  natGateways?: number;
}

export class StardagVpc extends Construct {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: StardagVpcProps = {}) {
    super(scope, id);

    const { maxAzs = 2, natGateways = 1 } = props;

    // Create VPC with public and private subnets
    this.vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs,
      natGateways,

      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: "Isolated",
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],

      // Enable DNS support for RDS
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });

    // Add VPC flow logs for debugging (optional, can be removed for cost savings)
    this.vpc.addFlowLog("FlowLog", {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(),
      trafficType: ec2.FlowLogTrafficType.REJECT,
    });

    // Output VPC ID
    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      description: "VPC ID",
    });
  }
}
