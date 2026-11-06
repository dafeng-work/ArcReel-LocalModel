"""参考生视频扁平草稿结构的机械校验（script_plan / prompt_authoring 共用）。"""

import unicodedata

import pytest

from lib.script.draft_quarantine import violation_entries
from lib.script.reference_video.draft_validation import (
    DraftViolation,
    DraftViolations,
    assert_dialogue_preserved,
    contact_pose_marker,
    dialogue_speakers,
    normative_lines,
    pose_mark_token,
    validate_dialogue_load,
    validate_mention_closure,
    validate_plan_pose_marker,
    validate_pose_lock,
    validate_pose_plan_alignment,
    validate_pose_spec,
    validate_source_text_anchor,
    validate_unit_text,
)

PROJECT = {
    "characters": {"李明": {}, "王五": {}},
    "scenes": {"酒馆": {}},
    "props": {"长剑": {}},
}

#: 角色带衍生的项目载荷：``@[角色/衍生]`` 的登记判定用例（docs/adr/0072）。
DERIVATIVE_PROJECT = {
    "characters": {"李明": {"derivatives": {"劲装": {}}}, "王五": {}},
    "scenes": {"酒馆": {}},
    "props": {"长剑": {}},
}

#: 带组合附加符的角色名（越南语），两种编码屏幕显示相同、字节不同——资产名比对的坐标系用例。
_NAME_NFC = unicodedata.normalize("NFC", "Hiếu")
_NAME_NFD = unicodedata.normalize("NFD", "Hiếu")


class TestSourceTextAnchor:
    def test_verbatim_substring_accepted(self):
        assert (
            validate_source_text_anchor("unit E1U01", "李明推开酒馆的门", "夜色深沉。李明推开酒馆的门，环视四周。")
            is None
        )

    def test_whitespace_differences_tolerated(self):
        """空白折叠后比对：换行 / 缩进的还原不可靠，但删字改字必须被抓住。"""
        assert (
            validate_source_text_anchor("unit E1U01", "李明推开\n  酒馆的门", "李明推开 酒馆的门，环视四周。") is None
        )

    def test_unicode_form_differences_tolerated(self):
        """源文以 NFD 落盘、模型回写 NFC（组合附加符语种常见）不算改写：两侧先归一到 NFC。"""
        novel = unicodedata.normalize("NFD", "Anh ấy mở cửa quán rượu.")
        anchor = unicodedata.normalize("NFC", "Anh ấy mở cửa")
        assert validate_source_text_anchor("unit E1U01", anchor, novel) is None

    def test_rewritten_text_rejected(self):
        with pytest.raises(DraftViolation, match="不是小说原文的逐字片段"):
            validate_source_text_anchor("unit E1U01", "李明走进了酒馆", "李明推开酒馆的门。")

    def test_blank_anchor_rejected(self):
        with pytest.raises(DraftViolation, match="source_text 为空"):
            validate_source_text_anchor("unit E1U01", "   ", "李明推开酒馆的门。")


