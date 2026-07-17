#!/bin/bash
# Run database migrations on AWS
# Uses ECS to run migrations task against Aurora database
#
# The migration task reuses the API service's task definition and overrides
# the container command with `alembic upgrade head`. This assumes the image
# working directory contains alembic.ini and migrations/ — true for both
# the locally-built stardag-api image and the prebuilt public server image
# (ghcr.io/stardag-dev/stardag-server:X.Y.Z), which keep the same layout.
# For images with a different layout, override with e.g.:
#   MIGRATION_COMMAND="alembic -c /path/to/alembic.ini upgrade head" ./scripts/run-migrations.sh
set -e

# Command to run inside the container (split into argv with shell-style
# quoting, so arguments containing spaces can be quoted)
MIGRATION_COMMAND="${MIGRATION_COMMAND:-alembic upgrade head}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CDK_DIR"

# Load config
if [ -f .env.deploy ]; then
    export $(grep -v '^#' .env.deploy | xargs)
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

# Only use AWS_PROFILE if credentials aren't already set (CI uses OIDC env vars)
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    AWS_PROFILE="${AWS_PROFILE:-stardag}"
    AWS_CMD="aws --profile $AWS_PROFILE"
    echo "=== Running Database Migrations ==="
    echo "AWS Profile: $AWS_PROFILE"
else
    AWS_CMD="aws"
    echo "=== Running Database Migrations ==="
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"
echo ""

# Get stack exports
echo "=== Getting stack exports ==="
ECR_URI=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagEcrRepositoryUri'].Value" \
    --output text \
    --region $AWS_REGION)

CLUSTER_NAME=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagApiClusterName'].Value" \
    --output text \
    --region $AWS_REGION)

if [ -z "$ECR_URI" ] || [ "$ECR_URI" == "None" ]; then
    echo "ERROR: Could not find ECR repository. Have you deployed the infrastructure and API?"
    exit 1
fi

echo "ECR Repository: $ECR_URI"
echo "ECS Cluster: $CLUSTER_NAME"
echo ""

# Get the task definition ARN for the API service
TASK_DEF_ARN=$($AWS_CMD ecs describe-services \
    --cluster $CLUSTER_NAME \
    --services stardag-api \
    --query "services[0].taskDefinition" \
    --output text \
    --region $AWS_REGION)

echo "Task Definition: $TASK_DEF_ARN"

# Get VPC and subnet info for the task
VPC_ID=$($AWS_CMD ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=*Stardag*" \
    --query "Vpcs[0].VpcId" \
    --output text \
    --region $AWS_REGION)

SUBNET_ID=$($AWS_CMD ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:aws-cdk:subnet-type,Values=Private" \
    --query "Subnets[0].SubnetId" \
    --output text \
    --region $AWS_REGION)

SECURITY_GROUP=$($AWS_CMD ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=description,Values=*Stardag API*" \
    --query "SecurityGroups[0].GroupId" \
    --output text \
    --region $AWS_REGION)

echo "VPC: $VPC_ID"
echo "Subnet: $SUBNET_ID"
echo "Security Group: $SECURITY_GROUP"
echo ""

# Run migrations as a one-off ECS task with admin credentials
echo "=== Running migrations task ==="

# Get the admin secret ARN
ADMIN_SECRET_ARN=$($AWS_CMD secretsmanager list-secrets \
    --filters Key=name,Values=stardag/db/admin \
    --query "SecretList[0].ARN" \
    --output text \
    --region $AWS_REGION)

echo "Admin Secret: $ADMIN_SECRET_ARN"

# Run a one-off task from the API service's task definition, overriding
# the command to run alembic instead of the API server. The command is
# configurable via MIGRATION_COMMAND (see header comment).
echo "Migration command: $MIGRATION_COMMAND"
OVERRIDES=$(python3 -c "
import json, shlex, sys
command = shlex.split(sys.argv[1])
print(json.dumps({'containerOverrides': [{'name': 'Api', 'command': command}]}))
" "$MIGRATION_COMMAND")

TASK_RUN_RESULT=$($AWS_CMD ecs run-task \
    --cluster $CLUSTER_NAME \
    --task-definition $TASK_DEF_ARN \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SECURITY_GROUP],assignPublicIp=DISABLED}" \
    --overrides "$OVERRIDES" \
    --region $AWS_REGION \
    --output json)

TASK_ARN=$(echo $TASK_RUN_RESULT | python3 -c "import sys, json; print(json.load(sys.stdin)['tasks'][0]['taskArn'])")

echo "Migration task started: $TASK_ARN"
echo ""
echo "=== Waiting for migration to complete ==="

# Wait for task to complete
$AWS_CMD ecs wait tasks-stopped \
    --cluster $CLUSTER_NAME \
    --tasks $TASK_ARN \
    --region $AWS_REGION

# Check exit code
EXIT_CODE=$($AWS_CMD ecs describe-tasks \
    --cluster $CLUSTER_NAME \
    --tasks $TASK_ARN \
    --query "tasks[0].containers[0].exitCode" \
    --output text \
    --region $AWS_REGION)

if [ "$EXIT_CODE" == "0" ]; then
    echo ""
    echo "=== Migrations completed successfully ==="
else
    echo ""
    echo "ERROR: Migrations failed with exit code $EXIT_CODE"
    echo ""
    echo "Check logs with:"
    echo "  $AWS_CMD logs tail /stardag/api --region $AWS_REGION"
    exit 1
fi
