# Self-Hosting

Deploy your own Stardag API and UI.

## Prerequisites

- Docker and Docker Compose
- PostgreSQL database (or use included container)
- OAuth/OIDC provider (optional, for authentication)

## Local Development Stack

The repository includes a `docker-compose.yml` for running the full stack locally. This is intended for:

- Local development and testing
- Running a personal local registry
- Evaluating the platform before deploying

!!! warning "Not for Production"
The local Docker Compose setup uses development defaults (simple passwords, Keycloak in dev mode). For production deployment, see [Production Deployment](#production-deployment) below.

### Start the Stack

```bash
git clone https://github.com/andhus/stardag.git
cd stardag
docker compose up -d
```

This starts:

| Service     | Port | Description                    |
| ----------- | ---- | ------------------------------ |
| PostgreSQL  | 5432 | Database                       |
| Keycloak    | 8080 | OAuth/OIDC provider (dev mode) |
| Stardag API | 8000 | REST API                       |
| Stardag UI  | 3000 | Web dashboard                  |

The stack automatically:

- Runs database migrations
- Seeds development data
- Configures Keycloak with a `stardag` realm

### Verify

```bash
# Check API health
curl http://localhost:8000/health

# Open UI
open http://localhost:3000

# Keycloak admin console
open http://localhost:8080  # admin/admin
```

### Configure SDK

Point your SDK to the local registry:

=== "Activated venv"

    ```bash
    stardag config registry add local --url http://localhost:8000
    stardag auth login --registry local
    ```

=== "uv run ..."

    ```bash
    uv run stardag config registry add local --url http://localhost:8000
    uv run stardag auth login --registry local
    ```

### Stop the Stack

```bash
docker compose down        # Stop containers
docker compose down -v     # Stop and remove volumes (reset data)
```

## Production Deployment

### AWS Deployment

The project includes AWS CDK infrastructure:

```bash
cd infra/aws-cdk
npm install
npx cdk deploy --all
```

This creates:

- Aurora Serverless PostgreSQL
- ECS Fargate for API
- S3 + CloudFront for UI
- Cognito for authentication
- Route 53 DNS records

See the `infra/aws-cdk/` directory for configuration details.

### Manual Deployment

#### API Service

1. Build Docker image:

```bash
cd app/stardag-api
docker build -t stardag-api .
```

2. Required environment variables:

```bash
STARDAG_API_DATABASE_HOST=your-db-host
STARDAG_API_DATABASE_PORT=5432
STARDAG_API_DATABASE_NAME=stardag
STARDAG_API_DATABASE_USER=user
STARDAG_API_DATABASE_PASSWORD=password
OIDC_ISSUER_URL=https://your-idp
OIDC_AUDIENCE=client-id
STARDAG_API_CORS_ORIGINS=https://your-ui-domain
```

3. Run migrations:

```bash
docker run stardag-api alembic upgrade head
```

4. Start API:

```bash
docker run -p 8000:8000 stardag-api
```

#### Web UI

1. Build UI:

```bash
cd app/stardag-ui
VITE_API_BASE_URL=https://your-api-domain \
VITE_OIDC_ISSUER=https://your-idp \
VITE_OIDC_CLIENT_ID=client-id \
npm run build
```

2. Deploy `dist/` to static hosting (S3, CloudFront, Nginx, etc.)

## Authentication Setup

### Without Authentication

For internal/development use, you can run without OAuth:

<!-- TODO: Document unauthenticated mode -->

### With Cognito

AWS Cognito setup:

1. Create User Pool
2. Configure GitHub as Identity Provider
3. Set callback URLs
4. Update API and UI environment variables

### With Other OIDC Providers

Any OIDC-compliant provider works:

- Okta
- Auth0
- Keycloak
- Google Workspace

Configure:

```bash
OIDC_ISSUER_URL=https://your-idp/.well-known/openid-configuration
OIDC_AUDIENCE=your-client-id
```

## Database

### PostgreSQL Requirements

- PostgreSQL 13+
- Create database and user
- Run migrations

### Migrations

```bash
cd app/stardag-api
alembic upgrade head
```

## Scaling

### API

- Horizontal scaling with load balancer
- Stateless - scale freely
- Consider read replicas for database

### Database

- Aurora Serverless scales automatically
- Or provision based on expected load

## Monitoring

### Health Checks

```
GET /health        # Basic health
GET /health/ready  # Ready for traffic
```

### Logging

API logs to stdout in JSON format. Configure log aggregation as needed.

## Troubleshooting

### Database Connection Failed

Check:

- Database host/port reachable
- Credentials correct
- Security groups/firewall rules

### Authentication Errors

Verify:

- OIDC issuer URL correct
- Audience matches client ID
- Callback URLs configured

### CORS Errors

Ensure `STARDAG_API_CORS_ORIGINS` includes your UI domain.

## See Also

- [API Documentation](api.md)
- [Configuration Guide](../configuration/index.md)
