"""create tasks table

Revision ID: 5b1fb3cab891
Revises:
Create Date: 2025-12-11 20:04:40.936642

"""

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "5b1fb3cab891"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    file_text = Path(__file__).with_suffix(".sql").read_text()
    if file_text:
        op.execute(file_text)
