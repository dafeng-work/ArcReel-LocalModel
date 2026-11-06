"""生成模式的取值集合与参考家族判据。

本模块只放纯数据与纯函数，不引任何项目层 / 脚本层模块——谁都能引。

原先这些定义住在 :mod:`lib.project.project_manager`，但产物溯源
（:mod:`lib.artifacts.artifact_provenance`）只拿到生成模式字符串、不拿项目字典，
而 ``lib.project.project_manager`` 会把脚本层拉进导入链：溯源层引它会成环。
判据本身是纯的，住在叶子模块里，两侧引的是同一份。

「参考家族」= ``reference_video`` 或 ``mixed``：混合模式复用参考生视频的全部管线
（video_units 骨架、参考投影与执行链），只是单元级自选首帧 / 参考路线。凡「走参考
管线」语义的消费方用本判据；需严格区分两种模式的判据（批量闸、首帧字段生效范围）
继续读 ``generation_mode`` 原值。
"""

from __future__ import annotations

#: 生成模式的全部合法取值。
VALID_GENERATION_MODES: frozenset[str] = frozenset({"storyboard", "reference_video", "mixed"})

#: 参考家族的生成模式取值。
REFERENCE_FAMILY_GENERATION_MODES: frozenset[str] = frozenset({"reference_video", "mixed"})


def is_reference_family_generation_mode(generation_mode: object) -> bool:
    """生成模式是否属参考家族（``reference_video`` 或 ``mixed``）。

    非字符串（缺字段 / 脏数据）一律判否：模式不明时不得默认落到参考管线。
    """
    return isinstance(generation_mode, str) and generation_mode in REFERENCE_FAMILY_GENERATION_MODES


__all__ = [
    "REFERENCE_FAMILY_GENERATION_MODES",
    "VALID_GENERATION_MODES",
    "is_reference_family_generation_mode",
]
