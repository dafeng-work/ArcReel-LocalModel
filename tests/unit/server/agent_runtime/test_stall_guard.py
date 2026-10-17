"""停滞熔断判定：同一工具累计失败 N 次触发（不要求连续）。

判定键的由来见 :mod:`server.agent_runtime.stall_guard` 模块 docstring——实测事故里
7 次失败全被成功的空转 Bash 隔开，故「连续失败」规则永不触发，必须按工具累计。
"""

from __future__ import annotations

import pytest

from server.agent_runtime.stall_guard import STALL_FAIL_THRESHOLD, StallGuard, StallTrip


def _tool_use(use_id: str, name: str) -> dict[str, object]:
    return {"type": "assistant", "content": [{"type": "tool_use", "id": use_id, "name": name}]}


def _result(use_id: str, *, is_error: bool, timestamp: str = "2026-10-14T08:00:00Z") -> dict[str, object]:
    return {"type": "tool_result", "tool_use_id": use_id, "is_error": is_error, "timestamp": timestamp}


def _feed(guard: StallGuard, use_id: str, name: str, *, is_error: bool, timestamp: str = "t") -> StallTrip | None:
    """跑一次完整的 tool_use → tool_result 对，返回本次是否触发。"""
    guard.observe(_tool_use(use_id, name))
    return guard.observe(_result(use_id, is_error=is_error, timestamp=timestamp))


def test_threshold_default_is_three() -> None:
    """默认阈值 3 —— 在实测事故的第 3 次失败处即可触发。"""
    assert STALL_FAIL_THRESHOLD == 3


def test_two_failures_do_not_trip() -> None:
    guard = StallGuard()
    assert _feed(guard, "a1", "Agent", is_error=True) is None
    assert _feed(guard, "a2", "Agent", is_error=True) is None
    assert not guard.tripped


def test_third_failure_trips() -> None:
    guard = StallGuard()
    _feed(guard, "a1", "Agent", is_error=True)
    _feed(guard, "a2", "Agent", is_error=True)
    trip = _feed(guard, "a3", "Agent", is_error=True, timestamp="2026-10-14T08:07:38Z")

    assert trip is not None
    assert trip.tool_name == "Agent"
    assert trip.failures == 3
    assert trip.last_timestamp == "2026-10-14T08:07:38Z"
    assert guard.tripped


def test_failures_separated_by_other_tools_success_still_trip() -> None:
    """本次事故的核心回归用例：失败之间夹着成功的空转 Bash（sleep 180）。

    「连续失败」规则在这里永远不触发，必须按同一工具累计计数。
    """
    guard = StallGuard()
    assert _feed(guard, "a1", "Agent", is_error=True) is None
    assert _feed(guard, "b1", "Bash", is_error=False) is None  # sleep 180
    assert _feed(guard, "a2", "Agent", is_error=True) is None
    assert _feed(guard, "b2", "Bash", is_error=False) is None
    trip = _feed(guard, "a3", "Agent", is_error=True)

    assert trip is not None
    assert trip.tool_name == "Agent"


def test_success_resets_that_tool_counter() -> None:
    """同一工具出现成功结果后计数清零 —— 该工具确实能用，就不算停滞。"""
    guard = StallGuard()
    _feed(guard, "r1", "Read", is_error=True)
    _feed(guard, "r2", "Read", is_error=True)
    _feed(guard, "r3", "Read", is_error=False)
    assert guard.failures_for("Read") == 0

    assert _feed(guard, "r4", "Read", is_error=True) is None
    assert _feed(guard, "r5", "Read", is_error=True) is None
    assert not guard.tripped


def test_distinct_tools_do_not_accumulate_together() -> None:
    """交替失败的不同工具不互相累加。"""
    guard = StallGuard()
    _feed(guard, "p1", "Read", is_error=True)
    _feed(guard, "q1", "Edit", is_error=True)
    _feed(guard, "p2", "Read", is_error=True)
    _feed(guard, "q2", "Edit", is_error=True)

    assert not guard.tripped
    assert guard.failures_for("Read") == 2
    assert guard.failures_for("Edit") == 2


def test_trip_is_latched_and_reported_once() -> None:
    """触发后锁存：同一次事故不会在会话尾部反复要求中断。"""
    guard = StallGuard()
    _feed(guard, "a1", "Agent", is_error=True)
    _feed(guard, "a2", "Agent", is_error=True)
    assert _feed(guard, "a3", "Agent", is_error=True) is not None
    assert _feed(guard, "a4", "Agent", is_error=True) is None


def test_entries_without_tool_use_id_are_ignored() -> None:
    """非工具条目（用户输入、系统事件）不参与计数。"""
    guard = StallGuard()
    assert guard.observe({"type": "user", "content": "hi"}) is None
    assert guard.observe({"type": "system", "subtype": "interrupt"}) is None
    assert not guard.tripped


def test_unresolvable_tool_use_id_is_ignored() -> None:
    """tool_result 找不到对应 tool_use（关联丢失）时不记失败 —— 宁可漏报不误杀。"""
    guard = StallGuard()
    assert guard.observe(_result("orphan", is_error=True)) is None
    assert guard.observe(_result("orphan2", is_error=True)) is None
    assert not guard.tripped


def test_unnamed_tool_use_falls_into_unknown_bucket() -> None:
    """缺 name 的 tool_use 归入 <unknown> 桶，仍按「同一工具」判定。"""
    guard = StallGuard()
    assert _feed(guard, "n1", "", is_error=True) is None
    assert _feed(guard, "n2", "", is_error=True) is None
    trip = _feed(guard, "n3", "", is_error=True)

    assert trip is not None
    assert trip.tool_name == "<unknown>"


def test_custom_threshold_is_honored() -> None:
    guard = StallGuard(1)
    assert _feed(guard, "a1", "Agent", is_error=True) is not None


def test_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="threshold"):
        StallGuard(0)
