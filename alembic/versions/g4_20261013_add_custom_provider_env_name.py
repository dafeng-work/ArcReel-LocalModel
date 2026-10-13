"""add env_name to custom_provider

Revision ID: g4custom_provider_env_name
Revises: g1exclusive_resource_group
Create Date: 2026-10-13 15:05:00.000000

G4（文本 env 前置闸门）的可空列迁移：自定义 provider 加 `env_name` 字段。
非空时，本 provider 由宿主该 env 提供——发文本请求前先确认 `run/current.env` 与此一致，
不一致则经 `arcreel-env-gate.sh` 切换（空闲时）或快速失败并提示（有在跑任务时）。

与 G1 的 `exclusive_resource_group` 分工：那个字段表达「若干 provider 共享一块独占资源」
（并发互斥），这个字段表达「provider 归哪个 env 管」（可用性）。二者正交，可同时非空。

NULL = 现状行为（完全不感知 env），是所有现存 provider 的默认值，保证零回归。
不需要 backfill。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g4custom_provider_env_name"
down_revision: str | Sequence[str] | None = "g1exclusive_resource_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加可空列 `env_name` + 索引。SQLite 不支持在 batch_alter_table 里直接建索引，拆成两步。"""
    with op.batch_alter_table("custom_provider", schema=None) as batch_op:
        batch_op.add_column(sa.Column("env_name", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_custom_provider_env_name",
        "custom_provider",
        ["env_name"],
    )


def downgrade() -> None:
    """回退：删索引 + 列。"""
    op.drop_index("ix_custom_provider_env_name", table_name="custom_provider")
    with op.batch_alter_table("custom_provider", schema=None) as batch_op:
        batch_op.drop_column("env_name")
