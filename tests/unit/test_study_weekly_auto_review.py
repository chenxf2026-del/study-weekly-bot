"""test_study_weekly_auto_review.py — 群里分享标题含「周报」的云文档 → 免 @ 自动评审
(主理 2026-07-22)。覆盖:关键词读取 / 免@进入门槛判定 / scene 字段解析。

只测**进入门槛**的纯逻辑 (不碰网络): 真正拉文档 + 用真实标题二次核对在
_maybe_review_cloud_doc(require_title_keyword=...) 里, 属集成路径, 此处不覆盖。
"""
from __future__ import annotations

import json

import feishu_events as fe
import scene_loader as sl


def _text_msg(text: str) -> dict:
    return {"content": json.dumps({"text": text})}


def _post_link_msg(link_text: str) -> dict:
    # 飞书分享云文档 = 富文本 post, 链接的显示文本即文档标题 (tag=a)。
    return {"content": json.dumps({"content": [[{"tag": "a", "text": link_text, "href": "https://x.feishu.cn/docx/abc"}]]})}


class TestAutoReviewKeyword:
    def test_configured(self):
        assert fe._auto_review_keyword({"auto_review_title_keyword": "周报"}) == "周报"

    def test_empty_is_none(self):
        assert fe._auto_review_keyword({"auto_review_title_keyword": ""}) is None
        assert fe._auto_review_keyword({"auto_review_title_keyword": "  "}) is None

    def test_missing_is_none(self):
        assert fe._auto_review_keyword({}) is None
        assert fe._auto_review_keyword(None) is None


class TestShouldAutoReviewDoc:
    CFG = {"auto_review_title_keyword": "周报"}

    def test_doc_url_only_no_keyword_in_text_true(self):
        # ★真机场景: 飞书分享文档, 消息文本常只有 URL (标题不在文本里) → 靠 doc link 触发,
        #   关键词留给拉到真标题后核对 (2026-07-22 实测修复)。
        assert fe._should_auto_review_doc(
            _text_msg("https://x.feishu.cn/docx/AbCd1234"), self.CFG,
            is_group=True, at_bot=False) is True

    def test_keyword_in_text_no_url_true(self):
        # 纯文本提及关键词也放行 (即便无链接; _maybe_review_cloud_doc 无 ref 会空过)
        assert fe._should_auto_review_doc(
            _text_msg("这是我的周报"), self.CFG, is_group=True, at_bot=False) is True

    def test_post_link_title_has_keyword_true(self):
        assert fe._should_auto_review_doc(
            _post_link_msg("张路工作周报"), self.CFG, is_group=True, at_bot=False) is True

    def test_at_bot_false(self):
        # 已 @机器人 → 走显式路径, 不是"免@自动"
        assert fe._should_auto_review_doc(
            _text_msg("我的周报 https://x.feishu.cn/docx/AbCd1234"), self.CFG,
            is_group=True, at_bot=True) is False

    def test_p2p_false(self):
        # 单聊本就免@, 不走此路 (各自既有逻辑)
        assert fe._should_auto_review_doc(
            _text_msg("https://x.feishu.cn/docx/AbCd1234"), self.CFG,
            is_group=False, at_bot=False) is False

    def test_no_keyword_configured_false(self):
        assert fe._should_auto_review_doc(
            _text_msg("我的周报 https://x.feishu.cn/docx/AbCd1234"),
            {"auto_review_title_keyword": ""}, is_group=True, at_bot=False) is False

    def test_no_doc_no_keyword_false(self):
        # 无文档链接 + 文本不含关键词 → 不触发 (普通闲聊)
        assert fe._should_auto_review_doc(
            _text_msg("今天天气不错"), self.CFG, is_group=True, at_bot=False) is False


class TestSceneFieldParsed:
    def test_study_weekly_scene_has_keyword(self):
        # scene.yaml 的 auto_review_title_keyword 被 SceneAccess 正确解析
        scene = sl.load_scene("study-weekly-reflect")
        assert getattr(scene.access, "auto_review_title_keyword", "") == "周报"

    def test_default_empty_when_absent(self):
        # 未配该字段的场景默认为空 (feature off, 仍需 @)
        assert sl.SceneAccess().auto_review_title_keyword == ""