class TestUnitText:
    def test_mixed_speech_is_a_structured_planning_violation(self):
        with pytest.raises(DraftViolations) as exc_info:
            validate_unit_text(
                "unit E1U01",
                "镜头1：门被推开\n@[李明]：{快走。}\n{风吹过旷野。}",
                PROJECT,
                max_refs=None,
            )

        problem = exc_info.value.items[0]
        assert problem.code == "mixed_speech"
        assert problem.label == "unit E1U01"
        assert violation_entries([problem]) == [
            {
                "code": "mixed_speech",
                "label": "unit E1U01",
                "message": str(problem),
                "line": None,
                "locations": [
                    {"path": ["text"], "line": 1},
                    {"path": ["text"], "line": 2},
                ],
                "reason": "character_and_narrator_mixed",
                "action": "replan_unit",
            }
        ]
        assert "character_and_narrator_mixed" in str(problem)
        assert "replan_unit" in str(problem)

    def test_derives_references_in_first_mention_order(self):
        refs = validate_unit_text(
            "unit E1U01",
            "@[李明] 推开 @[酒馆] 的门\n@[李明] 放下 @[长剑]",
            PROJECT,
            max_refs=None,
        )
        assert [(r.type, r.name) for r in refs] == [
            ("character", "李明"),
            ("scene", "酒馆"),
            ("prop", "长剑"),
        ]

    def test_dialogue_speaker_not_a_reference_image(self):
        """台词记号的说话人位只驱动音色声明，不进参考图（画外说话的角色不该被画进来）。"""
        refs = validate_unit_text("unit E1U01", "镜头1：门在风里晃动\n@[李明]：{我来了。}", PROJECT, max_refs=None)
        assert refs == []

    def test_blank_text_rejected(self):
        with pytest.raises(DraftViolation, match="正文为空"):
            validate_unit_text("unit E1U01", "   \n  ", PROJECT, max_refs=None)

    def test_unclosed_brace_rejected(self):
        with pytest.raises(DraftViolation, match="未闭合的花括号") as exc_info:
            validate_unit_text("unit E1U01", "镜头1：@[李明] 说 {我来了", PROJECT, max_refs=None)
        assert exc_info.value.line == 0

    def test_fullwidth_braces_rejected_carries_line(self):
        with pytest.raises(DraftViolation, match="全角花括号") as exc_info:
            validate_unit_text("unit E1U01", "镜头1：门开了\n@[李明]：｛我来了。｝", PROJECT, max_refs=None)
        assert exc_info.value.line == 1

    def test_brace_in_description_rejected(self):
        """没被识别成发声记号的花括号仍判违约：空台词会派生出没有内容的发声。"""
        with pytest.raises(DraftViolation, match="画面描述里使用了花括号"):
            validate_unit_text("unit E1U01", "镜头1：@[李明] 推门，音量 {}", PROJECT, max_refs=None)

    def test_inline_speech_marks_are_accepted(self):
        """台词与画外音写在同一行的画面描述之后照常放行；说话人位不进参考图。"""
        refs = validate_unit_text(
            "unit E1U01", "镜头1：@[李明] 推开 @[酒馆] 木门。@[李明]{我来了}", PROJECT, max_refs=None
        )
        assert [r.name for r in refs] == ["李明", "酒馆"]

    def test_unregistered_mention_rejected(self):
        with pytest.raises(DraftViolation, match="未登记的资产名"):
            validate_unit_text("unit E1U01", "镜头1：@[路人甲] 走过", PROJECT, max_refs=None)

    def test_unregistered_speaker_rejected(self):
        with pytest.raises(DraftViolation, match="说话人未登记"):
            validate_unit_text("unit E1U01", "镜头1：门开了\n@[无名氏]：{我来了。}", PROJECT, max_refs=None)

    def test_registered_derivative_accepted_in_a_mention_and_in_the_speaker_slot(self):
        """``@[角色/衍生]`` 与 ``@[角色/衍生]{台词}``：登记过的衍生两处都放行。"""
        refs = validate_unit_text(
            "unit E1U01", "镜头1：@[李明/劲装] 推门。@[李明/劲装]{我来了}", DERIVATIVE_PROJECT, max_refs=None
        )

        assert [(r.type, r.name) for r in refs] == [("character", "李明/劲装")]

    def test_unregistered_derivative_in_a_mention_rejected(self):
        with pytest.raises(DraftViolation, match="未登记的资产名"):
            validate_unit_text("unit E1U01", "镜头1：@[李明/夜行衣] 推门", DERIVATIVE_PROJECT, max_refs=None)

    def test_unregistered_derivative_in_the_speaker_slot_rejected(self):
        """说话人位不进参考图派生，未登记的事实要在这里单独抓住。"""
        with pytest.raises(DraftViolation, match="写了该角色没有的衍生") as excinfo:
            validate_unit_text("unit E1U01", "镜头1：门开了\n@[李明/夜行衣]{我来了}", DERIVATIVE_PROJECT, max_refs=None)

        assert excinfo.value.code == "unregistered_speaker_derivative"

    def test_derivative_speaker_is_the_base_character(self):
        """衍生说话仍绑本体的声音，故 speaker 位不因写了衍生而被判未登记角色。"""
        assert dialogue_speakers("@[李明/劲装]{我来了}") == ["李明"]

    @pytest.mark.parametrize("registered", [_NAME_NFC, _NAME_NFD], ids=["登记NFC", "登记NFD"])
    @pytest.mark.parametrize("written", [_NAME_NFC, _NAME_NFD], ids=["出场NFC", "出场NFD"])
    def test_combining_char_name_passes_in_every_encoding_pairing(self, registered: str, written: str):
        """组合字符角色名的四种 NFC/NFD 配对判定一致：肉眼同字，不该有任一组合被判未登记。

        画面描述 mention 与台词记号说话人同时出现——两者走不同的比对点（引用派生 / 说话人登记），
        任一处漏归一都会在这里以不同的违约 code 冒出来。
        """
        project = {"characters": {registered: {}}, "scenes": {}, "props": {}}
        text = f"镜头1：@[{written}] 推门而入\n@[{written}]：{{Tôi đến rồi.}}"

        refs = validate_unit_text("unit E1U01", text, project, max_refs=None)

        # 派生出的引用一律是归一形式：下游拿它回查资产表、在正文里替换成主体记号，须与此处同形
        assert [(r.type, r.name) for r in refs] == [("character", _NAME_NFC)]

    def test_unregistered_mention_still_rejected_after_normalization(self):
        """归一只消除编码形式差异，不放宽登记判定：真没登记的名字照常拒。"""
        project = {"characters": {_NAME_NFC: {}}, "scenes": {}, "props": {}}
        with pytest.raises(DraftViolation, match="未登记的资产名"):
            validate_unit_text("unit E1U01", f"镜头1：@[{_NAME_NFD}Ⅱ] 推门而入", project, max_refs=None)

    def test_over_max_refs_rejected(self):
        with pytest.raises(DraftViolation, match="超过模型上限"):
            validate_unit_text("unit E1U01", "镜头1：@[李明] 与 @[王五] 在 @[酒馆]", PROJECT, max_refs=2)

    def test_fullwidth_braces_rejected(self):
        """全角花括号不被发声记号语法识别，放行会让台词静默降级成描述、说话人反被派生成参考图。"""
        with pytest.raises(DraftViolation, match="全角花括号"):
            validate_unit_text("unit E1U01", "镜头1：门开了\n@[李明]：｛我来了。｝", PROJECT, max_refs=None)

    def test_dialogue_without_braces_rejected(self):
        """漏花括号的台词会被当成画面描述：台词整句消失、说话人反被派生成参考图。"""
        with pytest.raises(DraftViolation, match="台词写法不合法"):
            validate_unit_text("unit E1U01", "镜头1：门开了\n@[李明]：我来了。", PROJECT, max_refs=None)

    def test_dialogue_followed_by_description_is_accepted(self):
        """行首台词后接描述不再判违约：记号可写在行内任意位置，其余是画面描述。"""
        refs = validate_unit_text("unit E1U01", "镜头1：门开了\n@[李明]：{我来了}，然后转身", PROJECT, max_refs=None)
        assert [r.name for r in refs] == []

    def test_non_character_mention_with_colon_is_a_description(self):
        """场景 / 道具做小标题是合法的画面描述写法，不能按「@[名称]：」形态一概判成写坏的台词。"""
        refs = validate_unit_text("unit E1U01", "镜头1：@[酒馆]：木门被风吹开，灯笼摇晃", PROJECT, max_refs=None)
        assert [(r.type, r.name) for r in refs] == [("scene", "酒馆")]

    def test_fullwidth_mention_delimiters_rejected(self):
        """全角 `＠` / `［］` 不被 mention 语法识别：参考图会从视频请求里静默消失。"""
        with pytest.raises(DraftViolation, match="写坏的资产引用"):
            validate_unit_text("unit E1U01", "镜头1：@［李明］ 推开门", PROJECT, max_refs=None)

    def test_malformed_mention_rejected(self):
        """写坏的 `@[` 既不进 references，又会原样进入供应商请求（渲染只替换认得的 mention）。"""
        with pytest.raises(DraftViolation, match="写坏的资产引用"):
            validate_unit_text("unit E1U01", "镜头1：@[李明 推开门", PROJECT, max_refs=None)

    def test_empty_mention_rejected(self):
        with pytest.raises(DraftViolation, match="写坏的资产引用"):
            validate_unit_text("unit E1U01", "镜头1：@[] 推开门", PROJECT, max_refs=None)

    def test_dialogue_only_text_rejected(self):
        """只有台词的正文没有可生成的画面：画面是 unit 要产出的东西，不能只有声音。"""
        with pytest.raises(DraftViolation, match="没有画面描述"):
            validate_unit_text("unit E1U01", "@[李明]：{我来了。}\n{风吹过。}", PROJECT, max_refs=None)

    def test_dialogue_written_on_shot_header_line_is_normative(self):
        """写在 ``镜头N：`` 同一行的台词在切分后就是规范行，判定须在剥 header 之后。"""
        refs = validate_unit_text("unit E1U01", "镜头1：@[李明]：{我来了。}\n门在风里晃动", PROJECT, max_refs=None)
        assert refs == []


