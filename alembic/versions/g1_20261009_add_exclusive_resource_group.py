"""add exclusive_resource_group to custom_provider

Revision ID: g1exclusive_resource_group
Revises: 015359a572fa
Create Date: 2026-10-09 16:45:00.000000

G1（资源组互斥）的可空列迁移：自定义 provider 加 `exclusive_resource_group` 字段。
非空时，提交 image / video 任务前调度层抢 `name = "group:<group>"` 的 worker_lease，
与同 group 的其它 provider 串行（用 worker_lease 命名空间前缀与 worker 心跳隔离）。
NULL = 现状行为，向后兼容。

迁移是不可空改可空回退路径；不需要 backfill。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g1exclusive_resource_group"
down_revision: str | Sequence[str] | None = "015359a572fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加可空列 `exclusive_resource_group` + 索引。SQLite 不支持在 batch_alter_table 里
    直接建索引，拆成两步。"""
    with op.batch_alter_table("custom_provider", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("exclusive_resource_group", sa.String(length=64), nullable=True)
        )
    op.create_index(
        "ix_custom_provider_exclusive_resource_group",
        "custom_provider",
        ["exclusive_resource_group"],
    )


def downgrade() -> None:
    """回退：删索引 + 列。"""
    op.drop_index("ix_custom_provider_exclusive_resource_group", table_name="custom_provider")
    with op.batch_alter_table("custom_provider", schema=None) as batch_op:
        batch_op.drop_column("exclusive_resource_group")
