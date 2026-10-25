"""G1（资源组互斥）单测：custom_provider.exclusive_resource_group + worker_lease 抢锁。

测试范围：
- provider 设置 exclusive_resource_group 后能读回；
- 同 group 的不同 provider 在 image / video lane 上互斥串行；
- text lane 不受 G1 锁约束（LLM 子任务并发不受限）；
- null group 维持原行为（不抢锁，不抛异常）。
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.base import utc_now
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

    async def test_same_provider_same_media_type_can_extend(self, db_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
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
        # 续约后 lease 持锁者仍是自己（owner_id 形如 "<provider>:<media_type>"）。
        async with factory() as s:
            lease_row = await s.get(WorkerLease, "group:gpu-local")
            assert lease_row is not None
            assert lease_row.owner_id == f"custom-{db_id}:image"

    async def test_same_provider_cross_media_type_is_busy(self, db_engine) -> None:
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


async def _make_grouped_provider(db_engine, name: str = "p-gpu", group: str = "gpu-local") -> int:
    """建一个配了 exclusive_resource_group 的自定义 provider，返回其 db id。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lib.db.repositories.custom_provider_repo import CustomProviderRepository

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = CustomProviderRepository(s)
        provider = await repo.create_provider(
            display_name=name,
            discovery_format="openai",
            base_url="http://127.0.0.1:8189",
            api_key="",
        )
        await s.flush()
        await repo.update_provider(provider.id, exclusive_resource_group=group)
        await s.commit()
        return provider.id


async def _enqueue_lane_task(db_engine, *, provider_id: str, media_type: str, resource_id: str) -> str:
    """在给定 lane 上落一条 queued 任务行，返回 task_id。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lib.db.repositories.task_repo import TaskRepository

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = TaskRepository(s)
        task = await repo.enqueue(
            project_name="demo",
            task_type="storyboard" if media_type == "image" else "video",
            media_type=media_type,
            resource_id=resource_id,
            payload={},
            provider_id=provider_id,
        )
        await s.commit()
        return str(task["task_id"])


class TestG1LockReleasedOnTerminal:
    """G1 锁的生命周期对齐真实负载。

    锁在**入队时**抢，而任务终态不释放、只靠 600s TTL 过期：持锁任务早已跑完，锁却还挂着，
    另一个 lane（image ↔ video）入队被 409 挡在门外，提示「GPU 正被 image 占用」——与实况不符。
    这里锁定的是：持锁 lane 的任务走到终态、且该 lane 再无排队/执行中任务时，锁被释放。
    """

    async def test_terminal_task_releases_group_lock(self, db_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue

        db_id = await _make_grouped_provider(db_engine)
        provider_id = f"custom-{db_id}"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        await queue._enforce_gpu_resource_group_lock(
            provider_id=provider_id, media_type="image", task_type="storyboard"
        )
        task_id = await _enqueue_lane_task(db_engine, provider_id=provider_id, media_type="image", resource_id="E1S01")

        claimed = await queue.claim_next_task(media_type="image")
        assert claimed is not None
        assert claimed["task_id"] == task_id
        await queue.mark_task_succeeded(task_id, {"ok": True})

        async with factory() as s:
            assert await s.get(WorkerLease, "group:gpu-local") is None
        # 锁释放后，另一个 lane 不再被 409 挡在门外
        await queue._enforce_gpu_resource_group_lock(provider_id=provider_id, media_type="video", task_type="video")

    async def test_sibling_queued_task_keeps_group_lock(self, db_engine) -> None:
        """同 lane 还有任务排队时锁必须留着——那些任务还没跑，仍要占那张卡。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue, ResourceGroupBusy

        db_id = await _make_grouped_provider(db_engine)
        provider_id = f"custom-{db_id}"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        await queue._enforce_gpu_resource_group_lock(
            provider_id=provider_id, media_type="image", task_type="storyboard"
        )
        first = await _enqueue_lane_task(db_engine, provider_id=provider_id, media_type="image", resource_id="E1S01")
        await _enqueue_lane_task(db_engine, provider_id=provider_id, media_type="image", resource_id="E1S02")

        claimed = await queue.claim_next_task(media_type="image")
        assert claimed is not None
        assert claimed["task_id"] == first
        await queue.mark_task_succeeded(first, {"ok": True})

        async with factory() as s:
            lease = await s.get(WorkerLease, "group:gpu-local")
            assert lease is not None
            assert lease.owner_id == f"{provider_id}:image"
        with pytest.raises(ResourceGroupBusy):
            await queue._enforce_gpu_resource_group_lock(provider_id=provider_id, media_type="video", task_type="video")

    async def test_text_lane_terminal_does_not_touch_group_lock(self, db_engine) -> None:
        """text lane 不受 G1 约束，其任务终态不得动 image/video 的组锁。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue

        db_id = await _make_grouped_provider(db_engine)
        provider_id = f"custom-{db_id}"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        await queue._enforce_gpu_resource_group_lock(
            provider_id=provider_id, media_type="image", task_type="storyboard"
        )
        text_task = await _enqueue_lane_task(
            db_engine, provider_id=provider_id, media_type="text", resource_id="episode-1"
        )
        claimed = await queue.claim_next_task(media_type="text")
        assert claimed is not None
        assert claimed["task_id"] == text_task
        await queue.mark_task_succeeded(text_task, {"ok": True})

        async with factory() as s:
            lease = await s.get(WorkerLease, "group:gpu-local")
            assert lease is not None
            assert lease.owner_id == f"{provider_id}:image"


class TestG1OrphanLockRecovery:
    """worker 崩溃 / SIGKILL 后留下的孤儿组锁，在启动自愈时回收。"""

    async def test_orphan_lock_released_when_owner_has_no_active_task(self, db_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue

        db_id = await _make_grouped_provider(db_engine)
        provider_id = f"custom-{db_id}"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        await queue._enforce_gpu_resource_group_lock(
            provider_id=provider_id, media_type="image", task_type="storyboard"
        )
        async with factory() as s:
            assert await s.get(WorkerLease, "group:gpu-local") is not None

        assert await queue.release_orphan_gpu_resource_group_locks() == 1
        async with factory() as s:
            assert await s.get(WorkerLease, "group:gpu-local") is None

    async def test_orphan_lock_kept_when_owner_still_has_active_task(self, db_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue

        db_id = await _make_grouped_provider(db_engine)
        provider_id = f"custom-{db_id}"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        await queue._enforce_gpu_resource_group_lock(
            provider_id=provider_id, media_type="image", task_type="storyboard"
        )
        await _enqueue_lane_task(db_engine, provider_id=provider_id, media_type="image", resource_id="E1S01")

        assert await queue.release_orphan_gpu_resource_group_locks() == 0
        async with factory() as s:
            assert await s.get(WorkerLease, "group:gpu-local") is not None

    async def test_worker_heartbeat_lease_is_untouched(self, db_engine) -> None:
        """worker 自己的心跳租约（name='default'）不在孤儿回收范围内。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from lib.db.models.task import WorkerLease
        from lib.generation.generation_queue import GenerationQueue

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        queue = GenerationQueue(session_factory=factory)

        async with factory() as s:
            s.add(
                WorkerLease(
                    name="default",
                    owner_id="worker-test",
                    lease_until=time.time() + 600,
                    updated_at=utc_now(),
                )
            )
            await s.commit()

        assert await queue.release_orphan_gpu_resource_group_locks() == 0
        async with factory() as s:
            assert await s.get(WorkerLease, "default") is not None
