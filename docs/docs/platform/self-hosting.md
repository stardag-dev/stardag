# Self-Hosting

Deploy your own Stardag API and UI.

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

!!! warning "The AWS CDK deployment is experimental"

    It should run "as is" against a fresh AWS account, but please review the configuration and verify that it meet your needs and expectations.

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
