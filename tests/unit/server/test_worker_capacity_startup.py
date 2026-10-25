"""启动时必须从 DB 装载 worker 容量表，否则自定义供应商落进"未知 provider"懒默认。

**为什么这条要有**：``GenerationWorker.__init__`` 用 ``CapacityTable.from_env()`` 建表，
那张表只有内置注册表里的 provider，**没有 ``custom-*`` 条目**。``CapacityTable.get``
遇到未知 provider 会回退 ``_defaults``（视频 3、图像 5）——那是给"provider 已被删除"
的兜底，不是给"provider 存在但没被装载"的兜底。

于是自定义 ComfyUI 供应商（本机单卡）拿到视频并发 3 而不是 1，多个视频任务同时抢
同一张卡：2026-10-19 实际打爆显存（一个任务 CUDA OOM 失败，随后 ComfyUI 在文本
编码阶段 fatal abort 整个服务）。

DB 装载路径本身是对的（``CapacityTable.from_db`` 对 comfyui 协议、并发列为 NULL 的
自定义供应商给每条 lane 1，见
``tests/integration/lib/generation/test_generation_worker_capacity_table.py``），
缺的是**启动时把它调一次**——``reload_limits`` 此前只在 Web 端改供应商配置时触发。

本文件钉死两件事：
1. lifespan 启动序列会调 ``reload_limits``，且在 ``worker.start()`` 之前；
2. 启动完成后，容量表里自定义供应商的 lane 上限来自 DB，而不是 env 懒默认。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from lib.generation.generation_worker import CapacityTable
from lib.project.project_manager import ProjectManager


def _patch_startup_side_effects(monkeypatch, tmp_path):
    """把 lifespan 里与本用例无关的长跑副作用全部挖掉。"""
    monkeypatch.setenv("ARCREEL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("lib.project.project_manager.get_project_manager", lambda: ProjectManager(tmp_path))
    monkeypatch.setattr(
        "server.app.migrate_local_transcripts_to_store",
        AsyncMock(return_value={"imported": 0, "skipped": 0, "failed": 0}),
    )
    monkeypatch.setattr("server.app.init_db", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "server.app.run_project_migrations",
        lambda *args, **kwargs: type("M", (), {"migrated": [], "failed": [], "skipped": []})(),
    )
    monkeypatch.setattr("server.app.cleanup_stale_backups", lambda *args, **kwargs: None)
    monkeypatch.setattr("server.app.startup_http_client", AsyncMock(return_value=None))
    monkeypatch.setattr("server.app.shutdown_http_client", AsyncMock(return_value=None))
    monkeypatch.setattr("server.app.close_db", AsyncMock(return_value=None))
    monkeypatch.setattr("server.app.assistant.assistant_service", _fake_assistant_service())
    monkeypatch.setattr(
        "server.app.ProjectEventService",
        lambda *args, **kwargs: _fake_project_event_service(),
    )


def _fake_assistant_service():
    svc = type("S", (), {})()
    svc.startup = AsyncMock()
    svc.session_manager = type(
        "SM",
        (),
        {
            "start_patrol": lambda self: None,
            "stop_patrol": lambda self: None,
            "set_autonomous_turn_listener": lambda self, listener: None,
        },
    )()
    return svc


def _fake_project_event_service():
    pes = type("P", (), {})()
    pes.start = AsyncMock()
    pes.shutdown = AsyncMock()
    pes.publish_assistant_session_resumed = lambda self, project_name, session_id: None
    return pes


@pytest.mark.asyncio
async def test_lifespan_loads_capacity_from_db_before_worker_starts(tmp_path, monkeypatch):
    """lifespan 必须先 ``reload_limits`` 再 ``start``：认领任务前容量表得是 DB 的。

    顺序反了的话，worker 可能在容量表还是 env 默认（视频 3）时就已经认领并提交了
    第一个任务——单卡上这一步就会并发抢显存。
    """
    _patch_startup_side_effects(monkeypatch, tmp_path)

    calls: list[str] = []
    worker = type(
        "W",
        (),
        {
            "reload_limits": AsyncMock(side_effect=lambda: calls.append("reload_limits")),
            "start": AsyncMock(side_effect=lambda: calls.append("start")),
            "stop": AsyncMock(),
        },
    )()

    with patch("server.app.create_generation_worker", return_value=worker):
        from server.app import app, lifespan

        async with lifespan(app):
            pass

    worker.reload_limits.assert_awaited_once()
    assert calls == ["reload_limits", "start"], f"启动顺序错：{calls}"


@pytest.mark.asyncio
async def test_custom_provider_lane_limit_comes_from_db_at_startup(tmp_path, monkeypatch):
    """启动完成后，自定义 ComfyUI 供应商的视频 lane 上限必须是 DB 里的 1，不是 env 默认的 3。"""
    _patch_startup_side_effects(monkeypatch, tmp_path)

    db_table = CapacityTable(
        _limits={"custom-9": {"image": 1, "video": 1, "audio": 0}},
        _defaults={"image": 5, "video": 3, "audio": 10},
    )
    monkeypatch.setattr(
        "lib.generation.generation_worker.CapacityTable.from_db",
        AsyncMock(return_value=db_table),
    )
    # worker.start() 会起主循环抢 worker lease；本用例只关心容量表，挖掉它。
    monkeypatch.setattr("lib.generation.generation_worker.GenerationWorker.start", AsyncMock())
    monkeypatch.setattr("lib.generation.generation_worker.GenerationWorker.stop", AsyncMock())

    from server.app import app, lifespan

    async with lifespan(app):
        worker = app.state.generation_worker
        # 容量表必须在 worker 起来之前就位——断言此刻已经是 DB 的数字。
        assert worker._capacity.get("custom-9", "video") == 1
        assert worker._capacity.get("custom-9", "image") == 1
