FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy everything needed for install
COPY pyproject.toml .
COPY src/ src/

# Install the package
RUN uv pip install --system .

# Copy alembic configuration and migrations
COPY alembic.ini .
COPY migrations/ migrations/

EXPOSE 8000

# gunicorn with multiple uvicorn workers absorbs CPU-bound bursts (auth /
# JWT verification, bcrypt cache misses) better than a single uvicorn
# process. The default is sized for the production Fargate sizing
# (1 vCPU); override at deploy time via GUNICORN_WORKERS for a different
# sizing. Note: limits.py rate-limiter and entity-count cache are
# per-process; effective per-instance limits scale with worker count.
#
# --preload imports the app module once in the master process before
# fork()-ing workers. main.py eagerly instantiates the InternalTokenManager
# at import time, so its (potentially ephemeral) RSA keypair is generated
# in the master and inherited by every worker. Without --preload, each
# worker would generate its own keypair — a token signed in worker A
# would then fail validation in worker B and return 401.
ENV GUNICORN_WORKERS=2
CMD ["sh", "-c", "exec gunicorn stardag_api.main:app -k uvicorn.workers.UvicornWorker -w ${GUNICORN_WORKERS} --preload --bind 0.0.0.0:8000 --access-logfile - --error-logfile -"]
