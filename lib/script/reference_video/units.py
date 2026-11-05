"""参考生视频 unit 的查找与定桶纯映射。

可执行请求的实际定桶由 ``ReferenceUnitRequestProjector`` 先从正文派生参考图、再水合项目
文件完成；本模块只保留已给定布尔事实的纯映射与 unit 查找。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.infra.path_safety import PathTraversalError, safe_join

if TYPE_CHECKING:
    # 仅类型导入：lib.project.project_manager 经 lib.script.reference_video 包初始化间接加载本模块，而
    # lib.config.resolver 又反向 import lib.project.project_manager，运行时导入会成环。
    from lib.config.resolver import VideoGenerationType


def reference_video_bucket(
    *,
    with_references: bool,
    first_frame_available: bool = False,
) -> VideoGenerationType:
    """视频单元的任务类型桶：首帧可用 → i2v（首帧优先，即使有参考图）；有参考图 → r2v；都没有 → i2v。

    本函数只对已给定的布尔事实做纯映射。预检、报价、Agent、队列与执行层须先由
    ``ReferenceUnitRequestProjector`` 从正文派生引用并水合实际可用资产，不能自行猜测。
    """
    if first_frame_available:
        return "i2v"
    return "r2v" if with_references else "i2v"


def find_reference_unit(script: dict, unit_id: str) -> dict | None:
    """在剧本的自包含 ``video_units`` 中定位单元。"""
    units = script.get("video_units") or []
    return next((u for u in units if isinstance(u, dict) and u.get("unit_id") == unit_id), None)


def is_first_frame_available(unit: dict, project_path: Path) -> bool:
    """单元首帧图是否可用：字段已声明且项目内相对路径存在（越界/绝对路径/空值判不可用）。

    与投影侧 ``_candidate_path`` 同一 ``safe_join`` 判据：绝对路径按 os.path.join 语义丢前缀后
    仍须落在项目内。混合模式定桶与投影载荷 first_frame 因此共用这一可用性出口。
    """
    asset = unit.get("first_frame_asset")
    if not isinstance(asset, str) or not asset:
        return False
    try:
        candidate = safe_join(project_path, asset)
    except (OSError, PathTraversalError, TypeError):
        return False
    return candidate.exists()


def resolve_unit_video_bucket(project: dict, unit: dict, project_path: Path) -> VideoGenerationType:
    """项目生成模式 + 单元事实 → 该单元执行要落的任务类型桶（读侧与执行侧共用的单元级入口）。

    ``storyboard`` → i2v；``reference_video`` → r2v（无参考图的逐单元降级由执行器水合后按
    ``reference_video_bucket`` 完成）；``mixed`` → 首帧已声明且文件可用时 i2v，否则 r2v。
    未知模式落默认桶 i2v，与 ``video_bucket_for_generation_mode`` 同口径。
    """
    mode = project.get("generation_mode")
    if mode == "storyboard":
        return "i2v"
    if mode == "mixed":
        return "i2v" if is_first_frame_available(unit, project_path) else "r2v"
    return "r2v" if mode == "reference_video" else "i2v"
