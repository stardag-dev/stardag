"""Deployments of the in-memory test registry (split from ``_registry.py``
to keep it under the file-length limit)."""

from __future__ import annotations

from uuid import UUID

from stardag.build._registration import new_id
from stardag.registry import (
    DeploymentInfo,
)
from stardag.registry._models import DeploymentKind
from stardag.testing._registry_state import (
    DeploymentRow,
    RegistryState,
    refuse,
)


class DeploymentsMixin(RegistryState):
    """Deployment records: create, activate, list."""

    def _deployment_info(
        self, row: DeploymentRow, *, created: bool = False
    ) -> DeploymentInfo:
        current = self.current_deployment(row.kind, row.app_name)
        return DeploymentInfo(
            id=row.id,
            kind=row.kind,
            app_name=row.app_name,
            code_id=row.code_id,
            image_id=row.image_id,
            modal_app_id=row.modal_app_id,
            generation=row.generation,
            deployed_at=row.deployed_at,
            activated_at=row.activated_at,
            is_current=current is not None and current.id == row.id,
            created=created,
        )

    def deployment_create(
        self,
        *,
        kind: DeploymentKind,
        code_id: str,
        deployment_id: UUID | None = None,
        app_name: str | None = None,
        image_id: str | None = None,
        modal_app_id: str | None = None,
    ) -> DeploymentInfo:
        self._record(
            "deployment_create",
            kind=kind,
            code_id=code_id,
            deployment_id=deployment_id,
            app_name=app_name,
        )
        now = self.now()
        if kind == "local":
            app_name = app_name or "local"
            for row in self.deployments.values():
                if row.kind == "local" and row.code_id == code_id:
                    if row.app_name != app_name:
                        raise refuse("local_deployment_conflict")
                    return self._deployment_info(row)
        else:
            if deployment_id is None:
                raise refuse("deployment_id_required", status=400)
            if not app_name:
                raise refuse("app_name_required", status=400)
            existing = self.deployments.get(deployment_id)
            if existing is not None:
                if (existing.kind, existing.app_name, existing.code_id) != (
                    kind,
                    app_name,
                    code_id,
                ):
                    raise refuse("deployment_id_conflict")
                return self._deployment_info(existing)
        generation = 1 + max(
            (
                d.generation
                for d in self.deployments.values()
                if d.kind == kind and d.app_name == app_name
            ),
            default=0,
        )
        row = DeploymentRow(
            id=deployment_id or new_id(),
            kind=kind,
            app_name=app_name,
            code_id=code_id,
            generation=generation,
            deployed_at=now,
            activated_at=now if kind == "local" else None,
            image_id=image_id,
            modal_app_id=modal_app_id,
        )
        self.deployments[row.id] = row
        return self._deployment_info(row, created=True)

    def deployment_activate(
        self,
        deployment_id: UUID,
        *,
        modal_app_id: str | None = None,
        image_id: str | None = None,
    ) -> DeploymentInfo:
        self._record(
            "deployment_activate",
            deployment_id=deployment_id,
            modal_app_id=modal_app_id,
            image_id=image_id,
        )
        row = self.deployments.get(deployment_id)
        if row is None:
            raise refuse("unknown_deployment", status=404)
        # A given value fills a NULL or must match (the server's rule):
        # 409 `deployment_activation_conflict` with every clashing field,
        # nothing written (services/deployments.py, activate_deployment).
        given = {"modal_app_id": modal_app_id, "image_id": image_id}
        clashing = sorted(
            column
            for column, value in given.items()
            if value is not None and getattr(row, column) not in (None, value)
        )
        if clashing:
            raise refuse("deployment_activation_conflict", fields=clashing)
        for column, value in given.items():
            if value is not None:
                setattr(row, column, value)
        if row.activated_at is None:
            row.activated_at = self.now()
        return self._deployment_info(row)

    def deployment_list(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        self._record(
            "deployment_list",
            kind=kind,
            app_name=app_name,
            current=current,
            limit=limit,
        )
        rows = sorted(
            (
                d
                for d in self.deployments.values()
                if (kind is None or d.kind == kind)
                and (app_name is None or d.app_name == app_name)
            ),
            key=lambda d: d.deployed_at,
            reverse=True,
        )
        infos = [self._deployment_info(d) for d in rows]
        if current:
            infos = [i for i in infos if i.is_current]
        return infos[:limit]
