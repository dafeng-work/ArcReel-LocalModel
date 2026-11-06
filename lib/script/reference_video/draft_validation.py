"""参考生视频 script_plan / prompt_authoring 扁平草稿结构的机械校验。

LLM 产出与人在编辑器写的是同一种格式，校验因此也落在同一份文本上；本模块是「parser
后校验」这一层的落点：schema 已卡死枚举与外层结构，剩下的语法与内容约束在这里逐 unit
判定，任一违约 fail-loud 抛 :class:`DraftViolation`，不把违规产物当成功结果写盘。

「不当成功结果写盘」不等于丢弃：调用侧用 :func:`collect_violations` 把逐 unit 的违约收齐成
一份报告，产物落待修复草稿（``lib.script.draft_quarantine``）等 Agent 修复后重判，不重抽。
每条违约带 ``code``（违约类）与 ``label``（unit 定位），报告因此可逐条定位、可按类断言。

违约条目类型与报告渲染（:class:`DraftViolation` / :func:`collect_violations` /
:func:`render_violation_report`）三条路线通用，定义在路线中立的 ``lib.script.draft_violation``；
本模块原样再导出它们，参考生视频的既有导入路径不变。

与编辑器侧（人写）的容忍口径分流：``lib.script.reference_video.script_preview`` 对同样的文本只
出 warning、照常落盘——那里有作者意图要保护；本模块面向机器产物，没有意图可保护，一律拒。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Container
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib.project.asset_types import asset_name_comparison_key
from lib.references.reference_catalog import build_reference_catalog
from lib.script.draft_violation import (
    DraftViolation,
    DraftViolations,
    collect_violations,
    render_violation_report,
    violation_items,
)
from lib.script.reference_video.text_parser import (
    SpeechMark,
    derive_references_from_text,
    find_malformed_mention,
    leading_mention_before_colon,
    line_speech_marks,
    mention_names,
    pending_derivative_references,
    speech_line_description,
    speech_speaker_references,
    split_speech_line,
    strip_speech_marks,
)
from lib.script.script_models import ReferenceResource
from lib.speech.speech_composition import SpeechProblem, admit_script_unit
from lib.speech.speech_rate import estimate_spoken_seconds

#: 台词口播时长相对 unit 时长的宽容系数：估算超出 unit 时长这个比例才判超载。
#: 语速是统计估算（``lib.speech.speech_rate``），逐字计数与真实配音节奏必然有出入；不留宽容会
#: 把「刚好写满」的正常产出判违约。与 drama 保存期上界 warning 的 20% 同量级——两处都是
#: 「同一套语速估算 vs 已定时长」的比对，宽容度没有理由不同。
SPEECH_OVERFLOW_TOLERANCE = 0.20


def _normalize_for_anchor(text: str) -> str:
    """Unicode NFC 归一后把连续空白折叠为单个空格，只消除编码与空白差异，不删除空白本身。

    与 narration 覆盖校验的 ``_normalize_for_coverage`` 同一口径：模型复制原文时换行与
    缩进的还原不可靠，但删字改字必须被抓住。NFC 与 ``lib.episode.episode_ledger.normalize_source_text``
    定义的源文坐标系一致——带组合附加符的语种（如 vi）源文可能以 NFD 落盘、模型回写 NFC，
    纯编码形式差异不该被判成改写。
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def is_verbatim_source_anchor(source_text: str, novel_text: str) -> bool:
    """``source_text`` 在空白归一后是否为源文的逐字子串（空锚不算命中）。"""
    anchor = _normalize_for_anchor(source_text)
    return bool(anchor) and anchor in _normalize_for_anchor(novel_text)


def validate_source_text_anchor(label: str, source_text: str, novel_text: str) -> None:
    """校验 ``source_text`` 是源文的逐字子串（空白归一后）。

    script_plan 的 unit 边界要能追溯回原文，锚失配意味着模型在拆分时改写或杜撰了原文——这是内容
    层的根本违约，比任何下游画面问题都更早需要被拦下。只判子串、不判顺序与完整覆盖：unit
    是画面单元不是朗读单元，允许原文中的对话提示语、转述段落不进任何 unit 的锚。
    """
    anchor = _normalize_for_anchor(source_text)
    if not anchor:
        raise DraftViolation(
            f"{label} 的 source_text 为空：每个 unit 必须摘录其所依据的原文片段作为追溯锚",
            code="source_text_empty",
            label=label,
        )
    if not is_verbatim_source_anchor(source_text, novel_text):
        raise DraftViolation(
            f"{label} 的 source_text 不是小说原文的逐字片段（存在改写、翻译或杜撰）："
            f"{source_text.strip()[:40]!r}；请原样复制原文，不要转述",
            code="source_text_not_verbatim",
            label=label,
        )


