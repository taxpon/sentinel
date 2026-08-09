"""Give `poller_lag_seconds` a source: the poller_heartbeat table.

Revision ID: a1b7c93d4e21
Revises: d76e40f7bd73
Create Date: 2026-08-09 03:10:00.000000

One row, holding when the poller last completed a tick. See the docstring on
`sentinel.models.PollerHeartbeat` for why this is a table of its own rather than a column on
`remediation`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b7c93d4e21"
down_revision: str | Sequence[str] | None = "d76e40f7bd73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "poller_heartbeat",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("ticked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poller_heartbeat")),
    )


def downgrade() -> None:
    op.drop_table("poller_heartbeat")