class TestDialogueLoad:
    def test_within_budget_accepted(self):
        assert validate_dialogue_load("unit E1U01", "镜头1：门开了\n@[李明]：{我来了。}", 4, "zh") is None

    def test_overload_rejected(self):
        long_line = "这是一段非常长的台词" * 6  # 60 字 ÷ 5 字/秒 ≈ 12 秒
        with pytest.raises(DraftViolation, match="超过该 unit"):
            validate_dialogue_load("unit E1U01", f"镜头1：门开了\n@[李明]：{{{long_line}}}", 4, "zh")

    def test_tolerance_admits_slight_overrun(self):
        """宽容系数内放行：语速是统计估算，「刚好写满」的正常产出不该被判违约。"""
        # 21 字 ÷ 5 字/秒 = 4.2 秒，落在 4 秒 × 1.2 = 4.8 秒的宽容上限内
        line = "一二三四五六七八九十一二三四五六七八九十。"
        assert validate_dialogue_load("unit E1U01", f"@[李明]：{{{line}}}", 4, "zh") is None

    def test_voiceover_counts_toward_budget(self):
        long_line = "画外音很长很长的一段" * 6
        with pytest.raises(DraftViolation, match="超过该 unit"):
            validate_dialogue_load("unit E1U01", f"镜头1：空镜\n{{{long_line}}}", 4, "zh")

    def test_project_override_changes_budget(self):
        """项目级语速覆盖生效：同一段台词在慢速覆盖下判超载、在快速覆盖下放行。"""
        text = "镜头1：门开了\n@[李明]：{一二三四五六七八九十一二三四五六七八九十。}"
        with pytest.raises(DraftViolation, match="超过该 unit"):
            validate_dialogue_load("unit E1U01", text, 4, "zh", 2.0)
        validate_dialogue_load("unit E1U01", text, 4, "zh", 10.0)

    def test_non_string_language_falls_back_to_default_rate(self):
        """project.json 的 source_language 可能是脏数据：估算按默认语速走，不抛 AttributeError。"""
        assert validate_dialogue_load("unit E1U01", "@[李明]：{我来了。}", 4, 123) is None

    def test_normalizes_unicode_before_estimating(self):
        """NFD 台词先归一再估：组合附加符会被词计数拆成多个单位，不归一会把念得完的 unit 判超载。"""
        line = "Anh ấy mở cửa quán rượu ngay lập tức"
        text = f"镜头1：@[李明] 推门\n@[李明]：{{{unicodedata.normalize('NFD', line)}}}"
        assert validate_dialogue_load("unit E1U01", text, 4, "vi") is None


