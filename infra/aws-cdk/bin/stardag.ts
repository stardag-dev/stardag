#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { FoundationStack } from "../lib/foundation-stack";
import { ApiStack } from "../lib/api-stack";
import { FrontendStack } from "../lib/frontend-stack";
import { BastionStack } from "../lib/bastion-stack";
import { config } from "../lib/config";

const app = new cdk.App();

const env = {
  account: config.awsAccountId,
  region: config.awsRegion,
};

// =============================================================
// Stack 1: Foundation (VPC, Database, Cognito, ECR, DNS)
// =============================================================
const foundation = new FoundationStack(app, "StardagFoundation", {
  env,
  config,
  description: "Stardag Foundation - VPC, Database, Cognito, ECR, DNS/Certificates",
});

// =============================================================
// Stack 2: API (ECS Fargate + ALB)
// Depends on Foundation for VPC, DB, Cognito, ECR
// =============================================================
const api = new ApiStack(app, "StardagApi", {
  env,
  config,
  foundation,
  description: "Stardag API - ECS Fargate service with ALB",
});
api.addDependency(foundation);

// =============================================================
// Stack 3: Frontend (S3 + CloudFront)
// Depends on Foundation for DNS/Certificates
// =============================================================
const frontend = new FrontendStack(app, "StardagFrontend", {
  env,
  config,
  foundation,
  description: "Stardag Frontend - S3 static hosting with CloudFront CDN",
});
frontend.addDependency(foundation);

// =============================================================
// Stack 4: Bastion (Optional - EC2 for database access)
// Deploy separately: npx cdk deploy StardagBastion
// Destroy when done: npx cdk destroy StardagBastion
// =============================================================
const bastion = new BastionStack(app, "StardagBastion", {
  env,
  vpc: foundation.vpc,
  dbEndpoint: foundation.dbClusterEndpoint,
  dbSecurityGroupId: foundation.dbSecurityGroup.securityGroupId,
  description: "Stardag Bastion - Optional EC2 host for database access via SSM",
});
bastion.addDependency(foundation);
