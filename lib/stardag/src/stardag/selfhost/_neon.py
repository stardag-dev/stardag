"""Minimal Neon (https://neon.com) API client for provisioning Postgres.

Uses only the endpoints needed by `stardag self-host`: find-or-create a
project and obtain direct + pooled connection strings.

API reference: https://api-docs.neon.tech/reference/getting-started-with-neon-api
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

NEON_API_BASE = "https://console.neon.tech/api/v2"


class NeonError(Exception):
    """Neon API interaction failed."""


class NeonAuthError(NeonError):
    """Neon API key rejected."""


@dataclass
class NeonDatabase:
    """Provisioned database connection info."""

    project_id: str
    project_name: str
    created: bool  # True if the project was created by this call
    direct_uri: str  # postgres://... (non-pooled endpoint)
    pooled_uri: str  # postgres://...-pooler... (PgBouncer, transaction mode)


def to_sqlalchemy_asyncpg_url(uri: str) -> str:
    """Convert a Neon postgres:// URI to an SQLAlchemy asyncpg URL.

    - scheme -> postgresql+asyncpg
    - query params reduced to ssl=require (asyncpg does not understand
      libpq params like sslmode/channel_binding; SQLAlchemy's asyncpg
      dialect accepts ssl=require)
    """
    parsed = urlsplit(uri)
    scheme = "postgresql+asyncpg"
    return urlunsplit((scheme, parsed.netloc, parsed.path, "ssl=require", ""))


class NeonClient:
    """Thin Neon API v2 client."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.Client(
            base_url=NEON_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise NeonError(f"Could not reach the Neon API: {e}") from e
        if response.status_code in (401, 403):
            raise NeonAuthError(
                "Neon API key rejected. Create one at "
                "https://console.neon.tech/app/settings/api-keys"
            )
        if response.status_code >= 400:
            raise NeonError(
                f"Neon API error ({response.status_code}) on {method} {path}: "
                f"{response.text}"
            )
        return response.json()

    def find_project_by_name(self, name: str) -> dict | None:
        data = self._request("GET", "/projects", params={"search": name})
        for project in data.get("projects", []):
            if project.get("name") == name:
                return project
        return None

    def create_project(self, name: str, pg_version: int = 16) -> dict:
        """Create a project; returns the full creation response.

        Postgres 16 by default - matches the version stardag targets in
        its AWS reference deployment and local docker-compose stack.
        """
        return self._request(
            "POST",
            "/projects",
            json={"project": {"name": name, "pg_version": pg_version}},
        )

    def _get_default_branch_id(self, project_id: str) -> str:
        data = self._request("GET", f"/projects/{project_id}/branches")
        branches = data.get("branches", [])
        for branch in branches:
            if branch.get("default"):
                return branch["id"]
        if branches:
            return branches[0]["id"]
        raise NeonError(f"Neon project {project_id} has no branches")

    def _get_connection_uri(
        self,
        project_id: str,
        branch_id: str,
        database_name: str,
        role_name: str,
        pooled: bool,
    ) -> str:
        data = self._request(
            "GET",
            f"/projects/{project_id}/connection_uri",
            params={
                "branch_id": branch_id,
                "database_name": database_name,
                "role_name": role_name,
                "pooled": str(pooled).lower(),
            },
        )
        return data["uri"]

    def get_connection_uris(self, project_id: str) -> tuple[str, str]:
        """Get (direct_uri, pooled_uri) for a project's default branch/db/role."""
        branch_id = self._get_default_branch_id(project_id)
        databases = self._request(
            "GET", f"/projects/{project_id}/branches/{branch_id}/databases"
        ).get("databases", [])
        if not databases:
            raise NeonError(f"Neon project {project_id} has no databases")
        database = databases[0]
        database_name = database["name"]
        role_name = database["owner_name"]
        direct = self._get_connection_uri(
            project_id, branch_id, database_name, role_name, pooled=False
        )
        pooled = self._get_connection_uri(
            project_id, branch_id, database_name, role_name, pooled=True
        )
        return direct, pooled

    def get_or_create_project(self, name: str) -> NeonDatabase:
        """Find a project by name or create it; returns connection info."""
        project = self.find_project_by_name(name)
        created = False
        if project is None:
            logger.info("Creating Neon project %r", name)
            creation = self.create_project(name)
            project = creation["project"]
            created = True
        direct_uri, pooled_uri = self.get_connection_uris(project["id"])
        return NeonDatabase(
            project_id=project["id"],
            project_name=project["name"],
            created=created,
            direct_uri=direct_uri,
            pooled_uri=pooled_uri,
        )

    def close(self) -> None:
        self._client.close()