#: 全角花括号。语法只认半角，但中文输入法下模型很容易写出全角形；行里出现全角花括号时
#: 该段不成发声记号，会被当成画面描述放行——台词静默降级成描述、说话人反而被派生成参考图。
#: 故在语法判定处显式识别并拒绝，不静默、也不代模型改写。
_FULLWIDTH_BRACES = "｛｝"


def _assert_line_syntax(label: str, text: str, character_references: Container[str]) -> None:
    """逐行判引用语法：花括号用法、写坏的 ``@[`` 引用、缺花括号的台词。

    三类共性是「解析器不报错、但派生结果与作者意图相反」：台词降级成画面描述、说话人反被
    派生成参考图、坏 token 原样进供应商请求。机器产物没有作者意图可保护，一律在语法判定处
    响亮拒绝，不静默、也不代模型改写。
    """
    for idx, line in enumerate(text.splitlines()):
        if any(ch in line for ch in _FULLWIDTH_BRACES):
            raise DraftViolation(
                f"{label} 使用了全角花括号：{line.strip()[:40]!r}；"
                "台词与画外音的花括号必须是半角 `{}`，全角形不会被识别为台词",
                code="fullwidth_braces",
                label=label,
                line=idx,
            )
        malformed = find_malformed_mention(line)
        if malformed is not None:
            raise DraftViolation(
                f"{label} 有写坏的资产引用：{malformed!r}；"
                "引用须写成 `@[资产名]`，方括号要成对闭合、名称非空，否则既不进 references，"
                "又会原样进入视频请求",
                code="malformed_mention",
                label=label,
                line=idx,
            )
        parts = split_speech_line(line)
        leads_with_speech = bool(parts) and isinstance(parts[0], SpeechMark) and bool(parts[0].speaker)
        # 只有登记角色 + 冒号才判成写坏的台词：场景 / 道具做小标题（``@[酒馆]：木门被风吹开``）
        # 是合法的画面描述写法，按同一形态一概判违约会把正常的 script_plan 产出拒掉。
        if not leads_with_speech and (leading_mention_before_colon(line) or "") in character_references:
            raise DraftViolation(
                f"{label} 的台词写法不合法：{line.strip()[:40]!r}；"
                "台词须写成 `@[角色]{台词}`——说话人非空、台词由半角花括号成对包裹，"
                "否则这段会被当成画面描述、台词整句丢失",
                code="dialogue_line_syntax",
                label=label,
                line=idx,
            )
        # 只判记号之外的残余：一行里已识别的台词不因同行另有花括号被连坐。
        rest = speech_line_description(parts)
        if "{" not in rest and "}" not in rest:
            continue
        excerpt = line.strip()[:40]
        if rest.count("{") != rest.count("}"):
            raise DraftViolation(f"{label} 有未闭合的花括号：{excerpt!r}", code="unclosed_brace", label=label, line=idx)
        raise DraftViolation(
            f"{label} 在画面描述里使用了花括号：{excerpt!r}；"
            "花括号是发声保留语法，台词写作 `@[角色]{台词}`、画外音写作 `{画外音}`，"
            "说话人须非空、花括号不得嵌套",
            code="braces_in_description",
            label=label,
            line=idx,
        )


def _has_description_line(text: str) -> bool:
    """该单元是否有画面描述：某一行剥掉全部发声记号后仍有非空文本。"""
    return any(strip_speech_marks(line).strip() for line in text.splitlines())


def dialogue_speakers(text: str) -> list[str]:
    """按出现顺序取出台词记号的说话人（去重）——登记校验据此判说话人是否为登记角色。

    说话人取自 ``split_speech_line``，已在解析器入口归一到资产名比对坐标系
    （``lib.script.reference_video.text_parser`` 的 ``_normalize_source``），与资产表归一后的 key
    同形，本函数直接使用该结果，不再额外归一。

    说话人位写 ``@[角色/衍生]`` 时给出的是本体名：声音属于身份，衍生共享本体的参考音频与
    音色（见 ``docs/adr/0072``）。写下的那套外观见 ``speech_speaker_references``。
    """
    seen: set[str] = set()
    speakers: list[str] = []
    for line in text.splitlines():
        for mark in line_speech_marks(line):
            if mark.speaker and mark.speaker not in seen:
                seen.add(mark.speaker)
                speakers.append(mark.speaker)
    return speakers


