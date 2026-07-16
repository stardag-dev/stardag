import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cloudfront_origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53_targets from "aws-cdk-lib/aws-route53-targets";
import { Construct } from "constructs";
import { StardagConfig } from "./config";
import { FoundationStack } from "./foundation-stack";

export interface FrontendStackProps extends cdk.StackProps {
  config: StardagConfig;
  foundation: FoundationStack;

  /**
   * Route ``/api/*`` (plus ``/health`` and ``/.well-known/*``) through
   * CloudFront to the API, making the UI and API same-origin.
   *
   * Required when deploying a prebuilt UI dist from a GitHub release
   * (``deploy-ui.sh --release server-vX.Y.Z``): the prebuilt bundle has
   * no baked ``VITE_*`` config and resolves its auth/API configuration at
   * runtime from ``GET /api/v1/auth/config`` on ``window.location.origin``
   * — which only reaches the API if CloudFront forwards ``/api/*`` to it.
   *
   * Can also be enabled via CDK context (``-c uiApiProxy=true``) or the
   * ``STARDAG_UI_API_PROXY`` environment variable.
   *
   * Only supported when DNS is configured (the CloudFront origin points
   * at the API custom domain, which needs a valid certificate).
   *
   * Side effect: SPA fallback switches from distribution-wide 403/404
   * error responses (which would also rewrite legitimate API error
   * responses to ``index.html``) to a viewer-request CloudFront Function
   * scoped to the S3 behavior.
   *
   * @default false
   */
  apiProxy?: boolean;
}

/**
 * Frontend Stack - S3 + CloudFront for static UI hosting
 *
 * Depends on FoundationStack for:
 * - DNS/SSL certificates (optional)
 */
