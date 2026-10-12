"""工作流步骤级 env 前置闸门（G3）—— 仅适配本机单卡多模型互斥场景。

调用方传入目标 env 名（如 ``video-ltx23-sulphur``），本模块负责：

1. 读宿主机 ``/home/dafeng/ai-stack/run/current.env``，若已在目标 env 直接返回。
2. 否则 spawn 宿主机的 ``arcreel-env-gate.sh``，由它负责切 env + 探测就绪 + 放行。
3. 等回 0 视为切换完成；非零 / 超时抛结构化错误给上层的 workflow dispatcher。

**显式不做通用化**：不接受任意命令 / 任意 URL；切换入口只有 ``arcreel-env-gate.sh``
一条，env 名白名单由 ``lib.config.dispatcher_schema`` 提供。
"""

from __future__ import annotations

import asyncio
import logging
import time
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
#: :class:`EnvSwitchFailed` 附带绝对路径，便于排查。
DEFAULT_GATE_SCRIPT: Final[Path] = Path("/home/dafeng/ai-stack/bin/arcreel-env-gate.sh")

#: env-gate 子进程超时（秒）。与 ``arcreel-env-gate.sh`` 的 ``GATE_TIMEOUT=150`` 默认值
#: 对齐 —— 留 5s 富余给 asyncio.wait_for 兜底。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 155.0

#: 切换日志目录。与宿主机 ``r9700-env`` 共享 ``$STACK_ROOT/logs``；容器内同样须 bind mount。
DEFAULT_LOG_DIR: Final[Path] = Path("/home/dafeng/ai-stack/logs")

#: ready_probe 探测端点。ComfyUI 类 env（image / video）走 ``/system_stats``，LLM 类
#: env 走 ``/v1/models``。两条都跑：先 ``/v1/models`` 后 ``/system_stats``，任意一条 200
#: 即视为就绪。
#:
#: 之所以两条都探：单跑 llama-server 时 ``/system_stats`` 404，单跑 ComfyUI 时
#: ``/v1/models`` 500；让两个探测互为兜底。
DEFAULT_READY_PROBE_URLS: Final[tuple[str, ...]] = (
    "http://127.0.0.1:8080/v1/models",
    "http://127.0.0.1:8080/system_stats",
)

#: ready_probe 单次 HTTP 调用超时。
READY_PROBE_HTTP_TIMEOUT_SECONDS: Final[float] = 4.0

#: ready_probe 重试次数（默认 30 × 5s = 150s 上限；与 env-gate GATE_TIMEOUT 一致）。
READY_PROBE_MAX_ATTEMPTS: Final[int] = 30
READY_PROBE_INTERVAL_SECONDS: Final[float] = 5.0


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
# ready_probe
# ---------------------------------------------------------------------------


async def _probe_ready() -> bool:
    """轮询 ``/v1/models`` 与 ``/system_stats``，任一 200 即视为后端就绪。"""

    client = get_http_client()
    for attempt in range(1, READY_PROBE_MAX_ATTEMPTS + 1):
        for url in DEFAULT_READY_PROBE_URLS:
            try:
                response = await client.get(url, timeout=READY_PROBE_HTTP_TIMEOUT_SECONDS)
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("ready_probe %s attempt %d failed: %s", url, attempt, exc)
                continue
            # 响应体非空才算真就绪 —— 与 arcreel-env-gate.sh:wait_ready 的语义一致
            if response.status_code == 200 and response.content:
                logger.info("ready_probe %s 200 (attempt %d)", url, attempt)
                return True
        if attempt < READY_PROBE_MAX_ATTEMPTS:
            await asyncio.sleep(READY_PROBE_INTERVAL_SECONDS)
    return False


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
        # 又被另一进程抢走」的边缘情况。
        if not await _probe_ready():
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
    "DEFAULT_CURRENT_ENV_PATH",
    "DEFAULT_GATE_SCRIPT",
    "DEFAULT_LOG_DIR",
    "DEFAULT_READY_PROBE_URLS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EnvSwitchError",
    "EnvSwitchFailed",
    "EnvSwitchGateMissing",
    "EnvSwitchResult",
    "EnvSwitchTimeout",
    "ensure_env",
    "in_flight_target",
    "read_current_host_env",
]