def normative_lines(text: str) -> list[tuple[str, str, str]]:
    """按出现顺序取出全部发声记号：``(kind, speaker, 台词)``，``kind`` 为 dialogue / voiceover。

    prompt_authoring 的保结构 diff 以此为比对项：画面描述可自由展开，发声记号必须逐字不变。

    台词与说话人已在解析器入口归一到 NFC（``lib.script.reference_video.text_parser`` 的
    ``_normalize_source``）：源文可能以 NFD 落盘而模型回写 NFC，两种形式肉眼同字、逐字比对
    却不等，保结构 diff 会把纯编码差异判成改写；口播时长估算同样要求 NFC（按词计的语种下
    组合附加符会把一个词拆成多个阅读单位）。归一在解析器一处完成，两个消费方口径天然一致。
    """
    result: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        result.extend(
            ("dialogue" if mark.speaker else "voiceover", mark.speaker, mark.text) for mark in line_speech_marks(line)
        )
    return result


def validate_unit_text(
    label: str,
    text: str,
    project: dict[str, Any],
    *,
    unit_id: str | None = None,
    max_refs: int | None,
    tolerated_speakers: Collection[str] = (),
    relational_word_check: bool = True,
) -> list[ReferenceResource]:
    """校验一个 unit 的正文并机械派生参考图引用。

    覆盖四类阻断违约：正文为空或只有发声记号、引用语法误用（花括号、写坏的引用、缺花
    括号的台词）、``@[名称]`` 未登记（含台词记号的说话人位与它写下的衍生）、参考图数超模型
    上限。正文是唯一落盘物，派生结果只服务本次校验与能力判定，不写回。

    ``tolerated_speakers`` 里的说话人不要求已登记：正式正文里确认过的说话人可以是未登记的
    群演，执行时该行不绑声音、按原文发送。

    ``relational_word_check=False`` 给逐字锚层用：script_plan 的正文 = 源文逐字副本 + 姿态
    标记行，源文措辞不是判定对象，加标记不得让源文措辞被判违约——关系词黑名单只作用于
    prompt_authoring 的自由扩写层。
    """
    if not text.strip():
        raise DraftViolation(f"{label} 的正文为空", code="empty_text", label=label)

    # 已登记名字一律取自引用目录：目录的名字已在比对坐标系中，正文一侧由解析器入口归一，
    # 两侧同形才判得准。
    catalog = build_reference_catalog(project)
    _assert_line_syntax(label, text, catalog.reference_names("character"))

    # 只有台词与画外音的正文没有可生成的画面：整段非空时上面的空正文检查放不住它。
    if not _has_description_line(text):
        raise DraftViolation(
            f"{label} 没有画面描述；只有台词与画外音的单元没有可生成的画面",
            code="blank_description",
            label=label,
        )

    # 与编辑器预览共用 ``derive_references_from_text``：严格度分流（此处对 missing 与上限一律拒），
    # 派生口径不分流——否则同一份正文在两侧派生出不同的 `图N` 编号。
    refs, missing = derive_references_from_text(text, project)
    if missing:
        raise DraftViolation(
            f"{label} 引用了未登记的资产名: {missing}；资产名必须逐字取自 project.json 三张表",
            code="unregistered_asset",
            label=label,
        )

    # 裸提及 ``@本体/衍生``：解析器在斜线处停笔，衍生段静默回落到本体——未登记的衍生
    # （如男性专属衍生挂到女性角色）就这样漏过 missing。按完整引用名判登记。
    character_references = catalog.reference_names("character")
    bad_derivatives = sorted({ref for ref in pending_derivative_references(text) if ref not in character_references})
    if bad_derivatives:
        raise DraftViolation(
            f"{label} 引用了未登记的衍生: {bad_derivatives}；衍生须先登记在该角色下，或改写成本体名 `@[角色]`",
            code="unregistered_reference_derivative",
            label=label,
        )

    # 说话人位问的不是「能不能引用」而是「是不是一条角色资产」：它决定该句台词绑哪段参考音频。
    tolerated = {asset_name_comparison_key(name) for name in tolerated_speakers}
    registered_speakers = catalog.asset_names("character")
    bad_speakers = sorted({s for s in dialogue_speakers(text) if s not in registered_speakers and s not in tolerated})
    if bad_speakers:
        raise DraftViolation(
            f"{label} 的台词说话人未登记为角色资产: {bad_speakers}；说话人决定该句台词绑哪段参考音频，必须是登记角色",
            code="unregistered_speaker",
            label=label,
        )

    # 本体已登记，说话人位仍不在引用名里，只可能是那个角色没有这个衍生。分成两条违约：一条
    # 该去登记角色，一条该去登记衍生或改名，合成一句会指错修复方向。
    bad_forms = sorted(
        {name for name in speech_speaker_references(text) if name not in character_references and name not in tolerated}
    )
    if bad_forms:
        raise DraftViolation(
            f"{label} 的台词说话人写了该角色没有的衍生: {bad_forms}；"
            "衍生须先登记在该角色下，或改写成本体名 `@[角色]{台词}`",
            code="unregistered_speaker_derivative",
            label=label,
        )

    if max_refs is not None and len(refs) > max_refs:
        raise DraftViolation(
            f"{label} 的参考图数 {len(refs)} 超过模型上限 {max_refs}；请把次要角色融入背景描述（不用 `@` 引用）",
            code="refs_over_limit",
            label=label,
        )
    canonical_unit_id = unit_id if unit_id is not None else label.removeprefix("unit ").strip()
    admission = admit_script_unit("video_units", {"unit_id": canonical_unit_id, "text": text})
    if not admission.allowed:
        raise DraftViolations([_speech_problem_violation(problem) for problem in admission.problems])
    if relational_word_check:
        _validate_relational_words(label, text)
    return refs


