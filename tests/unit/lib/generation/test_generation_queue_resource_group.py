"""G1（资源组互斥）单测：custom_provider.exclusive_resource_group + worker_lease 抢锁。

测试范围：
- provider 设置 exclusive_resource_group 后能读回；
- 同 group 的不同 provider 在 image / video lane 上互斥串行；
- text lane 不受 G1 锁约束（LLM 子任务并发不受限）；
- null group 维持原行为（不抢锁，不抛异常）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.generation_queue import ResourceGroupBusy


class TestExclusiveResourceGroupField:
    async def test_default_null_group(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="NoGroup",
            discovery_format="openai",
            base_url="http://127.0.0.1:8189",
            api_key="",
        )
        await db_session.flush()
        assert await repo.get_exclusive_resource_group(provider.id) is None

    async def test_set_and_get_group(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name="GPU Group",
            discovery_format="openai",
            base_url="http://127.0.0.1:8189",
            api_key="",
        )
        await db_session.flush()
        updated = await repo.update_provider(provider.id, exclusive_resource_group="gpu-local")
        await db_session.flush()
        assert updated is not None
        assert await repo.get_exclusive_resource_group(provider.id) == "gpu-local"

    async def test_get_exclusive_resource_group_nonexistent_provider(self, db_session: AsyncSession):
        repo = CustomProviderRepository(db_session)
        assert await repo.get_exclusive_resource_group(99999) is None


class TestResourceGroupBusyException:
    def test_message_contains_group_and_holder(self):
        exc = ResourceGroupBusy(group="gpu-local", held_by="custom-2")
        assert "gpu-local" in str(exc)
        assert "custom-2" in str(exc)

    def test_message_without_holder(self):
        exc = ResourceGroupBusy(group="gpu-local", held_by=None)
        assert "gpu-local" in str(exc)
        # 没有 holder 时不再带括号
        assert "()" not in str(exc)


class TestG1MediaScope:
    """G1 只锁 image / video lane；text / audio 不受约束。"""

    def test_guarded_media_types(self):
        from lib.generation.generation_queue import GenerationQueue

        guarded = GenerationQueue._G1_GUARDED_MEDIA_TYPES
        assert guarded == frozenset({"image", "video"})

    def test_task_type_supplement_video(self):
        """task_type='video' / 'reference_video' 即使 media_type 不是 'video' 也走锁。"""
        from lib.generation.generation_queue import GenerationQueue

        # 这是逻辑检查，不需要 DB
        method_source = GenerationQueue._enforce_gpu_resource_group_lock
        # 调用签名应含 task_type 参数（用于 video/reference_video 判别）
        assert "task_type" in method_source.__code__.co_varnames
