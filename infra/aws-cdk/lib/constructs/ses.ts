import * as cdk from "aws-cdk-lib";
import * as ses from "aws-cdk-lib/aws-ses";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export interface StardagSesProps {
  /**
   * Domain name for SES identity (e.g., stardag.com)
   */
  domainName: string;

  /**
   * Route 53 hosted zone for DNS record creation
   */
  hostedZone: route53.IHostedZone;
}

/**
 * SES Email Identity construct for transactional emails
 *
 * Creates:
 * - SES domain identity
 * - DKIM records in Route 53
 * - Mail-from subdomain configuration
 */
export class StardagSes extends Construct {
  public readonly emailIdentity: ses.EmailIdentity;
  public readonly domainName: string;

  constructor(scope: Construct, id: string, props: StardagSesProps) {
    super(scope, id);

    const { domainName, hostedZone } = props;
    this.domainName = domainName;

    // =============================================================
    // SES Email Identity
    // =============================================================
    this.emailIdentity = new ses.EmailIdentity(this, "EmailIdentity", {
      identity: ses.Identity.publicHostedZone(hostedZone),
      // Enable DKIM signing (creates DNS records automatically)
      dkimSigning: true,
      // Configure mail-from subdomain for SPF alignment
      mailFromDomain: `mail.${domainName}`,
    });

    // =============================================================
    // Outputs
    // =============================================================
    new cdk.CfnOutput(this, "SesIdentityArn", {
      value: `arn:aws:ses:${cdk.Stack.of(this).region}:${
        cdk.Stack.of(this).account
      }:identity/${domainName}`,
      description: "SES Email Identity ARN",
      exportName: "StardagSesIdentityArn",
    });

    new cdk.CfnOutput(this, "SesDomain", {
      value: domainName,
      description: "SES verified domain",
      exportName: "StardagSesDomain",
    });
  }

  /**
   * Create an IAM policy statement granting permission to send emails from this domain
   */
  public grantSendEmail(grantee: iam.IGrantable): iam.Grant {
    const stack = cdk.Stack.of(this);
    return iam.Grant.addToPrincipal({
      grantee,
      actions: ["ses:SendEmail", "ses:SendRawEmail"],
      resourceArns: [
        `arn:aws:ses:${stack.region}:${stack.account}:identity/${this.domainName}`,
      ],
    });
  }
}
