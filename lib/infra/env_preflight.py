"""工作流步骤级 env 前置闸门（G3）—— 仅适配本机单卡多模型互斥场景。

调用方传入目标 env 名（如 ``video-ltx23-sulphur``），本模块负责：

1. 读宿主机 ``/home/dafeng/ai-stack/run/current.env``，若已在目标 env 直接返回。
2. 否则 spawn 宿主机的 ``arcreel-env-gate.sh``，由它负责切 env + 探测就绪 + 放行。
3. 等回 0 视为切换完成；非零 / 超时抛结构化错误给上层。

**显式不做通用化**：不接受任意命令 / 任意 URL；切换入口只有 ``arcreel-env-gate.sh``
一条，env 名白名单由 ``lib.config.dispatcher_schema`` 提供。

**为什么住在 lib 而不是 server**：文本调用链的收敛点 ``lib/backends/text_generator.py``
也需要这套闸门（G4：文本模型 env 预检），而 ``lib`` 对 ``server`` 零导入。反向依赖会把
分层打破，故实现下沉至此，``server`` 侧各模块改为从这里导入。

鉴权：r9700-gateway 自 2026-10-10 起对所有端点强制 Bearer（见 ``arcreel-env-gate.sh``
同段注释）。探活请求不带 token 会拿到 401 而非 200，``ready_probe`` 因此永远判不出就绪。
``gateway_auth_token`` 复刻闸门脚本的取值顺序：环境变量优先，回退读 ``env/common.env``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from lib.infra.httpx_shared import get_http_client

logger = logging.getLogger(__name__)


#: 宿主机 env 状态文件路径。容器内须经 ``deploy/docker-compose.yml`` 的 bind mount
#: 暴露 ``/home/dafeng/ai-stack``（AGENTS.md「部署适配约定」）。
DEFAULT_CURRENT_ENV_PATH: Final[Path] = Path("/home/dafeng/ai-stack/run/current.env")

#: env-gate 闸门脚本绝对路径。bind mount 缺失时（即文件不存在）``ensure_env`` 抛
#: :class:`EnvSwitchGateMissing` 附带绝对路径，便于排查。
DEFAULT_GATE_SCRIPT: Final[Path] = Path("/home/dafeng/ai-stack/bin/arcreel-env-gate.sh")

#: env-gate 子进程超时（秒）。与 ``arcreel-env-gate.sh`` 的 ``GATE_TIMEOUT=150`` 默认值
#: 对齐 —— 留 5s 富余给 asyncio.wait_for 兜底。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 155.0

#: 切换日志目录。与宿主机 ``r9700-env`` 共享 ``$STACK_ROOT/logs``；容器内同样须 bind mount。
DEFAULT_LOG_DIR: Final[Path] = Path("/home/dafeng/ai-stack/logs")

#: 网关统一入口。ComfyUI 类 env 走 ``/system_stats``，LLM 类 env 走 ``/v1/models``。
GATEWAY_BASE_URL: Final[str] = "http://127.0.0.1:8080"

#: 探活端点。**按目标 env 类别二选一**（见 :func:`probe_urls_for_env`），不同时探两条。
COMFYUI_PROBE_URL: Final[str] = f"{GATEWAY_BASE_URL}/system_stats"
LLM_PROBE_URL: Final[str] = f"{GATEWAY_BASE_URL}/v1/models"

#: 兼容保留：env 类别未知时的兜底探测集合（两条都探，任一 200 即就绪）。
DEFAULT_READY_PROBE_URLS: Final[tuple[str, ...]] = (LLM_PROBE_URL, COMFYUI_PROBE_URL)

#: 网关鉴权 token 的环境变量名与 common.env 位置。与 ``arcreel-env-gate.sh`` 同源同语义。
GATEWAY_TOKEN_ENV_KEY: Final[str] = "R9700_API_KEY"
DEFAULT_STACK_ENV_FILE: Final[Path] = Path("/home/dafeng/ai-stack/env/common.env")

#: ready_probe 单次 HTTP 调用超时。
READY_PROBE_HTTP_TIMEOUT_SECONDS: Final[float] = 4.0

#: ready_probe 重试次数（默认 30 × 5s = 150s 上限；与 env-gate GATE_TIMEOUT 一致）。
READY_PROBE_MAX_ATTEMPTS: Final[int] = 30
READY_PROBE_INTERVAL_SECONDS: Final[float] = 5.0

#: env 类别 → 探活端点。分类与 ``arcreel-env-gate.sh`` 的 ``probe_for_env`` 保持一致。
#:
#: 之所以必须分类而不能「任一 200 即就绪」：单机上 ComfyUI 与 LLM 是两套互斥 env，但网关
#: 两条端点同时活着是常态（视频 env 起来时 ``/system_stats`` 就是 200 而 ``/v1/models`` 是
#: 502）。按「任一 200」判定会把「LLM 未就绪」误判成就绪 —— 正是本缺陷要修的那类误判。
_LLM_ENVS: Final[frozenset[str]] = frozenset({"fast", "novel", "story", "nsfw", "nsfw-moe", "wide"})
_COMFYUI_ENVS: Final[frozenset[str]] = frozenset(
    {
        "video-fast",
        "video-hq",
        "video-nsfw",
        "video-s2v",
        "video-ltx23",
        "video-ltx23-dev",
        "video-ltx23-sulphur",
        "video-ltx25",
        "video-ltx25-uc",
        "image-qwen21",
        "qwenimage",
    }
)


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class EnvSwitchError(RuntimeError):
    """env 切换前置闸门失败。"""


class EnvSwitchFailed(EnvSwitchError):
    """env-gate 子进程退出码非零；附带目标 env、退出码与 stderr 末尾以便诊断。"""

    def __init__(self, target_env: str, exit_code: int, stderr_tail: str, log_path: str | None = None) -> None:
        self.target_env = target_env
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        self.log_path = log_path
        super().__init__(
            f"arcreel-env-gate.sh exited with code {exit_code} while switching to {target_env!r}: "
            f"{stderr_tail[-200:] if stderr_tail else '(no stderr)'}"
        )


class EnvSwitchTimeout(EnvSwitchError):
    """env-gate 子进程在 deadline 内未返回；附带目标 env 与超时秒数。"""

    def __init__(self, target_env: str, timeout_seconds: float) -> None:
        self.target_env = target_env
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"arcreel-env-gate.sh did not finish within {timeout_seconds:.0f}s while switching to {target_env!r}"
        )


class EnvSwitchGateMissing(EnvSwitchError):
    """env-gate 脚本不存在 —— 容器 bind mount 可能未配（AGENTS.md「部署适配约定」）。"""

    def __init__(self, gate_path: Path) -> None:
        self.gate_path = gate_path
        super().__init__(
            f"env-gate script not found at {gate_path}; "
            f"check that /home/dafeng/ai-stack is bind-mounted into the ArcReel container"
        )


@dataclass(frozen=True)
class EnvSwitchResult:
    """ensure_env 的成功回报，便于上游做日志/统计。"""

    target_env: str
    switched: bool  # False = 已在目标 env；True = 真正切了一次
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# 模块级并发互斥（计划书 §State, Ownership, Concurrency）
# ---------------------------------------------------------------------------

#: 进程内 asyncio 单实例锁 —— 同一时刻只能有一个 ensure_env 在跑；第二个进入者等锁并打印
#: 「另一项切换进行中」日志。多 fork worker（本机没有，未来可能）不在此锁范围。
_SWITCH_LOCK: Final[asyncio.Lock] = asyncio.Lock()

#: 持有锁的协程上报「我在切哪个 env / 何时开始」，便于第二个进入者日志。
_IN_FLIGHT: dict[str, float] = {}


# ---------------------------------------------------------------------------
# 当前 env 读取
# ---------------------------------------------------------------------------


def read_current_host_env(current_env_path: Path) -> str:
    """读 current.env。文件不存在或读不动时返回 ``"(unknown)"`` —— 强制切到目标 env。"""

    try:
        return current_env_path.read_text().strip()
    except FileNotFoundError:
        return "(unknown)"
    except OSError:
        logger.exception("failed to read %s; treating as unknown", current_env_path)
        return "(unknown)"


# ---------------------------------------------------------------------------
# 探活端点选择与鉴权
# ---------------------------------------------------------------------------


def probe_urls_for_env(target_env: str) -> tuple[str, ...]:
    """按目标 env 类别选探活端点。

    LLM 类 env 只探 ``/v1/models``、ComfyUI 类只探 ``/system_stats``；类别未知时回退到
    两条都探（保持「任一 200 即就绪」的宽松判据，避免对未登记 env 误报失败）。
    """
    env = target_env.strip()
    if env in _LLM_ENVS:
        return (LLM_PROBE_URL,)
    if env in _COMFYUI_ENVS:
        return (COMFYUI_PROBE_URL,)
    logger.warning("unknown env %r; probing both gateway endpoints", env)
    return DEFAULT_READY_PROBE_URLS


def gateway_auth_token(
    *,
    env_var: str | None = None,
    common_env_path: Path | None = None,
) -> str | None:
    """取网关 Bearer token，取值顺序与 ``arcreel-env-gate.sh`` 一致。

    环境变量优先；回退读 ``env/common.env`` 的 ``R9700_API_KEY=`` 行（值不再 strip 引号，
    与闸门脚本的 ``awk -F=`` 口径相同 —— 该文件里就是裸值）。两者都取不到时返回 None，
    调用方据此保持无头请求，以兼容远端 / 未启用鉴权的网关。
    """
    resolved_env_var = env_var if env_var is not None else globals()["GATEWAY_TOKEN_ENV_KEY"]
    token = os.environ.get(resolved_env_var, "").strip()
    if token:
        return token

    resolved_common = common_env_path if common_env_path is not None else globals()["DEFAULT_STACK_ENV_FILE"]
    try:
        for line in resolved_common.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{resolved_env_var}="):
                value = stripped.split("=", 1)[1].strip()
                if value:
                    return value
    except (FileNotFoundError, OSError):
        logger.debug("could not read gateway token from %s", resolved_common)
    return None


async def _default_probe_ready(urls: tuple[str, ...] = DEFAULT_READY_PROBE_URLS) -> bool:
    """轮询给定探活端点，任一 200 且响应体非空即视为后端就绪。

    带 token 时附加 ``Authorization: Bearer``：网关自 2026-10-10 起对全部端点强制鉴权，
    无头请求恒为 401，会把「已就绪」误判成「未就绪」并白等满 150s。
    """

    client = get_http_client()
    token = gateway_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    if token is None:
        logger.info("ready_probe: no gateway token found; probing without auth header")

    for attempt in range(1, READY_PROBE_MAX_ATTEMPTS + 1):
        for url in urls:
            try:
                response = await client.get(url, timeout=READY_PROBE_HTTP_TIMEOUT_SECONDS, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("ready_probe %s attempt %d failed: %s", url, attempt, exc)
                continue
            # 响应体非空才算真就绪 —— 与 arcreel-env-gate.sh:wait_ready 的语义一致
            if response.status_code == 200 and response.content:
                logger.info("ready_probe %s 200 (attempt %d)", url, attempt)
                return True
            logger.debug("ready_probe %s attempt %d -> %s", url, attempt, response.status_code)
        if attempt < READY_PROBE_MAX_ATTEMPTS:
            await asyncio.sleep(READY_PROBE_INTERVAL_SECONDS)
    return False


# 测试 / 调用方注入的 seam —— 形如 ``AwaitableCallable[[], bool]``。默认走
# :func:`_default_probe_ready`；测试无需 monkey-patch 私有符号。
ProbeReadyFn = typing.Callable[[], typing.Awaitable[bool]]

#: 向后兼容：保留 ``_probe_ready`` 私有名以防现有调用方 / 旧式 monkey-patch 仍引用。
_probe_ready = _default_probe_ready


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def ensure_env(
    target_env: str,
    *,
    current_env_path: Path | None = None,
    gate_script: Path | None = None,
    timeout_seconds: float | None = None,
    log_dir: Path | None = None,
    probe_ready: ProbeReadyFn | None = None,
) -> EnvSwitchResult:
    """确保宿主 env 是 ``target_env``；不在则触发 ``arcreel-env-gate.sh`` 切换并等就绪。

    并发语义：进程内 ``_SWITCH_LOCK`` 串行化；同 env 第二次调用命中 current == target 立即返回。

    各路径类参数缺省值从模块全局查找（而非函数默认参数）—— 这样测试可以通过
    ``monkeypatch.setattr`` 改写模块级常量，而不需要 patch 函数签名本身。
    """

    if not target_env or not target_env.strip():
        raise EnvSwitchError("target_env is empty")

    target = target_env.strip()
    started_at = time.monotonic()

    # 运行时解析路径/超时 —— 用 globals() 直接读模块常量，绕开 Python 默认参数的「导入时绑定」问题。
    resolved_current_path = current_env_path if current_env_path is not None else globals()["DEFAULT_CURRENT_ENV_PATH"]
    resolved_gate_script = gate_script if gate_script is not None else globals()["DEFAULT_GATE_SCRIPT"]
    resolved_timeout = timeout_seconds if timeout_seconds is not None else globals()["DEFAULT_TIMEOUT_SECONDS"]
    resolved_log_dir = log_dir if log_dir is not None else globals()["DEFAULT_LOG_DIR"]

    async with _SWITCH_LOCK:
        current = read_current_host_env(resolved_current_path)
        if current == target:
            logger.info("ensure_env: already at %s (no switch needed)", target)
            return EnvSwitchResult(target_env=target, switched=False, elapsed_seconds=0.0)

        if current != "(unknown)":
            logger.info("ensure_env: current=%s target=%s, switching", current, target)

        if not resolved_gate_script.is_file():
            raise EnvSwitchGateMissing(resolved_gate_script)

        resolved_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = resolved_log_dir / f"env_switch_{int(started_at)}_{target}.log"

        try:
            proc = await asyncio.create_subprocess_exec(
                str(resolved_gate_script),
                target,
                "--timeout",
                str(int(resolved_timeout - 5)),
                "--",
                "true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise EnvSwitchGateMissing(resolved_gate_script) from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=resolved_timeout)
        except TimeoutError as exc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            raise EnvSwitchTimeout(target, resolved_timeout) from exc

        exit_code = proc.returncode if proc.returncode is not None else -1
        stderr_text = stderr.decode(errors="replace") if stderr else ""
        stdout_text = stdout.decode(errors="replace") if stdout else ""

        log_path.write_text(
            f"# arcreel-env-gate.sh {target}\n"
            f"# exit_code={exit_code}\n"
            f"# elapsed={time.monotonic() - started_at:.1f}s\n\n"
            f"--- stdout ---\n{stdout_text}\n\n--- stderr ---\n{stderr_text}\n",
            encoding="utf-8",
        )

        if exit_code != 0:
            raise EnvSwitchFailed(
                target_env=target,
                exit_code=exit_code,
                stderr_tail=stderr_text[-2000:],
                log_path=str(log_path),
            )

        # env-gate 已 wait_ready —— 这里再补一道探测，吸收「env-gate 内部探测通过的瞬间 GPU
        # 又被另一进程抢走」的边缘情况。``probe_ready`` seam 让测试注入替代函数而不需
        # monkey-patch 私有符号。
        resolved_probe_ready = probe_ready if probe_ready is not None else _ready_probe_for(target)
        if not await resolved_probe_ready():
            raise EnvSwitchFailed(
                target_env=target,
                exit_code=2,
                stderr_tail=(
                    f"ready_probe: {READY_PROBE_MAX_ATTEMPTS} attempts × "
                    f"{READY_PROBE_INTERVAL_SECONDS:.0f}s without 200 response"
                ),
                log_path=str(log_path),
            )

    elapsed = time.monotonic() - started_at
    logger.info("ensure_env: switched to %s in %.1fs", target, elapsed)
    return EnvSwitchResult(target_env=target, switched=True, elapsed_seconds=elapsed)


def _ready_probe_for(target_env: str) -> ProbeReadyFn:
    """把 :func:`_default_probe_ready` 绑到目标 env 的探测端点上。"""

    async def _probe() -> bool:
        return await _default_probe_ready(probe_urls_for_env(target_env))

    return _probe


def in_flight_target() -> str | None:
    """给上层（workflow dispatcher）做日志与并发去重辅助；返回当前在切的 env 名。"""

    if not _IN_FLIGHT:
        return None
    target, started_at = next(iter(_IN_FLIGHT.items()))
    if time.monotonic() - started_at > 600:
        # 600s 视作悬挂，避免卡死 —— 实际应由 asyncio 上下文确保正常释放。
        _IN_FLIGHT.pop(target, None)
        return None
    return target


__all__ = [
    "COMFYUI_PROBE_URL",
    "DEFAULT_CURRENT_ENV_PATH",
    "DEFAULT_GATE_SCRIPT",
    "DEFAULT_LOG_DIR",
    "DEFAULT_READY_PROBE_URLS",
    "DEFAULT_STACK_ENV_FILE",
    "DEFAULT_TIMEOUT_SECONDS",
    "GATEWAY_BASE_URL",
    "GATEWAY_TOKEN_ENV_KEY",
    "LLM_PROBE_URL",
    "EnvSwitchError",
    "EnvSwitchFailed",
    "EnvSwitchGateMissing",
    "EnvSwitchResult",
    "EnvSwitchTimeout",
    "ProbeReadyFn",
    "ensure_env",
    "gateway_auth_token",
    "in_flight_target",
    "probe_urls_for_env",
    "read_current_host_env",
]
