---
status: accepted
---

# 混合模式（mixed）：参考家族第三值，单向汇点，逐单元自选生成路线

视频项目的 `generation_mode` 从二值（`storyboard` / `reference_video`）扩为三值，新增 `mixed`。混合模式下视频单元**逐个自选**生成路线：单元附挂首帧资产（`first_frame_asset`）→ 首帧生视频（i2v 桶）；不附挂 → 参考生视频（r2v 桶）。同一项目内两条路线共存。

动机：参考家族项目里既有「角色/场景/道具参考图已经足够」的单元，也有「构图已定、只差把静图动起来」的单元。二值模式逼着这类项目整体选一条路线，另一类单元被迫绕路（参考单元重写成构图描述、或首帧单元放弃精确构图）。混合模式把路线决策下沉到单元粒度。

## 规则

- `mixed` 属于**参考家族**：`is_reference_family_project` 覆盖 `reference_video` 与 `mixed`。分镜路线的端点、执行器、准入对 mixed 项目整体关闭（mixed 项目没有分镜实体）；参考家族端点对 mixed 项目开放。
- **单向汇点**：转换矩阵只允许 `storyboard → mixed` 与 `reference_video → mixed`，`mixed` 不可转出（任何目标值均拒绝）。`validate_generation_mode_transition` 是该矩阵的单一实现，PATCH 项目端点在其上做闸。storyboard 项目转入 mixed 时，既有分镜实体不被删除但不再被视频管线消费——等于放弃分镜路线，需要按参考单元重新组织内容（重拆后果），UI 在转换确认里明示。
- 模式级**代表桶**：无单元上下文时（项目级 caps、计价主模型、自报身份、pair 自报），mixed 回退参考家族代表桶 `r2v`（`lib/config/resolver.py` 的 `VIDEO_BUCKET_BY_GENERATION_MODE`）。逐单元真桶由执行器按 `resolve_unit_video_bucket`（mixed）/ `reference_video_bucket`（reference_video，保留无参考图时降级 t2v 的既有行为）解析。代表桶是读侧回退值，不是逐单元承诺。
- 首帧资产是**单元字段** `first_frame_asset`（项目内图片路径，Pydantic 严格白名单登记）。服务端闸：非 mixed 项目设值 → 400；mixed 项目空串 = 清除。可用性判据单一出口 `is_first_frame_available`（safe_join 语义 + 文件系统存在），投影层与执行器共用。
- 首帧模式下参考图**不发**，投影侧给非阻断问题码 `references_ignored_in_first_frame_mode`；首帧缺失时给 `first_frame_asset_missing`。暂存走 `start_image` role（`logical_type="unit"`），与参考图槽独立；checkpoint 对 reference-kind 允许 start_image/end_image 各至多 1 个。
- **批量排队不支持**：`admit_reference_video_batch` 顶部 mixed 闸，在任何状态探测前结构化拒绝（`mixed_mode_batch_not_supported`）。批量语义是「一次准入、整批入队」，逐单元定桶的项目里整批共享一个桶假设不成立；逐单元生成入口不受影响。
- 视觉摘要（artifact provenance / currency）计入首帧：首帧变更使既有视频产物失效。首帧走文件系统判据，不走 manifest 认领（`_claim_for` 对 `kind="first_frame"` 返回 None）。

## Considered Options

- **不做 mixed，让用户建两个项目**：内容（角色/场景/道具资产、提示词）在两项目间复制漂移，且两项目各自只能批量，体验割裂。单元粒度路线选择才是真实需求。
- **mixed 可转出（双向转换）**：mixed 单元上已存在首帧字段与首帧路线产物，转回 storyboard 会静默丢弃单元级路线状态，转回 reference_video 会让首帧单元悄悄降级。单向汇点让状态演化不可逆但可预期。
- **模式级双桶并集（caps/计价取 i2v ∪ r2v）**：`video_caps`、计价、pair 自报的结构都是单桶标量，并集化要动消费方结构；且「并集」语义对计价无意义（同一请求只走一个桶）。代表桶回退让消费方结构不动，逐单元真值由执行器负责。
- **允许 mixed 批量排队**：批量准入的「整批同桶」假设与逐单元定桶冲突，只能把批量拆成 N 次单单元准入——那与逐单元生成入口等价，不如直接结构化拒绝并指向逐单元入口。
- **首帧走上传通道**：首帧来源（分镜静图、外部静图）已是项目内资产，新增上传通道只增加资产归属与清理语义；附挂选择器先用既有资产，上传属独立功能。

## Consequences

- 参考家族从「一条路线」变成「一条路线 + 一个汇点」：所有 `is_reference_family_project` 消费方须逐一核对 mixed 行为（本次实施已列清单核对，漏一处 = 混合项目某路径静默走错管线）。
- 「创建即定」的生成模式纪律松一格：模式仍不可任意变更，但允许向汇点单向收敛。后续若出现第四种模式，须先决定它是新路线还是新汇点。
- 项目级 caps/计价在 mixed 下报的是 r2v 代表桶：逐单元 i2v 请求的实际能力由执行器定桶时的单元级闸兜底，项目级数字对 mixed 项目是「多数情况」近似而非精确承诺。
- 批量端点对 mixed 项目恒拒：依赖批量排队的自动化脚本须改为逐单元提交；该拒绝是结构化的（问题码），不是 500。
