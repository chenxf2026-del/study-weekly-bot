"""标题闸 · 只评标题含关键词的文档 (2026-07-26 主理拍板)。

需求原文: 「只审核飞书云文档或者直接发的文档 (word/pdf 等) 标题带有『周报』2 个字的,
如果标题没有『周报』的, 都不进入评审。」

此前只在「免 @ 群分享」这一条路上过滤 —— @ 机器人或单聊发任意文档都会被评, 与意图不符。
本次改为**全路径硬门槛**: 云文档 / 直接发的文件 / 引用的文件, 三条路一视同仁。

**群里恒静默, 单聊才提示** (2026-07-27 主理两次实测反馈定的口径):
- 群里: 成员常分享各类文档 (合集、图书版 PDF …), 每份弹卡极吵; 且 group_open 场景发文件
  不需要 @, 所谓"显式提交"实际覆盖群内全部文件 → **恒静默, 不受开关控制**。
- 单聊: 一对一低流量, 对方明确冲机器人来的, 没反应会让人以为机器人坏了 → **默认提示**。
  `scene.access.title_gate_notify: false` 可把单聊也静音。
"""

from __future__ import annotations

import pytest

import feishu_events as fe

KW_CFG = {"auto_review_title_keyword": "周报"}
NO_KW_CFG = {"bot_name": "OP2 评审助手"}          # 其它场景: 未配关键词


class TestTitleGateHelper:
    def test_blocks_when_keyword_absent(self):
        assert fe._title_gate_blocked(KW_CFG, "张三-W30总结.docx") == "周报"

    def test_passes_when_keyword_present(self):
        assert fe._title_gate_blocked(KW_CFG, "张三-W30周报.docx") is None
        assert fe._title_gate_blocked(KW_CFG, "周报") is None

    def test_unconfigured_scene_always_passes(self):
        """未配关键词的场景 (op2-* / meeting-review) 行为必须完全不变。"""
        for title in ("任意文档.pdf", "", "周报.docx"):
            assert fe._title_gate_blocked(NO_KW_CFG, title) is None
            assert fe._title_gate_blocked(None, title) is None

    def test_empty_title_is_blocked_when_configured(self):
        assert fe._title_gate_blocked(KW_CFG, "") == "周报"
        assert fe._title_gate_blocked(KW_CFG, None) == "周报"


class TestFilePathGate:
    """直接发的 word/pdf 与「引用文件 + @」都走 _dispatch_review_file。"""

    @staticmethod
    def _msg(file_name: str) -> dict:
        import json
        return {"message_id": "om_x",
                "content": json.dumps({"file_key": "file_v1", "file_name": file_name})}

    @pytest.fixture
    def spy(self, monkeypatch, tmp_path):
        sent: list = []
        monkeypatch.setattr(fe, "_safe_send", lambda rid, card, rtype=None: sent.append(card))
        monkeypatch.setattr(fe.review_access, "check_access",
                            lambda *a, **k: type("A", (), {"allowed": True, "reason": ""})())
        enqueued: list = []
        monkeypatch.setattr(fe.review_queue, "enqueue",
                            lambda payload: (enqueued.append(payload), "job_1")[1])
        monkeypatch.setattr(fe.review_queue, "position", lambda j: 1)
        monkeypatch.setattr(fe.feishu_client, "download_message_file",
                            lambda *a, **k: None)
        return sent, enqueued, tmp_path

    def test_file_without_keyword_is_silently_skipped(self, spy):
        sent, enqueued, tmp = spy
        out = fe._dispatch_review_file(
            self._msg("张三-W30工作总结.docx"), "ou_1", "oc_1", "chat_id",
            scene_slug="study-weekly-reflect", is_group=True, chat_id="oc_1",
            cfg=KW_CFG, inbox=tmp)
        assert out["status"] == "skipped_no_title_keyword"
        assert not enqueued, "不该入队 (不占配额)"
        assert not sent, "群里恒静默 — 文件流量大, 每份弹卡极吵 (2026-07-27 实测拍板)"

    def test_file_with_keyword_is_accepted(self, spy):
        sent, enqueued, tmp = spy
        out = fe._dispatch_review_file(
            self._msg("张三-W30周报.docx"), "ou_1", "oc_1", "chat_id",
            scene_slug="study-weekly-reflect", is_group=True, chat_id="oc_1",
            cfg=KW_CFG, inbox=tmp)
        assert out["status"] == "accepted"
        assert len(enqueued) == 1

    def test_p2p_gets_the_card_by_default(self, spy):
        """单聊默认提示 —— 一对一没反应会让人以为机器人坏了。"""
        sent, enqueued, tmp = spy
        out = fe._dispatch_review_file(
            self._msg("张三-W30工作总结.docx"), "ou_1", "ou_1", "open_id",
            scene_slug="study-weekly-reflect", is_group=False, chat_id="",
            cfg=KW_CFG, inbox=tmp)
        assert out["status"] == "skipped_no_title_keyword"
        assert not enqueued
        assert sent and "周报" in str(sent[-1]), "单聊被拦下必须说明原因"

    def test_group_stays_silent_even_if_flag_on(self, spy):
        """群里恒静默是**硬约束** —— 开关打开也不许在群里弹卡 (这是本次改动的核心教训)。"""
        sent, enqueued, tmp = spy
        fe._dispatch_review_file(
            self._msg("张三-W30工作总结.docx"), "ou_1", "oc_1", "chat_id",
            scene_slug="study-weekly-reflect", is_group=True, chat_id="oc_1",
            cfg={**KW_CFG, "title_gate_notify": True}, inbox=tmp)
        assert not sent, "群里开了 notify 也必须静默"

    def test_flag_can_silence_p2p_too(self, spy):
        """开关只管单聊 (群里本就恒静默)。"""
        sent, enqueued, tmp = spy
        fe._dispatch_review_file(
            self._msg("张三-W30工作总结.docx"), "ou_1", "ou_1", "open_id",
            scene_slug="study-weekly-reflect", is_group=False, chat_id="",
            cfg={**KW_CFG, "title_gate_notify": False}, inbox=tmp)
        assert not sent

    def test_other_scene_file_unaffected(self, spy):
        """未配关键词的场景照旧收任意文件 —— 本次改动不得波及 op2-* 等。"""
        sent, enqueued, tmp = spy
        out = fe._dispatch_review_file(
            self._msg("某公司战略规划.pdf"), "ou_1", "oc_1", "chat_id",
            scene_slug="op2-company", is_group=True, chat_id="oc_1",
            cfg=NO_KW_CFG, inbox=tmp)
        assert out["status"] == "accepted"
        assert len(enqueued) == 1