class TestNormativeLines:
    def test_extracts_dialogue_and_voiceover_in_order(self):
        text = "镜头1：门开了\n@[李明]：{我来了。}\n{夜色深沉。}"
        assert normative_lines(text) == [
            ("dialogue", "李明", "我来了。"),
            ("voiceover", "", "夜色深沉。"),
        ]

    def test_dialogue_speakers_deduped_in_first_seen_order(self):
        text = "@[王五]：{在。}\n@[李明]：{我来了。}\n@[王五]：{知道了。}"
        assert dialogue_speakers(text) == ["王五", "李明"]


class TestDialoguePreserved:
    SCRIPT_PLAN = "镜头1：@[李明] 推门\n@[李明]：{我来了。}"

    def test_description_expansion_accepted(self):
        assert_dialogue_preserved(
            "unit E1U01",
            self.SCRIPT_PLAN,
            "镜头1：中景，平视。@[李明] 推开 @[酒馆] 的门，跨过门槛\n@[李明]：{我来了。}",
        )

    def test_unicode_form_difference_not_a_rewrite(self):
        """script_plan 存 NFD、prompt_authoring 回写 NFC 是纯编码差异，不该把已付费的展开判成改词。"""
        line = "Anh ấy mở cửa"
        script_plan = f"镜头1：@[李明] 推门\n@[李明]：{{{unicodedata.normalize('NFD', line)}}}"
        prompt_authoring = f"镜头1：中景。@[李明] 推开木门\n@[李明]：{{{unicodedata.normalize('NFC', line)}}}"
        assert_dialogue_preserved("unit E1U01", script_plan, prompt_authoring)

    def test_rewritten_dialogue_rejected(self):
        with pytest.raises(DraftViolation, match="第 1 条台词被改写"):
            assert_dialogue_preserved("unit E1U01", self.SCRIPT_PLAN, "镜头1：@[李明] 推门\n@[李明]：{我到了。}")

    def test_speaker_change_rejected(self):
        with pytest.raises(DraftViolation, match="第 1 条台词被改写"):
            assert_dialogue_preserved("unit E1U01", self.SCRIPT_PLAN, "镜头1：@[李明] 推门\n@[王五]：{我来了。}")

    def test_added_dialogue_rejected(self):
        with pytest.raises(DraftViolation, match="台词条数被改动"):
            assert_dialogue_preserved(
                "unit E1U01", self.SCRIPT_PLAN, "镜头1：@[李明] 推门\n@[李明]：{我来了。}\n{夜色深沉。}"
            )

    def test_dropped_dialogue_rejected(self):
        with pytest.raises(DraftViolation, match="台词条数被改动"):
            assert_dialogue_preserved("unit E1U01", self.SCRIPT_PLAN, "镜头1：@[李明] 推门")


