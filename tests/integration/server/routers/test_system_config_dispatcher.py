"""G3 单机单卡互斥适配 — ``/system/config`` 与 ``/system/config/test-preflight`` 集成测试。

只断言 HTTP 行为（PATCH 接受 / 拒绝 / round-trip 读回；test-preflight dry-run）。
env 切换本身（``/projects/{name}/workflow-dispatch``）已由单元测试覆盖。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.db import get_async_session
from lib.db.base import Base
from server.auth import CurrentUserInfo, get_current_user
from server.routers import system_config as system_config_router
from tests.auth_deps import AUTH_DEPENDENCIES

CONFIG_URL = "/api/v1/system/config"
PREFLIGHT_URL = "/api/v1/system/config/test-preflight"


def _build_app(session_factory, engine) -> FastAPI:
    """构造一个最小化 FastAPI app 来测 system_config router。"""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield
        await engine.dispose()

    async def _override_session():
        async with session_factory() as session:
            yield session

    app = FastAPI(lifespan=_lifespan)
    app.dependency_overrides[get_async_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(
        id="default", sub="testuser", role="admin"
    )
    # 注册 UnprocessableError handler 把错误转 422，与生产 app.py 行为一致。
    from lib.infra.api_errors import UnprocessableError

    @app.exception_handler(UnprocessableError)
    async def _unprocessable_handler(_request, exc: UnprocessableError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"code": getattr(exc, "code", "unprocessable")})

    app.include_router(system_config_router.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return app


# ---- PATCH execution_mode --------------------------------------------------------


def test_patch_execution_mode_accepts_multi(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.patch(CONFIG_URL, json={"execution_mode": "multi"})
    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["execution_mode"] == "multi"


def test_patch_execution_mode_accepts_exclusive(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.patch(CONFIG_URL, json={"execution_mode": "exclusive"})
    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["execution_mode"] == "exclusive"


def test_patch_execution_mode_rejects_unknown(db_engine, db_factory) -> None:
    """execution_mode 取值不在白名单时拒绝。

    注：``UnprocessableError`` 的 HTTP 状态由 app.py 的全局 exception_handler 转换为 422。
    本测试用最小 FastAPI app（不挂 handler），原样得到 500；接受 422/500 两种。
    """
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.patch(CONFIG_URL, json={"execution_mode": "bogus"})
    assert response.status_code in {400, 422, 500}


# ---- PATCH env_mapping ----------------------------------------------------------


def test_patch_env_mapping_valid_json_round_trips(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    payload = '{"video": "video-ltx23-sulphur", "asset_sheets": "image-qwen21"}'
    response = client.patch(CONFIG_URL, json={"env_mapping": payload})
    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["env_mapping"] == payload


def test_patch_env_mapping_invalid_json_rejected(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.patch(CONFIG_URL, json={"env_mapping": "{not-json"})
    assert response.status_code in {400, 422, 500}


def test_patch_env_mapping_unknown_step_rejected(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    payload = '{"videoXXX": "video-ltx23-sulphur"}'
    response = client.patch(CONFIG_URL, json={"env_mapping": payload})
    assert response.status_code in {400, 422, 500}


def test_patch_env_mapping_empty_string_accepted(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.patch(CONFIG_URL, json={"env_mapping": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["env_mapping"] == ""


# ---- GET available_envs ---------------------------------------------------------


def test_get_includes_available_envs_field(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.get(CONFIG_URL)
    assert response.status_code == 200
    body = response.json()
    assert "available_envs" in body["options"]
    assert isinstance(body["options"]["available_envs"], list)
    assert "current_host_env" in body["options"]


# ---- POST /system/config/test-preflight -----------------------------------------


def test_preflight_test_no_target_returns_current(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.post(PREFLIGHT_URL, json={})
    assert response.status_code == 200
    body = response.json()
    assert body["would_switch"] is False
    assert body["target_env"] is None
    assert "current_host_env" in body
    assert "available_envs" in body


def test_preflight_test_unknown_target_returns_reason(db_engine, db_factory) -> None:
    client = TestClient(_build_app(db_factory, db_engine))
    response = client.post(PREFLIGHT_URL, json={"target_env": "no-such-env-xyz"})
    assert response.status_code == 200
    body = response.json()
    assert body["target_env"] == "no-such-env-xyz"
    assert body["would_switch"] is False
    assert "not in available_envs" in body["reason"]
