import * as cdk from "aws-cdk-lib";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53_targets from "aws-cdk-lib/aws-route53-targets";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { Construct } from "constructs";

export interface StardagDnsProps {
  /**
   * Domain name (e.g., stardag.com)
   */
  domainName: string;

  /**
   * API subdomain (e.g., api)
   */
  apiSubdomain: string;

  /**
   * UI subdomain (e.g., app)
   */
  uiSubdomain: string;
}

export class StardagDns extends Construct {
  public readonly hostedZone: route53.IHostedZone;
  public readonly apiCertificate: acm.Certificate;
  public readonly uiCertificate: acm.Certificate;

  public readonly apiDomain: string;
  public readonly uiDomain: string;

  constructor(scope: Construct, id: string, props: StardagDnsProps) {
    super(scope, id);

    const { domainName, apiSubdomain, uiSubdomain } = props;

    this.apiDomain = `${apiSubdomain}.${domainName}`;
    this.uiDomain = `${uiSubdomain}.${domainName}`;

    // =============================================================
    // Route 53 Hosted Zone (lookup existing)
    // =============================================================
    this.hostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
      domainName: domainName,
    });

    // =============================================================
    // ACM Certificate for API (ALB) - in current region
    // =============================================================
    this.apiCertificate = new acm.Certificate(this, "ApiCertificate", {
      domainName: this.apiDomain,
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
      certificateName: `stardag-api-${this.apiDomain}`,
    });

    // =============================================================
    // ACM Certificate for UI (CloudFront) - MUST be in us-east-1
    // =============================================================
    // CloudFront requires certificates to be in us-east-1
    // Since we're deploying to us-east-1, we can use the same approach
    // If deploying to another region, would need cross-region certificate
    this.uiCertificate = new acm.Certificate(this, "UiCertificate", {
      domainName: this.uiDomain,
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
      certificateName: `stardag-ui-${this.uiDomain}`,
    });

    // =============================================================
    // Outputs
    // =============================================================
    new cdk.CfnOutput(this, "HostedZoneId", {
      value: this.hostedZone.hostedZoneId,
      description: "Route 53 Hosted Zone ID",
    });

    new cdk.CfnOutput(this, "ApiCertificateArn", {
      value: this.apiCertificate.certificateArn,
      description: "ACM Certificate ARN for API",
    });

    new cdk.CfnOutput(this, "UiCertificateArn", {
      value: this.uiCertificate.certificateArn,
      description: "ACM Certificate ARN for UI",
    });
  }

  /**
   * Create A record for API pointing to ALB
   */
  createApiRecord(alb: elbv2.IApplicationLoadBalancer): route53.ARecord {
    return new route53.ARecord(this, "ApiARecord", {
      zone: this.hostedZone,
      recordName: this.apiDomain,
      target: route53.RecordTarget.fromAlias(
        new route53_targets.LoadBalancerTarget(alb),
      ),
      comment: "Stardag API",
    });
  }

  /**
   * Create A record for UI pointing to CloudFront
   */
  createUiRecord(distribution: cloudfront.IDistribution): route53.ARecord {
    return new route53.ARecord(this, "UiARecord", {
      zone: this.hostedZone,
      recordName: this.uiDomain,
      target: route53.RecordTarget.fromAlias(
        new route53_targets.CloudFrontTarget(distribution),
      ),
      comment: "Stardag UI",
    });
  }
}