def _speech_problem_violation(problem: SpeechProblem) -> DraftViolation:
    locations = ", ".join(
        ".".join(str(part) for part in location.path) + (f" line {location.line}" if location.line is not None else "")
        for location in problem.locations
    )
    line = problem.locations[0].line if len(problem.locations) == 1 else None
    return DraftViolation(
        f"unit {problem.unit_id} 发声准入未通过（{problem.reason.value}，定位：{locations}）；"
        f"下一步：{problem.action.value}",
        code=problem.code.value,
        label=f"unit {problem.unit_id}",
        line=line,
        locations=tuple({"path": list(location.path), "line": location.line} for location in problem.locations),
        reason=problem.reason.value,
        action=problem.action.value,
    )


def validate_dialogue_load(
    label: str,
    text: str,
    duration_seconds: int,
    language: object,
    speech_rate_override: float | None = None,
) -> None:
    """校验该 unit 的台词量念得完：口播估算超出 unit 时长（含宽容系数）即违约。

    时长就是计费，unit 时长在 script_plan 定稿；台词写超了意味着成片必然吞词或抢拍，且这在
    script_plan 阶段是可改的（重拆 unit 或删台词），拖到生成后才发现只能重来。
    ``speech_rate_override`` 是项目级语速覆盖，None 即回退语言默认——与 prompt 构造侧同源，
    prompt 给的下界与此处判的上界始终是同一把尺。
    """
    # language 取自 project.json，可能是非字符串脏数据；非字符串回退 None（按默认语速估算），
    # 与 prompt 构造侧同口径——否则 ``count_reading_units`` 的 ``language.strip()`` 会在一次
    # 已付费的调用之后抛 AttributeError，草稿一并丢失。
    language = language if isinstance(language, str) else None
    # 台词取自 normative_lines，已归一到 NFC：``count_reading_units`` 的 en / vi 分支按
    # ``\b\w+\b`` 数词，NFD 形式下组合附加符不算词字符，一个越南语词会被拆成数个单位
    # （9 词的句子计成 16 个），估算随之虚高、把念得完的 unit 判成超载。
    spoken = sum(estimate_spoken_seconds(line[2], language, speech_rate_override) for line in normative_lines(text))
    budget = duration_seconds * (1 + SPEECH_OVERFLOW_TOLERANCE)
    if spoken > budget:
        raise DraftViolation(
            f"{label} 的台词念完约需 {spoken:.1f} 秒，超过该 unit 的 {duration_seconds} 秒"
            f"（宽容 {SPEECH_OVERFLOW_TOLERANCE:.0%} 后上限 {budget:.1f} 秒）；"
            "请改取更长的时长档、把该 unit 拆开，或精简台词",
            code="dialogue_overload",
            label=label,
        )