class TestPoseContract:
    """L4+ 显性接触姿态契约机检：标记触发 + 三个新违约码。

    触发条件 = 正文含 `[接触 L4/L5 · …]` 标记行；无标记单元全部新码静默（向后兼容）。
    标记契约载体是 `text` 正文首行——`source_text` 是逐字锚，校验器不允许任何附加。
    """

    MARK = "[接触 L4 · 传教士体位 · 支撑：双膝+双臂]"
    #: 骨架句 = 标记行整行（标记 + 上下位 + 支撑点）：姿态锁比对的是整行，不是光秃秃的 `[…]` token。
    SKELETON = MARK + " @[李明] 位于她上方，双膝跪在床面、双臂撑在她头侧床面、骨盆与她的骨盆对齐贴合。"

    def _good_text(self) -> str:
        return (
            f"{self.SKELETON}\n"
            "她的头枕在枕面、双臂环过他后颈、双手插进他后发、双腿打开、双脚脚掌踩在床面、骨盆与他对齐；"
            "他的头低靠她肩侧、双臂撑床、双膝跪在床面。\n"
            "她的双腿完整、双脚踩在床面全程保持在画面内。\n"
            "运镜：固定中景，双人全身入画。"
        )

    # -- contact_pose_marker ------------------------------------------------

    def test_skeleton_line_extracted(self):
        assert contact_pose_marker(self._good_text()) == self.SKELETON

    def test_marker_absent_returns_none(self):
        assert contact_pose_marker("@[李明] 推开酒馆的门") is None

    def test_marker_separator_variants_accepted(self):
        """分隔符形态随输入法漂移（· / ・ / . / ．），标记识别不能跟着漂。"""
        assert contact_pose_marker("[接触 L4·传教士体位 · 支撑：双膝]") is not None
        assert contact_pose_marker("[接触L5. 高潮段]") is not None
        assert contact_pose_marker("[接触 L3 · 吻]") is None

    # -- pose_spec_missing ----------------------------------------------------

    def test_full_spec_accepted(self):
        assert validate_pose_spec("unit E1U01", self._good_text()) is None

    def test_missing_limb_table_rejected(self):
        text = (
            f"{self.MARK} @[李明] 位于她上方。\n"
            "她的双腿完整、双脚踩在床面全程保持在画面内。\n"
            "运镜：固定中景，双人全身入画。"
        )
        with pytest.raises(DraftViolation, match="肢位表") as exc_info:
            validate_pose_spec("unit E1U01", text)
        assert exc_info.value.code == "pose_spec_missing"

    def test_missing_completeness_declaration_rejected(self):
        text = (
            f"{self.MARK} @[李明] 位于她上方，双膝跪在床面、双臂撑床、骨盆贴合。\n"
            "她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐；他的头低靠、双臂撑床、双膝跪床。\n"
            "运镜：固定中景，双人全身入画。"
        )
        with pytest.raises(DraftViolation, match="完整性/出框声明") as exc_info:
            validate_pose_spec("unit E1U01", text)
        assert exc_info.value.code == "pose_spec_missing"

    def test_missing_frame_lower_bound_rejected(self):
        text = (
            f"{self.MARK} @[李明] 位于她上方，双膝跪在床面、双臂撑床、骨盆贴合。\n"
            "她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐；她的双腿完整、双脚踩在床面全程保持在画面内。\n"
            "运镜：缓慢推近。"
        )
        with pytest.raises(DraftViolation, match="取景下界") as exc_info:
            validate_pose_spec("unit E1U01", text)
        assert exc_info.value.code == "pose_spec_missing"

    def test_below_l4_text_silent(self):
        """无标记单元（L4 以下）静默：七条款骨架下界不生效，向后兼容。"""
        assert validate_pose_spec("unit E1U01", "@[李明] 与 @[王五] 在 @[酒馆] 对坐喝酒") is None

    # -- relational_word_found（经 validate_unit_text 全链路） -------------------

    def test_blacklist_word_in_marked_unit_rejected(self):
        text = (
            f"{self.MARK} @[李明] 与她在 @[酒馆] 的床面上身体交叠。\n"
            "她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐；她的双腿完整、双脚踩在床面全程保持在画面内。\n"
            "运镜：固定中景，双人全身入画。"
        )
        with pytest.raises(DraftViolation, match="关系词黑名单") as exc_info:
            validate_unit_text("unit E1U01", text, PROJECT, max_refs=None)
        assert exc_info.value.code == "relational_word_found"

    def test_blacklist_word_without_marker_silent(self):
        """无标记单元命中黑名单词不判违约：契约只约束自证 L4+ 的单元。"""
        refs = validate_unit_text("unit E1U01", "@[李明] 与 @[王五] 在 @[酒馆] 身体交叠", PROJECT, max_refs=None)
        assert [r.name for r in refs] == ["李明", "王五", "酒馆"]

    # -- pose_lock_broken ------------------------------------------------------

    def test_identical_skeleton_accepted(self):
        assert validate_pose_lock("unit E1U02", self._good_text(), self.SKELETON) is None

    def test_whitespace_difference_tolerated(self):
        """空白 / 编码形式差异不算漂移：两侧按同一归一口径比对。"""
        drifted = self.SKELETON.replace(" ", "  ")
        assert validate_pose_lock("unit E1U02", drifted, self.SKELETON) is None

    def test_drifted_skeleton_rejected(self):
        with pytest.raises(DraftViolation, match="姿态锁被破坏") as exc_info:
            validate_pose_lock("unit E1U02", "[接触 L4 · 侧卧体位 · 支撑：左肘] 其余正文……", self.MARK)
        assert exc_info.value.code == "pose_lock_broken"

    def test_chain_reset_by_non_contact_unit(self):
        """非接触单元是 unit 边界：链重置后换骨架合法，不再判漂移。"""
        assert validate_pose_lock("unit E1U03", "[接触 L4 · 侧卧体位 · 支撑：左肘] 其余正文……", None) is None

    def test_unmarked_unit_silent(self):
        assert validate_pose_lock("unit E1U02", "@[李明] 转身离开", self.MARK) is None


