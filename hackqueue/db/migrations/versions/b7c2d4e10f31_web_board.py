"""web board opt-in + cached member display names

Revision ID: b7c2d4e10f31
Revises: a3e9c1bf798e
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2d4e10f31"
down_revision: str | None = "a3e9c1bf798e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guilds", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("web_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("guild_members", schema=None) as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("avatar_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("display_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("guild_members", schema=None) as batch_op:
        batch_op.drop_column("display_updated_at")
        batch_op.drop_column("avatar_url")
        batch_op.drop_column("display_name")
    with op.batch_alter_table("guilds", schema=None) as batch_op:
        batch_op.drop_column("web_enabled")
