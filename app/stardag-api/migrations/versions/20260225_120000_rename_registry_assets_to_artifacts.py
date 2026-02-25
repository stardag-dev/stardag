"""rename registry assets to artifacts

Revision ID: acbdcf130fb2
Revises: 941cd8b749c7
Create Date: 2026-02-25 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "acbdcf130fb2"
down_revision: Union[str, None] = "941cd8b749c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename the table
    op.rename_table("task_registry_assets", "task_artifacts")

    # Rename the column
    op.alter_column(
        "task_artifacts",
        "asset_type",
        new_column_name="artifact_type",
    )

    # Drop old indexes and constraints, create new ones with updated names
    op.drop_constraint(
        "uq_task_registry_asset_task_type_name",
        "task_artifacts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_task_artifact_task_type_name",
        "task_artifacts",
        ["task_pk", "artifact_type", "name"],
    )

    op.drop_index("ix_task_registry_assets_task_pk", table_name="task_artifacts")
    op.create_index(
        "ix_task_artifacts_task_pk",
        "task_artifacts",
        ["task_pk"],
    )

    op.drop_index("ix_task_registry_assets_environment", table_name="task_artifacts")
    op.create_index(
        "ix_task_artifacts_environment",
        "task_artifacts",
        ["environment_id"],
    )


def downgrade() -> None:
    # Reverse: rename indexes and constraints back
    op.drop_index("ix_task_artifacts_environment", table_name="task_artifacts")
    op.create_index(
        "ix_task_registry_assets_environment",
        "task_artifacts",
        ["environment_id"],
    )

    op.drop_index("ix_task_artifacts_task_pk", table_name="task_artifacts")
    op.create_index(
        "ix_task_registry_assets_task_pk",
        "task_artifacts",
        ["task_pk"],
    )

    op.drop_constraint(
        "uq_task_artifact_task_type_name",
        "task_artifacts",
        type_="unique",
    )
    # Rename column back before creating constraint referencing old name
    op.alter_column(
        "task_artifacts",
        "artifact_type",
        new_column_name="asset_type",
    )
    op.create_unique_constraint(
        "uq_task_registry_asset_task_type_name",
        "task_artifacts",
        ["task_pk", "asset_type", "name"],
    )

    # Rename table back
    op.rename_table("task_artifacts", "task_registry_assets")