def assert_dialogue_preserved(label: str, script_plan_text: str, prompt_authoring_text: str) -> None:
    """prompt_authoring 保结构 diff：发声记号的序列必须与 script_plan 逐字一致。

    prompt_authoring 的职责是提示词编写，台词属于 script_plan 已由用户完成内容确认的内容契约。改词、增删、
    重排一律响亮失败，不静默接受——台词不配画面时正确的出路是报错回到 script_plan，而不是让 prompt_authoring
    自行把台词改成好配的样子。
    """
    before = normative_lines(script_plan_text)
    after = normative_lines(prompt_authoring_text)
    if before == after:
        return
    if len(before) != len(after):
        raise DraftViolation(
            f"{label} 的台词条数被改动（script_plan 有 {len(before)} 条，prompt_authoring 产出 {len(after)} 条）；"
            "prompt_authoring 只做提示词编写，台词须逐字保留",
            code="dialogue_line_count_changed",
            label=label,
        )
    for index, (old, new) in enumerate(zip(before, after, strict=True), start=1):
        if old != new:
            raise DraftViolation(
                f"{label} 第 {index} 条台词被改写（原：{old[1] or '画外音'}「{old[2]}」，"
                f"现：{new[1] or '画外音'}「{new[2]}」）；prompt_authoring 只做提示词编写，台词须逐字保留",
                code="dialogue_rewritten",
                label=label,
            )


# ---------------------------------------------------------------------------
# 显性接触（L4+）姿态契约机检
#
# 标记 `[接触 L4 · <体位> · 支撑：<点>]` 写在 script_plan 阶段该 unit 的 `text` 正文首行
# （`source_text` 是逐字锚、不允许任何附加，标记不进去），prompt_authoring 把它原样保留为
# 主体段首句——标记即本组判定的触发条件与姿态锁契约载体：带标记的单元按七条款骨架机检，
# 不带标记的单元（L4 以下）全部新码静默，对既有单元零影响。
# 已知盲区：L4+ 内容但漏写标记的单元机检不到——模板要求正文自证分级、验收清单兜底，
# 首版通过率指标决定是否补强。
# ---------------------------------------------------------------------------

#: 标记正则：半角方括号 + 接触 + L4/L5 + 分隔符（· / ・ / . / ． 各输入法形态都认）。
_CONTACT_MARK_RE = re.compile(r"\[接触\s*L[45]\s*[·・.．]")

#: 肢位表关键词表：六组 × 两人逐字判不了，取去重关键词命中数做下界。
_POSE_BODY_PART_KEYWORDS = ("头", "臂", "手", "腿", "脚", "骨盆", "膝", "大腿", "髋", "腰")
_POSE_MIN_BODY_PARTS = 5

#: 易掉肢体完整性 / 出框声明关键词表。
_POSE_COMPLETENESS_KEYWORDS = ("完整", "出框", "画外", "画面内", "画面下缘", "保持在场", "存在于", "留在画面")

#: 取景下界关键词表：景别词 + 入画声明，两者同时出现才算声明过。
_POSE_SHOT_SIZE_KEYWORDS = ("中景", "全景", "远景")
_POSE_FRAME_KEYWORDS = ("入画", "入镜", "全身")

#: 关系词黑名单数据文件（一行一词，`#` 注释）：词表维护不动代码。
_RELATIONAL_BLACKLIST_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "prompt_rules" / "relational_word_blacklist.txt"
)


@lru_cache(maxsize=1)
def _load_relational_blacklist() -> tuple[str, ...]:
    words: list[str] = []
    for line in _RELATIONAL_BLACKLIST_PATH.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            words.append(word)
    return tuple(words)


def contact_pose_marker(text: str) -> str | None:
    """取 unit 正文里的姿态标记行（首个含 `[接触 L4/L5 …]` 的非空行）；无标记返回 None。

    返回原始行（未归一）——姿态锁比对时两侧各自归一，调用方展示用原形。
    """
    for line in text.splitlines():
        if _CONTACT_MARK_RE.search(line):
            return line.strip()
    return None


