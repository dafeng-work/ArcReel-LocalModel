"""G4（文本 env 前置闸门）单测：custom_provider.env_name 读写 + 在跑任务统计。

测试范围：
- provider 不设 env_name 时读回 None（未启用该特性的部署的默认值，零回归开关）；
- 设值 / 改值 / 清除（回 None）都能往返；
- 不存在的 provider 读回 None 而不是抛错；
- count_active_gpu_tasks 按 status + media_type 计数，text lane 不计入。
"""

from __future__ import annotations

from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.db.repositories.task_repo import TaskRepository


class TestEnvNameField:
    async def test_default_null_env_name(self, db_session: AsyncSession):
        """NULL = 现状行为（完全不感知 env）。这是所有现存 provider 的默认值。"""
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="NoEnv",
            discovery_format="openai",
            base_url="http://127.0.0.1:8080/v1",
            api_key="",
        )
        await db_session.flush()
        assert await repo.get_env_name(provider.id) is None

    async def test_create_with_env_name(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="Text Gateway",
            discovery_format="openai",
            base_url="http://127.0.0.1:8080/v1",
            api_key="",
            env_name="wide",
        )
        await db_session.flush()
        assert await repo.get_env_name(provider.id) == "wide"

    async def test_set_and_get_env_name(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="Bound",
            discovery_format="openai",
            base_url="http://127.0.0.1:8080/v1",
            api_key="",
        )
        await db_session.flush()
        await repo.update_provider(provider.id, env_name="nsfw")
        await db_session.flush()
        assert await repo.get_env_name(provider.id) == "nsfw"

        # 改绑
        await repo.update_provider(provider.id, env_name="wide")
        await db_session.flush()
        assert await repo.get_env_name(provider.id) == "wide"

    async def test_clear_env_name(self, db_session: AsyncSession):
        """PUT 全量提交：None 表示清除绑定，回到零回归旁路状态。"""
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="Cleared",
            discovery_format="openai",
            base_url="http://127.0.0.1:8080/v1",
            api_key="",
            env_name="wide",
        )
        await db_session.flush()
        await repo.update_provider(provider.id, env_name=None)
        await db_session.flush()
        assert await repo.get_env_name(provider.id) is None

    async def test_get_env_name_nonexistent_provider(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        assert await repo.get_env_name(99999) is None


class TestCountActiveGpuTasks:
    """在跑任务统计：决定 env 闸门是自动切还是快速失败。"""

    async def test_empty_queue_counts_zero(self, db_session: AsyncSession):
        assert await TaskRepository(db_session).count_active_gpu_tasks() == 0

    async def test_gpu_media_types_exclude_text(self, db_session: AsyncSession):
        """text lane 刻意不计入：否则并行文本任务会互相挡路。"""
        repo = TaskRepository(db_session)
        await _insert_task(db_session, task_type="text_something", media_type="text", status="running")
        assert await repo.count_active_gpu_tasks() == 0

    async def test_counts_running_and_queued_image_video_audio(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        await _insert_task(db_session, task_type="image_gen", media_type="image", status="running")
        await _insert_task(db_session, task_type="video_gen", media_type="video", status="queued")
        await _insert_task(db_session, task_type="tts", media_type="audio", status="running")

        assert await TaskRepository(db_session).count_active_gpu_tasks() == 3

    async def test_terminal_statuses_not_counted(self, db_session: AsyncSession):
        for status in ("succeeded", "failed", "cancelled"):
            await _insert_task(db_session, task_type="image_gen", media_type="image", status=status)

        assert await TaskRepository(db_session).count_active_gpu_tasks() == 0


async def _insert_task(
    session: AsyncSession,
    *,
    task_type: str,
    media_type: str,
    status: str,
    project_name: str = "demo",
) -> None:
    """直写一行 tasks —— 只喂本测试关心的列，其余交给列自身默认值。"""
    import uuid
    from datetime import datetime

    from lib.db.models.task import Task

    now = datetime.now(UTC)
    session.add(
        Task(
            task_id=uuid.uuid4().hex,
            project_name=project_name,
            task_type=task_type,
            media_type=media_type,
            resource_id=uuid.uuid4().hex,
            status=status,
            source="test",
            queued_at=now,
            updated_at=now,
        )
    )
    await session.flush()