class TestPoseMarkToken:
    """``pose_mark_token``：取括号段 token（不含行内后续骨架句），供 plan↔prompt 逐字对齐。"""

    def test_token_extracted_without_skeleton(self):
        text = "[接触 L4 · 传教士体位 · 支撑：双膝+双臂] @[李明] 位于她上方，双膝跪在床面。"
        assert pose_mark_token(text) == "[接触 L4 · 传教士体位 · 支撑：双膝+双臂]"

    def test_token_only_line(self):
        assert pose_mark_token("[接触 L5 · 侧卧 · 支撑：左肘]") == "[接触 L5 · 侧卧 · 支撑：左肘]"

    def test_no_marker_returns_none(self):
        assert pose_mark_token("镜头1：@[李明] 推门") is None


class TestDerivativeHardCheck:
    """P0-1 衍生名硬校验：裸 ``@本体/衍生`` 的衍生段必须登记在该本体名下。

    解析器 legacy 字符集不含 ``/``，裸提及在斜线处停笔——未登记衍生（如男性专属衍生挂到
    女性角色）静默回落到本体、漏过 missing。``pending_derivative_references`` 恢复完整引用
    名，``validate_unit_text`` 据此判登记；包裹形式 ``@[本体/衍生]`` 仍走 missing 判定。
    """

    def test_registered_bare_derivative_accepted(self):
        """裸 ``@李明/劲装``：劲装已登记在李明名下，不判未登记衍生。"""
        validate_unit_text("unit E1U01", "镜头1：@李明/劲装 推门", DERIVATIVE_PROJECT, max_refs=None)

    def test_unregistered_bare_derivative_rejected(self):
        """裸 ``@李明/夜行衣``：夜行衣没登记在李明名下 → unregistered_reference_derivative。"""
        with pytest.raises(DraftViolation, match="引用了未登记的衍生") as exc_info:
            validate_unit_text("unit E1U01", "镜头1：@李明/夜行衣 推门", DERIVATIVE_PROJECT, max_refs=None)
        assert exc_info.value.code == "unregistered_reference_derivative"

    def test_wrapped_unregistered_derivative_uses_missing_path(self):
        """包裹形式 ``@[李明/夜行衣]`` 由 missing 判定拦（unregistered_asset），不走衍生硬校验。"""
        with pytest.raises(DraftViolation, match="未登记的资产名") as exc_info:
            validate_unit_text("unit E1U01", "镜头1：@[李明/夜行衣] 推门", DERIVATIVE_PROJECT, max_refs=None)
        assert exc_info.value.code == "unregistered_asset"


