#!/usr/bin/env python3
"""
feishu_docio.py — 飞书云文档直读 (study-weekly M1 · 全系统 Phase B 能力首落地)

把消息文本里的飞书云文档链接解析出来并拉取纯文本正文, 供评审流水线消费,
成员**不必导出 Word** (PRD prd-study-weekly-reflect §4.1 F1)。

能力:
  - extract_doc_refs(text): 识别 docx / wiki 链接 (feishu.cn / larksuite.com), 去重保序。
  - fetch_doc_text(kind, token): wiki 先解析节点 → obj_token; docx 拉 raw_content + 标题。
  - 错误**分型** (DocioError.kind): permission / not_found / unsupported / api —
    上层据此回兜底卡 (授权两法 + "或直接发文件"), 绝不静默失败。

权限前提 (app 后台开通): docx:document:readonly + wiki:wiki:readonly;
且机器人对目标文档可读 — 两条授权路径 (写进使用说明):
  (a) 文档设「组织内获得链接可阅读」(推荐, 一次设置);
  (b) 把机器人添加为文档协作者。

凭据: 复用 feishu_client 进程级 creds (每 bot 进程 set_bot_creds 后本模块即用该 bot 身份)。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

import feishu_client

BASE = "https://open.feishu.cn/open-apis"

# docx 直链 / wiki 知识库链 (子域任意, 兼容 larksuite 海外域); token = 路径段
DOC_URL_RE = re.compile(
    r"https?://[\w.-]+\.(?:feishu\.cn|larksuite\.com)/(docx|wiki|docs)/([A-Za-z0-9]+)")

# 飞书权限类错误码 (raw_content/get_node 无权限时常见); HTTP 403 同判
_PERM_CODES = {99991672, 99991679, 1770032, 1770002, 230005, 131006}
_NOTFOUND_CODES = {1770001, 230001, 131005}


class DocioError(Exception):
    """云文档读取失败 (分型)。kind: permission | not_found | unsupported | api"""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


def extract_doc_refs(text: str) -> list[tuple[str, str]]:
    """→ [(kind, token)] 去重保序。kind: docx | wiki (docs 旧版链接按 wiki 解析节点兜底)。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in DOC_URL_RE.finditer(text or ""):
        kind, token = m.group(1), m.group(2)
        if token in seen:
            continue
        seen.add(token)
        out.append(("docx" if kind == "docx" else "wiki", token))
    return out


def _get_json(url: str, params: dict | None = None, *,
              access_token: str | None = None) -> dict[str, Any]:
    """GET → data dict; 错误分型抛 DocioError。access_token: 传 user_access_token 读私有文档
    (群历史回填 pull_chat_backfill 用; 缺省用 bot tenant token, 只读 org-可读/协作者文档)。"""
    try:
        resp = httpx.get(url, params=params or {},
                         headers=feishu_client._auth_headers(access_token), timeout=30.0)
    except Exception as e:  # noqa: BLE001
        raise DocioError("api", f"{type(e).__name__}: {e}") from e
    if resp.status_code == 403:
        raise DocioError("permission", "HTTP 403")
    if resp.status_code == 404:
        raise DocioError("not_found", "HTTP 404")
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        raise DocioError("api", f"非 JSON 响应 (HTTP {resp.status_code})") from e
    code = body.get("code", 0)
    if code:
        if code in _PERM_CODES:
            raise DocioError("permission", f"code={code}")
        if code in _NOTFOUND_CODES:
            raise DocioError("not_found", f"code={code}")
        raise DocioError("api", f"code={code}: {body.get('msg', '')}")
    return body.get("data") or {}


def _resolve_wiki(token: str, *, access_token: str | None = None) -> str:
    """wiki 节点 → 底层 docx obj_token; 非 docx 类型 → unsupported。"""
    data = _get_json(f"{BASE}/wiki/v2/spaces/get_node", {"token": token}, access_token=access_token)
    node = data.get("node") or {}
    obj_type = node.get("obj_type", "")
    if obj_type != "docx":
        raise DocioError("unsupported", f"wiki 节点类型 {obj_type or '未知'} (仅支持新版文档 docx)")
    return str(node.get("obj_token") or "")


def fetch_doc_text(kind: str, token: str, *, access_token: str | None = None) -> tuple[str, str]:
    """→ (标题, 纯文本正文)。失败抛 DocioError (分型)。
    access_token: user_access_token — 读成员私有文档时传 (群历史回填); 缺省用 bot tenant token。"""
    doc_id = _resolve_wiki(token, access_token=access_token) if kind == "wiki" else token
    if not doc_id:
        raise DocioError("not_found", "wiki 节点无 obj_token")
    data = _get_json(f"{BASE}/docx/v1/documents/{doc_id}/raw_content", access_token=access_token)
    text = str(data.get("content") or "")
    if not text.strip():
        raise DocioError("unsupported", "文档正文为空 (或为不支持的旧版文档)")
    # 标题 best-effort: 基本信息接口失败不影响正文
    title = ""
    try:
        info = _get_json(f"{BASE}/docx/v1/documents/{doc_id}", access_token=access_token)
        title = str((info.get("document") or {}).get("title") or "")
    except DocioError:
        pass
    if not title:
        title = text.strip().splitlines()[0][:50] or "未命名文档"
    return title, text


def permission_help_lines(kind: str = "permission") -> list[str]:
    """兜底卡文案 (按错误分型)。"""
    if kind == "permission":
        return [
            "机器人读不到这篇云文档 (无权限)。两种解决方法任选:",
            "① 文档右上角「分享」→ 组织内获得链接的人「可阅读」(推荐, 一次设置);",
            "② 「分享」→ 添加本机器人为协作者;",
            "或者: 导出为 PDF/Word 直接发文件给我 (老办法, 一定可用)。",
        ]
    if kind == "not_found":
        return ["链接指向的文档不存在或已删除, 请核对链接后重发; 或直接发文件。"]
    if kind == "unsupported":
        return ["暂只支持新版飞书文档 (docx); 旧版文档/表格/多维表请导出为 PDF/Word 发文件。"]
    return ["云文档读取暂时失败, 请稍后重试; 或导出为 PDF/Word 直接发文件。"]
