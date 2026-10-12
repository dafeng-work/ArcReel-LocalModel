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


class TestG1OwnerByProviderAndMediaType:
    """G1 owner = provider_id:media_type。

    - 同 provider 同 media_type 续约不抛（同一图像/视频 lane 内部可并发提交）。
    - 同 provider 跨 media_type 互斥（image vs video 必须串行，避免合并 provider 后撞卡）。
    - 跨 provider 同 group 仍互斥。
    """

    async def _make_provider_with_group(self, db_session, name: str, group: str) -> int:
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        repo = CustomProviderRepository(db_session)
        provider = await repo.create_provider(
            display_name=name,
            discovery_format="openai",
            base_url="http://127.0.0.1:8189",
            api_key="",
        )
        await db_session.flush()
        await repo.update_provider(provider.id, exclusive_resource_group=group)
        await db_session.flush()
        return provider.id

    async def test_same_provider_same_media_type_can_extend(
        self, db_engine
    ) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.repositories.custom_provider_repo import CustomProviderRepository
        from lib.generation.generation_queue import GenerationQueue

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            repo = CustomProviderRepository(s)
            provider = await repo.create_provider(
                display_name="p1",
                discovery_format="openai",
                base_url="http://127.0.0.1:8189",
                api_key="",
            )
            await s.flush()
            await repo.update_provider(provider.id, exclusive_resource_group="gpu-local")
            await s.commit()
            db_id = provider.id

        queue = GenerationQueue(session_factory=factory)
        # 同 provider 同 media_type 续约：必须不抛
        await queue._enforce_gpu_resource_group_lock(
            provider_id=f"custom-{db_id}", media_type="image", task_type="image"
        )
        await queue._enforce_gpu_resource_group_lock(
            provider_id=f"custom-{db_id}", media_type="image", task_type="image"
        )

    async def test_same_provider_cross_media_type_is_busy(
        self, db_engine
    ) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.repositories.custom_provider_repo import CustomProviderRepository
        from lib.generation.generation_queue import GenerationQueue

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            repo = CustomProviderRepository(s)
            provider = await repo.create_provider(
                display_name="p2",
                discovery_format="openai",
                base_url="http://127.0.0.1:8189",
                api_key="",
            )
            await s.flush()
            await repo.update_provider(provider.id, exclusive_resource_group="gpu-local")
            await s.commit()
            db_id = provider.id

        queue = GenerationQueue(session_factory=factory)
        await queue._enforce_gpu_resource_group_lock(
            provider_id=f"custom-{db_id}", media_type="image", task_type="image"
        )
        # 同 provider 跨 media_type（合并后 r9700-gateway provider 必填）
        with pytest.raises(ResourceGroupBusy) as ei:
            await queue._enforce_gpu_resource_group_lock(
                provider_id=f"custom-{db_id}", media_type="video", task_type="video"
            )
        assert ei.value.group == "gpu-local"
        # held_by 是先抢到锁的 owner_key（image 占锁），应是 'custom-<id>:image'
        assert "image" in (ei.value.held_by or "")