class TestMentionClosure:
    """P0-2 提及集合闭环：prompt 正文的每个 ``@`` 提及须来自 plan 对应单元或已登记角色表。

    已登记名字恒允许（同一角色可换衍生写法）；未登记名字已由 ``unregistered_asset`` /
    ``unregistered_reference_derivative`` 拦下，本检查只判「plan 外」这一维度——幽灵名字类漂移。
    """

    def test_prompt_stays_within_plan_mention_set(self):
        plan = "镜头1：@[李明] 在 @[酒馆] 推门\n@[李明]：{我来了。}"
        prompt = "镜头1：@[李明] 推开 @[酒馆] 的门"
        assert validate_mention_closure("unit E1U01", plan, prompt, PROJECT) is None

    def test_ghost_character_in_prompt_rejected(self):
        """「瑟琳」事件：上游各层 0 处、prompt 层凭空出现的未登记新角色。"""
        plan = "镜头1：@[李明] 推门\n@[李明]：{我来了。}"
        prompt = "镜头1：@[李明] 与 @瑟琳 在 @[酒馆]"
        with pytest.raises(DraftViolation, match="引入了脚本规划中没有的角色") as exc_info:
            validate_mention_closure("unit E1U01", plan, prompt, PROJECT)
        assert exc_info.value.code == "unplanned_character"
        assert "瑟琳" in str(exc_info.value)

    def test_registered_character_not_in_plan_allowed(self):
        """已登记角色恒允许：plan 只写了李明，prompt 出现已登记的王五不算新增角色。"""
        plan = "镜头1：@[李明] 推门"
        prompt = "镜头1：@[王五] 接过东西"
        assert validate_mention_closure("unit E1U01", plan, prompt, PROJECT) is None


class TestPosePlanAlignment:
    """P1 体位契约逐字化：prompt 的 L4+ 标记行须与 plan 标记行逐字一致（NFC 归一比）。

    双向对称：prompt 有标记而 plan 无 → ``pose_plan_missing``；两侧不一致 → ``pose_plan_drift``；
    plan 有标记而 prompt 无 → ``pose_plan_missing``。两侧都无标记静默（向后兼容）。
    """

    PLAN_MARK = "[接触 L4 · 传教士体位 · 支撑：双膝+双臂] @[李明] 位于她上方，双膝跪在床面。"
    PLAN = f"{PLAN_MARK}\n她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐。"

    def test_matching_marker_accepted(self):
        prompt = f"{self.PLAN_MARK}\n她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐。"
        assert validate_pose_plan_alignment("unit E1U01", self.PLAN, prompt) is None

    def test_prompt_marker_plan_silent_rejected(self):
        """prompt 有标记而 plan 无：plan 是体位唯一决策点，先在 plan 补标记行再扩写。"""
        prompt = f"{self.PLAN_MARK}\n她的头枕枕面……"
        with pytest.raises(DraftViolation, match="没有姿态标记行") as exc_info:
            validate_pose_plan_alignment("unit E1U01", "@[李明] 推门", prompt)
        assert exc_info.value.code == "pose_plan_missing"

    def test_marker_drift_rejected(self):
        """骑乘 vs 传教士：下游改写了 plan 决策的体位（集 10 U12–U15 漂移的形态）。"""
        drifted = "[接触 L4 · 骑乘 · 支撑：蒂法 双膝+双臂] @[李明] 位于她下方……"
        with pytest.raises(DraftViolation, match="与脚本规划漂移") as exc_info:
            validate_pose_plan_alignment("unit E1U01", self.PLAN, drifted)
        assert exc_info.value.code == "pose_plan_drift"

    def test_plan_marker_prompt_silent_rejected(self):
        """plan 有标记而 prompt 漏写标记：堵「prompt 侧漏写」盲区的一半。"""
        with pytest.raises(DraftViolation, match="缺姿态标记行") as exc_info:
            validate_pose_plan_alignment("unit E1U01", self.PLAN, "@[李明] 推门")
        assert exc_info.value.code == "pose_plan_missing"

    def test_both_unmarked_silent(self):
        assert validate_pose_plan_alignment("unit E1U01", "@[李明] 推门", "@[李明] 转身") is None