class TestCloudDocGate:
    """云文档: 标题闸全路径生效, 默认一律静默 (notify 开关另测)。"""

    @pytest.fixture
    def wired(self, monkeypatch, tmp_path):
        import json
        sent: list = []
        monkeypatch.setattr(fe, "_safe_send", lambda rid, card, rtype=None: sent.append(card))
        monkeypatch.setattr(fe.review_access, "check_access",
                            lambda *a, **k: type("A", (), {"allowed": True, "reason": ""})())
        enqueued: list = []
        monkeypatch.setattr(fe.review_queue, "enqueue",
                            lambda payload: (enqueued.append(payload), "job_1")[1])
        monkeypatch.setattr(fe.review_queue, "position", lambda j: 1)

        import feishu_docio
        monkeypatch.setattr(feishu_docio, "extract_doc_refs",
                            lambda text: [("docx", "tok_1")] if "http" in text else [])

        def _msg(url: str = "https://example.feishu.cn/docx/tok_1") -> dict:
            return {"message_id": "om_x",
                    "content": json.dumps({"text": url})}
        return sent, enqueued, tmp_path, monkeypatch, _msg, feishu_docio

    def _run(self, wired, title: str, *, silent: bool):
        sent, enqueued, tmp, monkeypatch, _msg, docio = wired
        monkeypatch.setattr(docio, "fetch_doc_text", lambda kind, tok: (title, "正文"))
        out = fe._maybe_review_cloud_doc(
            _msg(), "ou_1", "oc_1", "chat_id",
            scene_slug="study-weekly-reflect", is_group=True, chat_id="oc_1",
            cfg={**KW_CFG, "allow_cloud_doc": True}, inbox=tmp,
            require_title_keyword="周报", silent_skip=silent)
        return out, sent, enqueued

    def test_auto_share_without_keyword_is_silent(self, wired):
        out, sent, enqueued = self._run(wired, "本周工作小结", silent=True)
        assert out["status"] == "skipped_auto_no_keyword"
        assert not enqueued
        assert not sent, "免 @ 群分享不该回卡 (刷屏)"

    def test_explicit_without_keyword_also_silent_by_default(self, wired):
        out, sent, enqueued = self._run(wired, "本周工作小结", silent=False)
        assert out["status"] == "skipped_no_title_keyword"
        assert not enqueued
        assert not sent, "默认静默"

    def test_with_keyword_enqueues(self, wired):
        out, sent, enqueued = self._run(wired, "张三 W30 周报", silent=False)
        assert out["status"] == "accepted"
        assert len(enqueued) == 1


class TestGuideCardStatesTheRule:
    def test_guide_mentions_all_document_kinds(self):
        card = fe.guide_card("study-weekly-reflect",
                             {**KW_CFG, "allow_cloud_doc": True})
        blob = str(card)
        assert "周报" in blob
        assert "只评审标题含" in blob, "引导语要把规则说在前面"
        assert "Word" in blob or "PDF" in blob, "要点明直接发的文件同此规则"