export class FrontendStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const { config, foundation } = props;

    // Optional same-origin API proxy (see FrontendStackProps.apiProxy).
    // Resolution order: stack prop > CDK context > environment variable.
    const rawApiProxy: unknown =
      props.apiProxy ??
      this.node.tryGetContext("uiApiProxy") ??
      process.env.STARDAG_UI_API_PROXY;
    const apiProxy =
      rawApiProxy === true ||
      (typeof rawApiProxy === "string" &&
        ["true", "1"].includes(rawApiProxy.trim().toLowerCase()));

    if (apiProxy && !foundation.dns) {
      throw new Error(
        "uiApiProxy requires DNS to be configured (a real DOMAIN_NAME): " +
          "the CloudFront API origin points at the API custom domain " +
          `(https://${config.apiDomain}) which needs a valid certificate.`,
      );
    }

    // =============================================================
    // S3 Bucket for Static Assets
    // =============================================================
    this.bucket = new s3.Bucket(this, "Bucket", {
      bucketName: `stardag-ui-${this.account}`,

      // Block all public access (CloudFront uses OAC)
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,

      // Enable versioning for rollback capability
      versioned: true,

      // Encryption
      encryption: s3.BucketEncryption.S3_MANAGED,

      // CORS configuration for API calls from the UI
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.HEAD],
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          maxAge: 3000,
        },
      ],

      // Cleanup on stack deletion (for dev)
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // =============================================================
    // CloudFront Origin Access Control (OAC)
    // =============================================================
    const oac = new cloudfront.S3OriginAccessControl(this, "OAC", {
      originAccessControlName: "stardag-ui-oac",
      signing: cloudfront.Signing.SIGV4_ALWAYS,
    });

    // =============================================================
    // Optional same-origin API proxy behaviors
    // =============================================================
    // In apiProxy mode, CloudFront forwards API paths to the API custom
    // domain so the UI and API share an origin. The distribution-wide
    // 403/404 -> index.html error responses used for SPA routing in the
    // default mode would also rewrite legitimate API error responses
    // (CloudFront custom error responses apply to every cache behavior),
    // so SPA routing switches to a viewer-request function scoped to the
    // S3 behavior: extensionless paths are rewritten to /index.html.
    const spaRewriteFunction = apiProxy
      ? new cloudfront.Function(this, "SpaRewriteFn", {
          comment: "Rewrite extensionless paths to /index.html (SPA routing)",
          runtime: cloudfront.FunctionRuntime.JS_2_0,
          code: cloudfront.FunctionCode.fromInline(
            [
              "function handler(event) {",
              "  var request = event.request;",
              "  // Paths without a file extension are SPA routes.",
              "  if (!request.uri.includes('.')) {",
              "    request.uri = '/index.html';",
              "  }",
              "  return request;",
              "}",
            ].join("\n"),
          ),
        })
      : undefined;

    const apiBehavior: cloudfront.BehaviorOptions | undefined = apiProxy
      ? {
          // Point at the API custom domain (resolves to the ALB via
          // Route53); avoids a cross-stack reference to the Api stack and
          // presents a certificate that matches the origin hostname.
          origin: new cloudfront_origins.HttpOrigin(config.apiDomain, {
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
          }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          // Forward all viewer headers (incl. Authorization) except Host,
          // which must stay the origin's own hostname for TLS/routing.
          originRequestPolicy:
            cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          compress: true,
        }
      : undefined;

    // =============================================================
    // CloudFront Distribution
    // =============================================================
    const certificate = foundation.dns?.uiCertificate;

    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: `Stardag UI - ${config.uiDomain}`,

      // Custom domain configuration (if certificate provided)
      ...(certificate
        ? {
            domainNames: [config.uiDomain],
            certificate,
          }
        : {}),

      defaultBehavior: {
        origin: cloudfront_origins.S3BucketOrigin.withOriginAccessControl(this.bucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
        responseHeadersPolicy:
          cloudfront.ResponseHeadersPolicy
            .CORS_ALLOW_ALL_ORIGINS_WITH_PREFLIGHT_AND_SECURITY_HEADERS,
        compress: true,
        ...(spaRewriteFunction
          ? {
              functionAssociations: [
                {
                  function: spaRewriteFunction,
                  eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
                },
              ],
            }
          : {}),
      },

      // Same-origin API proxy behaviors (apiProxy mode only)
      ...(apiBehavior
        ? {
            additionalBehaviors: {
              "/api/*": apiBehavior,
              "/health": apiBehavior,
              // JWKS is served at the root-level standard location too
              "/.well-known/*": apiBehavior,
            },
          }
        : {}),

      // SPA routing (default mode): serve index.html for all 403/404
      // errors. In apiProxy mode the viewer-request function on the S3
      // behavior handles SPA routing instead, so API error responses
      // pass through untouched.
      ...(apiProxy
        ? {}
        : {
            errorResponses: [
              {
                httpStatus: 403,
                responseHttpStatus: 200,
                responsePagePath: "/index.html",
                ttl: cdk.Duration.minutes(5),
              },
              {
                httpStatus: 404,
                responseHttpStatus: 200,
                responsePagePath: "/index.html",
                ttl: cdk.Duration.minutes(5),
              },
            ],
          }),

      // Default root object
      defaultRootObject: "index.html",

      // Price class (use all edge locations for best performance)
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100, // North America & Europe only (cheaper)

      // Enable HTTP/2 and HTTP/3
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,

      // Minimum TLS version
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,

      // Enable logging (optional - can be removed for cost savings)
      enableLogging: false,
    });

    // =============================================================
    // DNS Record (if DNS is configured)
    // =============================================================
    if (foundation.dns) {
      new route53.ARecord(this, "UiARecord", {
        zone: foundation.dns.hostedZone,
        recordName: config.uiDomain,
        target: route53.RecordTarget.fromAlias(
          new route53_targets.CloudFrontTarget(this.distribution),
        ),
        comment: "Stardag UI",
      });
    }

    // =============================================================
    // Outputs
    // =============================================================
    new cdk.CfnOutput(this, "BucketName", {
      value: this.bucket.bucketName,
      description: "S3 bucket name for UI assets",
      exportName: "StardagFrontendBucketName",
    });

    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
      description: "CloudFront distribution ID",
      exportName: "StardagFrontendDistributionId",
    });

    new cdk.CfnOutput(this, "ApiProxyEnabled", {
      value: apiProxy ? "true" : "false",
      description:
        "Whether CloudFront routes /api/* to the API (required for " +
        "prebuilt UI dists deployed with deploy-ui.sh --release)",
      exportName: "StardagFrontendApiProxyEnabled",
    });

    new cdk.CfnOutput(this, "DistributionDomain", {
      value: this.distribution.distributionDomainName,
      description: "CloudFront distribution domain (temporary, before custom domain)",
      exportName: "StardagFrontendDistributionDomain",
    });

    new cdk.CfnOutput(this, "UiUrl", {
      value: certificate
        ? `https://${config.uiDomain}`
        : `https://${this.distribution.distributionDomainName}`,
      description: "UI application URL",
      exportName: "StardagUiUrl",
    });

    new cdk.CfnOutput(this, "DeployCommand", {
      value: `aws s3 sync ./dist s3://${this.bucket.bucketName} --delete && aws cloudfront create-invalidation --distribution-id ${this.distribution.distributionId} --paths "/*"`,
      description: "Command to deploy UI assets",
    });

    // Output Cognito values needed for UI build
    new cdk.CfnOutput(this, "CognitoClientId", {
      value: foundation.cognitoClientId,
      description: "Cognito Client ID for UI configuration",
    });

    new cdk.CfnOutput(this, "CognitoIssuerUrl", {
      value: foundation.cognitoIssuerUrl,
      description: "OIDC Issuer URL for UI configuration",
    });

    new cdk.CfnOutput(this, "CognitoDomain", {
      value: foundation.cognitoDomain,
      description: "Cognito Domain for UI configuration",
    });
  }
}