class TestPlanPoseMarker:
    """P1 plan 侧标记行良构：正文声明了接触标记就必须是完整的 L4/L5 三段式。

    plan 是逐字锚层，关系词黑名单不作用于 plan（源文措辞不是判定对象）；此处只判标记行
    本身——L4 以下单元不该写标记，写了即良构违约。无标记单元静默（向后兼容）。
    """

    def test_valid_marker_accepted(self):
        text = "[接触 L4 · 传教士体位 · 支撑：双膝+双臂] @[李明] 位于她上方……"
        assert validate_plan_pose_marker("unit E1U01", text) is None

    def test_l3_marker_rejected(self):
        """L4 以下单元不该写标记：L3 不满足 ``L[45]``。"""
        with pytest.raises(DraftViolation, match="plan 标记行不完整") as exc_info:
            validate_plan_pose_marker("unit E1U01", "[接触 L3 · 吻 · 支撑：无] 正文……")
        assert exc_info.value.code == "plan_pose_marker_malformed"

    def test_missing_support_segment_rejected(self):
        with pytest.raises(DraftViolation, match="plan 标记行不完整") as exc_info:
            validate_plan_pose_marker("unit E1U01", "[接触 L4 · 传教士体位] 正文……")
        assert exc_info.value.code == "plan_pose_marker_malformed"

    def test_missing_pose_segment_rejected(self):
        with pytest.raises(DraftViolation, match="plan 标记行不完整") as exc_info:
            validate_plan_pose_marker("unit E1U01", "[接触 L4 · 支撑：双膝] 正文……")
        assert exc_info.value.code == "plan_pose_marker_malformed"

    def test_unmarked_unit_silent(self):
        assert validate_plan_pose_marker("unit E1U01", "镜头1：@[李明] 推门") is None


class TestRelationalWordCheckFlag:
    """``relational_word_check`` flag：plan 侧逐字锚层传 False，源文措辞不得被判关系词违约。

    关系词黑名单只作用于 prompt_authoring 的自由扩写层；plan 正文 = 源文逐字副本 + 标记行，
    加标记不得让源文措辞被判违约。
    """

    MARKED_RELATIONAL = (
        "[接触 L4 · 传教士体位 · 支撑：双膝+双臂] @[李明] 位于她上方，两人身体交叠在床面。\n"
        "她的头枕枕面、双臂环过他后颈、双腿打开、双脚踩床面、骨盆对齐；双腿完整、双脚踩在床面全程保持在画面内。"
    )

    def test_plan_layer_skips_relational_check(self):
        """plan 侧（relational_word_check=False）：源文措辞含关系词不判违约。"""
        validate_unit_text("unit E1U01", self.MARKED_RELATIONAL, PROJECT, max_refs=None, relational_word_check=False)

    def test_prompt_layer_enforces_relational_check(self):
        """prompt 侧（默认 True）：L4+ 单元正文命中黑名单词判违约。"""
        with pytest.raises(DraftViolation, match="关系词黑名单") as exc_info:
            validate_unit_text("unit E1U01", self.MARKED_RELATIONAL, PROJECT, max_refs=None)
        assert exc_info.value.code == "relational_word_found"


class TestNeutralLayerReexport:
    """违约条目类型的定义处是路线中立的 ``lib.script.draft_violation``，本模块只再导出。

    分叉成两份类型定义时，``except DraftViolation`` 会按导入路径的不同静默漏接一半——三条
    路线的校验器与草稿信封都在同一条 except 上。import-linter 的「路线中立层不依赖参考
    生视频子包」契约管方向，本用例管同一性。
    """

    def test_symbols_are_the_neutral_layer_objects(self):
        from lib.script import draft_violation
        from lib.script.reference_video import draft_validation

        assert draft_validation.DraftViolation is draft_violation.DraftViolation
        assert draft_validation.DraftViolations is draft_violation.DraftViolations
        assert draft_validation.collect_violations is draft_violation.collect_violations
        assert draft_validation.render_violation_report is draft_violation.render_violation_report
        assert draft_validation.violation_items is draft_violation.violation_items
