"""停滞熔断：把「同一工具反复失败却零产出」变成有界失败。

**要治的病**：本机单 slot 推理服务上曾出现约 2 小时零产出的空转——Agent 每次调工具
都被 auto 模式的安全分类器超时拒绝，而 CLI 把该超时归类为「可重试」，于是模型
sleep 180 → 重试 → 再超时，循环自供能（每次重试都要重发 44K–92K 上下文）。

**为什么判定键是「同一工具累计失败」而不是「连续失败」**：实测那份 transcript 里，
7 次分类器超时**全部**被成功的空转 ``Bash``（即 ``sleep 180``）隔开——

    ERR 06:33:33 / ERR 06:37:42 / ok 06:41:05(Bash) / … / ERR 07:49:56 / ok 07:53:48(Bash)
    ERR 07:58:30 / ok 08:02:53(Bash) / ERR 08:07:38 / ok 08:13:09(Bash) / ERR 08:17:59 / …

任何「连续 N 次失败」规则都不会触发。真实特征是**同一工具反复失败、其间零产出**，
故按工具累计计数，只在该工具出现成功结果时清零。

**只看结构化字段**（``tool_name`` / ``is_error`` / ``tool_use_id``），不解析错误文案
做安全判断——与 ``failure_observation`` 的既有原则一致。工具名靠 ``tool_use`` 块的
id → name 关联得到；解析不到时归入 ``"<unknown>"`` 桶，不影响「同一工具」这一判据。

**不按时间连续性判定**：慢 ≠ 坏。正常的长时间生成（视频批次等待）不产生工具失败，
因此不会被本守卫误杀。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: 同一工具累计失败多少次判定为停滞。3 足以在实测事故的第 3 次失败处触发
#: （比实际结束早约 20 分钟），又不至于被偶发的一两次失败触发。
STALL_FAIL_THRESHOLD = 3

#: ``tool_use`` id → 工具名 的关联表容量上限。正常情况下每个 id 在其 tool_result
#: 到达时即被弹出；上限只兜住「tool_use 永无结果」的病态情况（如会话中途被杀）。
_TOOL_NAME_CACHE_LIMIT = 1024

#: 关联不上的工具名桶。
_UNKNOWN_TOOL = "<unknown>"


@dataclass(frozen=True)
class StallTrip:
    """一次停滞判定结果——触发方据此中断在途 query 并落可读结论。"""

    tool_name: str
    failures: int
    last_timestamp: str | None


class StallGuard:
    """每会话一个：喂入已归一化的日志条目，判定停滞并只触发一次。

    ``observe`` 对每条 entry 幂等可重复调用；一旦触发即锁存（latched），后续条目
    不再重复上报——避免同一次事故在会话尾部反复要求中断。
    """

    def __init__(self, threshold: int = STALL_FAIL_THRESHOLD) -> None:
        if threshold < 1:
            raise ValueError("threshold 必须 >= 1")
        self._threshold = threshold
        self._tool_name_by_use_id: dict[str, str] = {}
        self._fail_counts: dict[str, int] = {}
        self._tripped: bool = False

    @property
    def tripped(self) -> bool:
        """是否已触发（已触发后 ``observe`` 不再上报）。"""
        return self._tripped

    def failures_for(self, tool_name: str) -> int:
        """该工具当前的累计失败数（诊断 / 测试用）。"""
        return self._fail_counts.get(tool_name, 0)

    def observe(self, entry: Mapping[str, object]) -> StallTrip | None:
        """喂一条归一化条目；触发停滞时返回 :class:`StallTrip`，否则返回 ``None``。"""
        if self._tripped:
            return None

        entry_type = str(entry.get("type") or "")

        if entry_type == "assistant":
            self._remember_tool_uses(entry.get("content"))
            return None

        use_id = entry.get("tool_use_id")
        if not use_id:
            return None
        tool_name = self._resolve_tool_name(str(use_id))
        if tool_name is None:
            # 关联不上的 tool_result：既不记成功也不记失败，只清掉缓存条目。
            self._tool_name_by_use_id.pop(str(use_id), None)
            return None

        if bool(entry.get("is_error")):
            count = self._fail_counts.get(tool_name, 0) + 1
            self._fail_counts[tool_name] = count
            if count >= self._threshold:
                self._tripped = True
                return StallTrip(
                    tool_name=tool_name,
                    failures=count,
                    last_timestamp=self._as_optional_str(entry.get("timestamp")),
                )
            return None

        # 该工具出现成功结果 → 计数清零。
        self._fail_counts[tool_name] = 0
        return None

    def _remember_tool_uses(self, content: object) -> None:
        """从 assistant 条目的 ``content`` 里登记 ``tool_use.id → tool_use.name``。"""
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if str(block.get("type") or "") != "tool_use":
                continue
            use_id = block.get("id")
            if not use_id:
                continue
            name = str(block.get("name") or "").strip() or _UNKNOWN_TOOL
            if len(self._tool_name_by_use_id) >= _TOOL_NAME_CACHE_LIMIT:
                self._tool_name_by_use_id.clear()
            self._tool_name_by_use_id[str(use_id)] = name

    def _resolve_tool_name(self, use_id: str) -> str | None:
        """解析并弹出工具名；未登记返回 ``None``（调用方据此判定为不可归属）。"""
        return self._tool_name_by_use_id.pop(use_id, None)

    @staticmethod
    def _as_optional_str(value: object) -> str | None:
        return value if isinstance(value, str) and value else None


__all__ = ["STALL_FAIL_THRESHOLD", "StallGuard", "StallTrip"]
