"""Exclusion and skipping in :class:`~stardag.testing.InMemoryRegistry`:
giving up on a member in one plan (an operator's exclude, a tick's
``discovery-failed``) with its downstream cascade, and ``skip-blocked`` over
a build's active plan (design.md, "Exclusion")."""

from __future__ import annotations

from uuid import UUID

from stardag.registry import ExclusionResult
from stardag.testing._registry_state import TERMINAL_BUILD_STATUSES, RegistryState


class ExclusionMixin(RegistryState):
    def member_exclude(
        self, plan_id: UUID, task_id: str, *, reason: str | None = None
    ) -> ExclusionResult:
        self._record("member_exclude", plan_id=plan_id, task_id=task_id, reason=reason)
        return self._exclude(plan_id, task_id, "operator", reason)

    def member_discovery_failed(
        self, plan_id: UUID, task_id: str, *, error: str
    ) -> ExclusionResult:
        self._record(
            "member_discovery_failed", plan_id=plan_id, task_id=task_id, error=error
        )
        return self._exclude(plan_id, task_id, "discovery_failed", error)

    def _exclude(
        self, plan_id: UUID, task_id: str, reason: str, message: str | None
    ) -> ExclusionResult:
        plan = self.plan(plan_id)
        members = self.members.get(plan_id, {})
        if self.member(plan_id, task_id).excluded_reason is not None:
            # Idempotent by state, as on the server: nothing written.
            return ExclusionResult(plan_id=plan_id)
        # As on the server, a COMPLETED member blocks nobody: the cascade
        # neither starts from nor passes through one.
        members[task_id].excluded_reason = reason
        excluded_ids = [task_id]
        excluded = (
            set()
            if self.tasks[task_id].status == "completed"
            else {self.member(plan_id, task_id).instance_id}
        )
        changed = True
        while changed:
            changed = False
            for member in members.values():
                if member.excluded_reason is not None:
                    continue
                if self.tasks[member.task_id].status == "completed":
                    continue
                if excluded & set(self.instances[member.instance_id].upstreams):
                    member.excluded_reason = "upstream_excluded"
                    excluded.add(member.instance_id)
                    excluded_ids.append(member.task_id)
                    changed = True
        roots_excluded = sorted(t for t in excluded_ids if members[t].is_root)
        build_failed = False
        if roots_excluded:
            build = self.builds[plan.build_id]
            if build.status not in TERMINAL_BUILD_STATUSES:
                build.status = "failed"
                build.error_message = f"a root was excluded: {message}"
                self.release_build_claims(build)
                build_failed = True
        return ExclusionResult(
            plan_id=plan_id,
            excluded=excluded_ids,
            roots_excluded=roots_excluded,
            build_failed=build_failed,
        )

    def build_skip_blocked(self, build_id: UUID) -> list[str]:
        self._record("build_skip_blocked", build_id=build_id)
        if self.builds[build_id].status in ("cancelled", "completed"):
            return []  # nothing failed: no failure to propagate
        plan = self.active_plan(build_id)
        members = self.members.get(plan.id, {}) if plan is not None else {}
        skipped: list[str] = []
        changed = True
        while changed:
            changed = False
            for member in members.values():
                task = self.tasks[member.task_id]
                if task.status not in ("pending", "suspended", "interrupted"):
                    continue  # the server skips only these
                upstream_statuses = {
                    self.tasks[self.instances[u].task_id].status
                    for u in self.instances[member.instance_id].upstreams
                }
                # The server's blockers: failed, cancelled, skipped.
                if upstream_statuses & {"failed", "cancelled", "skipped"}:
                    self.move(task, "skipped")
                    skipped.append(member.task_id)
                    changed = True
        return skipped