#: 标记 token：括号整段 `[接触 L4/L5 · 体位 · 支撑：…]`（不含行内后续骨架句）。
_POSE_MARK_TOKEN_RE = re.compile(r"\[接触[^]]*\]")


def pose_mark_token(text: str) -> str | None:
    """取正文里的姿态标记 token（首个 `[接触 …]` 括号段）；无标记返回 None。

    ``contact_pose_marker`` 返回整行（姿态锁要比骨架句），这里只取括号段——plan 侧标记行
    只有 token、prompt 侧标记行是 token + 骨架句，逐字对齐要比的是 token，不是整行。
    """
    for line in text.splitlines():
        match = _POSE_MARK_TOKEN_RE.search(line)
        if match:
            return match.group(0)
    return None


def _validate_relational_words(label: str, text: str) -> None:
    """L4+ 单元正文关系词黑名单判：命中任一词即违约（词表见数据文件）。"""
    if contact_pose_marker(text) is None:
        return
    hits = [word for word in _load_relational_blacklist() if word in text]
    if hits:
        raise DraftViolation(
            f"{label} 的 L4+ 单元正文命中关系词黑名单: {'、'.join(hits)}；"
            "关系词零几何锚点，须改写为解剖位置词（谁的上肢 / 下肢 / 骨盆在哪、撑在哪）",
            code="relational_word_found",
            label=label,
        )


def validate_pose_spec(label: str, text: str) -> None:
    """L4+ 单元正文七条款骨架下界机检（prompt_authoring 阶段）。

    只对带标记的单元生效（无标记静默——向后兼容）。四项骨架按关键词锚点判，缺项合并成
    一条 ``pose_spec_missing``：肢位表（去重肢体关键词 ≥5）、易掉肢体完整性 / 出框声明、
    取景下界（景别词 + 入画声明同时出现）。骨架声明首句即标记行本身——能走到这里即已满足，
    不重复判。
    """
    if contact_pose_marker(text) is None:
        return
    missing: list[str] = []
    body_part_hits = sum(1 for keyword in _POSE_BODY_PART_KEYWORDS if keyword in text)
    if body_part_hits < _POSE_MIN_BODY_PARTS:
        missing.append(f"肢位表（肢体关键词 {body_part_hits}/{_POSE_MIN_BODY_PARTS}）")
    if not any(keyword in text for keyword in _POSE_COMPLETENESS_KEYWORDS):
        missing.append("易掉肢体完整性/出框声明")
    if not (
        any(keyword in text for keyword in _POSE_SHOT_SIZE_KEYWORDS)
        and any(keyword in text for keyword in _POSE_FRAME_KEYWORDS)
    ):
        missing.append("取景下界（景别+入画声明）")
    if missing:
        raise DraftViolation(
            f"{label} 的 L4+ 单元正文缺姿态规格项: {'、'.join(missing)}；"
            "按七条款扩写（骨架声明/肢位表/完整性声明/运动几何化/取景下界/措辞表/姿态锁）",
            code="pose_spec_missing",
            label=label,
        )


def validate_pose_lock(label: str, text: str, previous_skeleton: str | None) -> None:
    """姿态锁：连续 L4+ 单元的标记行须与上一 L4+ 单元逐字一致（空白归一后）。

    ``previous_skeleton`` 是紧邻前一 L4+ 单元的标记行；None 表示链已断（无前一 L4+ 单元，
    或中间隔着非接触单元——unit 边界即换体位的合法位置）。无标记单元静默，调用方据此
    重置链。
    """
    marker = contact_pose_marker(text)
    if marker is None:
        return
    if previous_skeleton is not None and _normalize_for_anchor(marker) != _normalize_for_anchor(previous_skeleton):
        raise DraftViolation(
            f"{label} 的姿态锁被破坏：标记行与前一 L4+ 单元不一致"
            f"（前：{previous_skeleton[:40]!r} / 本：{marker[:40]!r}）；"
            "连续 L4+ 单元骨架句须逐字一致，换体位必须在独立 unit 边界",
            code="pose_lock_broken",
            label=label,
        )


