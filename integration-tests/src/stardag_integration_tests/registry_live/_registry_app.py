"""A deployable Stardag registry with its Postgres inside the container.

One Modal function, pinned to a single container, that boots a local
Postgres, applies the Alembic chain from scratch and then serves the real
``stardag_api`` ASGI app. Everything the deployment owns -- the app, the
database, the volumes and secrets created around it -- lives inside one
Modal environment, so ``modal environment delete`` is the entire teardown.

Why not a hosted database: a per-run Postgres account means credentials to
store, projects to provision and to clean up, and a second thing that can
be left behind. None of that buys anything here, because the database is
wanted *empty* -- running the whole migration chain from scratch on every
pass is a check the deployed path never performs.

Two properties are load-bearing and neither is incidental:

- **Exactly one container.** ``max_containers=1`` so every request reaches
  the one process that has the database, and ``modal.concurrent`` so a
  single container still serves concurrent requests -- which the claim-race
  scenario depends on.
- **That container must not be recycled mid-run**, because a recycle loses
  the whole database rather than some rows, and the resulting failure reads
  as a scheduling bug. ``min_containers=1`` plus a generous
  ``scaledown_window`` is the prevention; ``/_harness/boot`` below is the
  detection, so that if it ever does happen the harness says so in one line
  instead of leaving someone to debug a phantom.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import modal

API_REMOTE_DIR = "/opt/stardag/api"
PGDATA = "/pgdata"
PG_USER = "stardag"
PG_DB = "stardag"

DEFAULT_APP_NAME = "registry"

# Discovering the bin directory beats naming a major version: the base
# image's Debian release decides which Postgres `apt` installs, and pinning
# the version here would break silently the next time that moves.
_PGBIN = "PGBIN=$(ls -d /usr/lib/postgresql/*/bin | head -1)"


def client_python_version() -> str:
    """The running interpreter's "major.minor".

    The function body below is serialized by *this* process, and cloudpickle
    bytecode is not portable across Python minors, so the image's Python is
    taken from here rather than pinned. A mismatch is not a graceful
    failure: the container dies without a traceback.
    """
    return "{}.{}".format(*sys.version_info[:2])


def _image(repo_root: Path, python_version: str | None = None) -> "modal.Image":
    """The registry image: Postgres initialised at build time, then the API.

    ``initdb`` and ``createdb`` run *here*, not at container start, so the
    ready-made cluster sits in an image layer and starting it costs about a
    second. The database only ever listens on ``127.0.0.1``, inside a
    container that runs exactly one thing, which is what makes ``trust``
    authentication the honest choice rather than a shortcut -- there is no
    network position from which to present a password.
    """
    import modal

    api_dir = repo_root / "app" / "stardag-api"

    return (
        modal.Image.debian_slim(
            python_version=python_version or client_python_version()
        )
        .apt_install("postgresql", "postgresql-client")
        .run_commands(
            f"mkdir -p {PGDATA} && chown postgres:postgres {PGDATA}",
            f'{_PGBIN} && su postgres -c "$PGBIN/initdb -D {PGDATA} '
            f'-U {PG_USER} -A trust"',
            # fsync off: the database is discarded with the container, and
            # durability across a crash we would fail on anyway is not worth
            # the write latency in a test.
            f"printf \"listen_addresses='127.0.0.1'\\nfsync=off\\n\" "
            f">> {PGDATA}/postgresql.conf",
            f'{_PGBIN} && su postgres -c "$PGBIN/pg_ctl -D {PGDATA} -w start" '
            f'&& su postgres -c "$PGBIN/createdb -h 127.0.0.1 -U {PG_USER} {PG_DB}" '
            f'&& su postgres -c "$PGBIN/pg_ctl -D {PGDATA} -m fast -w stop"',
        )
        .add_local_dir(
            api_dir.as_posix(),
            API_REMOTE_DIR,
            copy=True,
            ignore=[".venv", "__pycache__", ".pytest_cache", "tests"],
        )
        .run_commands(f"python -m pip install {API_REMOTE_DIR}")
    )


def _make_web(api_remote_dir: str, pgdata: str):
    """Return the ASGI-app factory as a **closure**.

    Not a module-level function, and this is not a style choice.
    ``serialized=True`` cloudpickles a closure by value, so the body travels
    with the function definition and the image needs none of this package.
    A module-level function pickles by *reference* instead: the deploy
    succeeds, and then every container dies at start with
    ``DeserializationError: ... module is not available``. Moving this body
    to module level is a green deploy followed by a dead deployment.
    """

    def _web():
        import glob
        import subprocess
        import time
        import uuid

        t0 = time.monotonic()
        pgbin = sorted(glob.glob("/usr/lib/postgresql/*/bin"))[-1]

        def run(cmd: str, **kwargs) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd, shell=True, capture_output=True, text=True, **kwargs
            )

        started = run(
            f'su postgres -c "{pgbin}/pg_ctl -D {pgdata} -l /tmp/postgres.log -w start"'
        )
        if started.returncode != 0:
            try:
                print(Path("/tmp/postgres.log").read_text())
            except OSError:
                pass
            raise RuntimeError(
                f"Postgres failed to start (rc={started.returncode}): "
                f"{started.stdout}{started.stderr}"
            )
        t_pg = time.monotonic()

        migrated = run(
            "python -m alembic -c alembic.ini upgrade head", cwd=api_remote_dir
        )
        output = ((migrated.stdout or "") + (migrated.stderr or "")).strip()
        if migrated.returncode != 0:
            raise RuntimeError(f"Migrations failed:\n{output}")
        t_migrated = time.monotonic()

        print(
            f"[registry] postgres {t_pg - t0:.1f}s, migrations {t_migrated - t_pg:.1f}s"
        )

        from stardag_api.server import create_app  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

        app = create_app(None)

        # The recycle detector. Generated per container, so a value that
        # changes mid-run is proof that the process holding the database was
        # replaced -- and every scenario that was mid-flight was reading a
        # database that no longer exists. Without this the symptom is a
        # build whose tasks have silently reverted to unregistered, which
        # looks exactly like a stardag bug and is not one.
        boot_id = uuid.uuid4().hex

        @app.get("/_harness/boot")
        async def _boot_id() -> dict[str, str]:
            return {"boot_id": boot_id}

        return app

    return _web


def build_registry_app(
    repo_root: Path,
    app_name: str = DEFAULT_APP_NAME,
    *,
    config: dict[str, str],
    python_version: str | None = None,
    scaledown_window: int = 1800,
) -> tuple["modal.App", dict[str, Any]]:
    """Build the single-container registry app.

    ``config`` is the API's environment: database URL, auth mode, bootstrap
    admin, JWT keys. It is passed as an inline ``modal.Secret`` rather than
    a named one so the deployment carries no state that outlives the Modal
    environment it is deployed into.
    """
    import modal

    app = modal.App(app_name)
    image = _image(repo_root, python_version)
    # Modal's from_dict takes optional values; ours are all set.
    env: dict[str, str | None] = dict(config)

    web = app.function(
        image=image,
        secrets=[modal.Secret.from_dict(env)],
        serialized=True,
        # One container, kept alive: see the module docstring. The pair is
        # what stands between a run and a lost database.
        min_containers=1,
        max_containers=1,
        scaledown_window=scaledown_window,
        timeout=900,
        name="web",
    )(
        modal.concurrent(max_inputs=100)(
            modal.asgi_app(label=app_name)(_make_web(API_REMOTE_DIR, PGDATA))
        )
    )

    return app, {"web": web}


def registry_config(
    *,
    admin_email: str,
    admin_password: str,
    workspace_name: str,
    environment_slug: str,
    jwt_private_key: str,
    jwt_public_key: str,
) -> dict[str, str]:
    """The API's environment for an embedded-Postgres deployment.

    Mirrors ``_build_config_env`` in the self-host CLI, minus everything
    that only a hosted database needs. The database URL is spelled out
    rather than left to ``Settings``' defaults, which happen to point at the
    same place today: a harness that silently depends on a default is a
    harness that breaks when the default moves.
    """
    return {
        "STARDAG_API_DATABASE_URL": (
            f"postgresql+asyncpg://{PG_USER}:unused@127.0.0.1:5432/{PG_DB}"
        ),
        "STARDAG_API_DATABASE_POOLER_COMPAT": "false",
        "AUTH_MODE": "local",
        "AUTH_LOCAL_REGISTRATION_ENABLED": "false",
        "AUTH_BOOTSTRAP_ADMIN_EMAIL": admin_email,
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        "AUTH_PRIMARY_WORKSPACE_NAME": workspace_name,
        "AUTH_PRIMARY_WORKSPACE_ENVIRONMENT": environment_slug,
        "EMAIL_ENABLED": "false",
        "JWT_PRIVATE_KEY": jwt_private_key,
        "JWT_PUBLIC_KEY": jwt_public_key,
    }


def generate_jwt_keypair() -> tuple[str, str]:
    """An RSA keypair (private_pem, public_pem) for the deployment's JWTs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
