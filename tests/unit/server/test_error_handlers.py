"""app 级异常处理器：领域异常必须映射到可诊断的状态码，而不是通用 500。

**为什么这条要有**：``ResourceGroupBusy`` 是入队时"资源组被别人持锁"的**预期**结果，
不是服务器故障。此前它没有任何 handler，冒泡进 ``@app.exception_handler(Exception)``
变成 500，前端只能显示"服务器内部错误，请稍后重试"——用户既看不到"GPU 正被占用"
这个可操作的事实，也不知道本次提交**未入队、未计费**，于是反复重试。

2026-10-19 实测：单卡 R9700 上跑视频时点"生成视频"连续 5 次 500，每次日志都写着
``resource group 'gpu-local' is busy (held by custom-1:image)``。

本文件钉死三件事：
1. ``ResourceGroupBusy`` → 409，且文案说清资源组、占用方与"未入队未计费"；
2. 占用方是 ``provider_id:media_type``，供应商按显示名称呼，目录里认不出时保留 ID
   而不是谎称"已删除的供应商"；
3. ``held_by`` 缺失（读不到 lease owner）时同样是 409，不能因文案缺参数退化成裸键。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.generation.generation_queue import ResourceGroupBusy
from lib.i18n.display_names import DisplayNames
from server.error_handlers import register_error_handlers


@pytest.fixture
def client_for(monkeypatch):
    """返回一个工厂：给定要抛的异常与显示名目录，拿到发请求用的 TestClient。"""

    def _build(exc: Exception, names: DisplayNames, *, accept_language: str | None = None) -> TestClient:
        monkeypatch.setattr("server.error_handlers.request_display_names", _async_return(names))
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/boom")
        async def _boom():
            raise exc

        headers = {"accept-language": accept_language} if accept_language else {}
        return TestClient(app, raise_server_exceptions=False, headers=headers)

    return _build


def _async_return(value):
    async def _inner(request):
        return value

    return _inner


def _names(providers: dict[str, str]) -> DisplayNames:
    return DisplayNames(providers=providers, models={}, deleted_provider="已删除的供应商")


def test_resource_group_busy_is_409_not_500(client_for):
    client = client_for(ResourceGroupBusy(group="gpu-local", held_by="custom-1:image"), _names({}))

    response = client.get("/boom")

    assert response.status_code == 409


def test_detail_states_group_holder_and_no_queue_no_charge(client_for):
    client = client_for(ResourceGroupBusy(group="gpu-local", held_by="custom-1:image"), _names({}))

    detail = client.get("/boom").json()["detail"]

    assert "gpu-local" in detail
    assert "custom-1" in detail
    assert "未入队" in detail
    assert "未计费" in detail


def test_known_provider_is_called_by_display_name(client_for):
    client = client_for(
        ResourceGroupBusy(group="gpu-local", held_by="custom-1:image"),
        _names({"custom-1": "本机 ComfyUI"}),
    )

    detail = client.get("/boom").json()["detail"]

    assert "本机 ComfyUI" in detail
    assert "custom-1" not in detail


def test_unknown_provider_keeps_id_instead_of_claiming_it_was_deleted(client_for):
    """目录查不到时不能套 ``DisplayNames.provider()`` 的"已删除的供应商"——那是在撒谎。"""
    client = client_for(ResourceGroupBusy(group="gpu-local", held_by="custom-1:image"), _names({}))

    detail = client.get("/boom").json()["detail"]

    assert "custom-1" in detail
    assert "已删除的供应商" not in detail


def test_missing_holder_still_409_and_still_names_the_group(client_for):
    """lease owner 读不到（``held_by=None``）时文案不能因缺参数退化成裸键。"""
    client = client_for(ResourceGroupBusy(group="gpu-local", held_by=None), _names({}))

    response = client.get("/boom")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "gpu-local" in detail
    assert "{" not in detail


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_translations_render_without_leaving_raw_placeholders(client_for, locale):
    """三种语言都必须有这条文案；缺键会让 ``_()`` 回退成键名本身，响应里就会看到裸键。"""
    client = client_for(
        ResourceGroupBusy(group="gpu-local", held_by="custom-1:image"),
        _names({}),
        accept_language=locale,
    )

    response = client.get("/boom")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "resource_group_busy" not in detail
    assert "{" not in detail
    assert "gpu-local" in detail