def validate_mention_closure(label: str, plan_text: str, prompt_text: str, project: dict[str, Any]) -> None:
    """提及集合闭环：prompt 正文的每个 ``@`` 提及须来自 plan 对应单元或角色表。

    plan 是离源文最近的已审层：只出现在 prompt 里的新角色是幽灵名字类语义漂移
    （「瑟琳」事件——上游各层 0 处、prompt 层凭空出现）。已登记名字恒允许：同一角色
    可换衍生写法；未登记名字已由 ``unregistered_asset`` / ``unregistered_reference_derivative``
    拦下，本检查只判「plan 外」这一维度。
    """
    catalog = build_reference_catalog(project)
    allowed = set(mention_names(plan_text)) | set(catalog.reference_names("character"))
    bad = sorted(set(mention_names(prompt_text)) - allowed)
    if bad:
        raise DraftViolation(
            f"{label} 引入了脚本规划中没有的角色: {bad}；正文角色引用须来自 plan 对应单元或已登记资产，不得新增角色",
            code="unplanned_character",
            label=label,
        )


def validate_pose_plan_alignment(label: str, plan_text: str, prompt_text: str) -> None:
    """体位契约逐字化：prompt 的 L4+ 标记行须与 plan 对应单元标记行逐字一致。

    plan 是体位的唯一决策点（对着源文判），prompt 只继承不改写——把「谁在上」这类
    语义判断从下游的自由扩写层移到逐字可比字段上。双向对称：prompt 有标记而 plan 无
    → ``pose_plan_missing``；两侧标记不一致 → ``pose_plan_drift``；plan 有标记而 prompt
    无 → 同 ``pose_plan_missing``（堵「prompt 侧漏写标记」盲区的一半）。两侧都无标记静默
    （向后兼容）。
    """
    plan_marker = pose_mark_token(plan_text)
    prompt_marker = pose_mark_token(prompt_text)
    if prompt_marker is not None:
        if plan_marker is None:
            raise DraftViolation(
                f"{label} 是 L4+ 单元，但对应脚本规划单元没有姿态标记行；"
                "plan 是体位唯一决策点，先在 plan 补标记行再扩写",
                code="pose_plan_missing",
                label=label,
            )
        if _normalize_for_anchor(prompt_marker) != _normalize_for_anchor(plan_marker):
            raise DraftViolation(
                f"{label} 的姿态标记行与脚本规划漂移（plan：{plan_marker[:40]!r} / 本：{prompt_marker[:40]!r}）；"
                "标记行须逐字继承 plan，禁止改写体位",
                code="pose_plan_drift",
                label=label,
            )
        return
    if plan_marker is not None:
        raise DraftViolation(
            f"{label} 对应脚本规划的 L4+ 单元，但正文缺姿态标记行；逐字继承 plan 的标记行",
            code="pose_plan_missing",
            label=label,
        )


#: plan 侧标记行良构：`[接触 L4/L5 · 体位名 · 支撑：支撑点]`，三段齐备才算声明过。
_PLAN_POSE_MARK_RE = re.compile(r"^\[接触\s*L[45]\s*[·・.．]\s*\S+\s*[·・.．]\s*支撑：.+\]$")


def validate_plan_pose_marker(label: str, text: str) -> None:
    """plan 侧标记行良构：正文声明了接触标记就必须是完整的 L4/L5 三段式。

    plan 是逐字锚层（正文 = 源文逐字副本 + 标记行），关系词黑名单不作用于 plan
    （源文措辞不是判定对象）；此处只判标记行本身——L4 以下单元不该写标记，写了即
    良构违约。无标记单元静默（向后兼容）。
    """
    token = pose_mark_token(text)
    if token is None:
        return
    if not _PLAN_POSE_MARK_RE.match(token):
        raise DraftViolation(
            f"{label} 的 plan 标记行不完整: {token!r}；"
            "格式须为 `[接触 L4/L5 · 体位名 · 支撑：支撑点]`，L4 以下单元不写标记",
            code="plan_pose_marker_malformed",
            label=label,
        )


__all__ = [
    "SPEECH_OVERFLOW_TOLERANCE",
    "DraftViolation",
    "DraftViolations",
    "assert_dialogue_preserved",
    "collect_violations",
    "contact_pose_marker",
    "dialogue_speakers",
    "is_verbatim_source_anchor",
    "normative_lines",
    "pose_mark_token",
    "render_violation_report",
    "validate_dialogue_load",
    "validate_mention_closure",
    "validate_plan_pose_marker",
    "validate_pose_lock",
    "validate_pose_plan_alignment",
    "validate_pose_spec",
    "validate_source_text_anchor",
    "validate_unit_text",
    "violation_items",
]
