#!/usr/bin/env python3
"""Small local Markdown canvas service."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlsplit

import fcntl
from markdown_it import MarkdownIt
from markdown_it.token import Token


HOST = "127.0.0.1"
PORT = 39173
MAX_REQUEST_BYTES = 8 * 1024 * 1024
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSET_PATH = SKILL_DIR / "assets" / "index.html"
EXAMPLE_FILES = {
    "tesla": SKILL_DIR / "examples" / "tesla-2025-annual-report.md",
}
DOCUMENT_NAME = "document.md"
REVIEW_NAME = "review.json"
DOCUMENTS_INDEX_NAME = "documents.json"
DOCUMENTS_DIR_NAME = "documents"
MAIN_DOCUMENT_ID = "main"
LOCK_NAME = ".lock"
PDF_FONT_NAME = "WritingCanvasCJK"
PDF_FONT_BOLD_NAME = "WritingCanvasCJKBold"
DOCUMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TASK_MARKER_PATTERN = re.compile(r"^\[([ xX])\](?:\s+|$)")


class CanvasError(Exception):
    """Expected user/data error that should not produce a traceback."""


def document_path(data_dir: Path, document_id: str) -> Path:
    validate_document_id(document_id)
    return data_dir / DOCUMENTS_DIR_NAME / document_id / DOCUMENT_NAME


def review_path(data_dir: Path, document_id: str) -> Path:
    validate_document_id(document_id)
    return data_dir / DOCUMENTS_DIR_NAME / document_id / REVIEW_NAME


def validate_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise CanvasError("文档 ID 格式无效。")
    return document_id


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / LOCK_NAME
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    index_file = data_dir / DOCUMENTS_INDEX_NAME
    if index_file.exists():
        return
    content = ""
    atomic_write(document_path(data_dir, MAIN_DOCUMENT_ID), content)
    atomic_write(
        index_file,
        json.dumps(
            {
                "active_id": MAIN_DOCUMENT_ID,
                "documents": [{"id": MAIN_DOCUMENT_ID, "title": document_title(content)}],
                "archived_documents": [],
            },
            ensure_ascii=False,
        ),
    )


def document_records(payload: Any, key: str, required: bool = False) -> list[dict[str, str]]:
    raw_records = payload.get(key) if isinstance(payload, dict) else None
    if raw_records is None and not required:
        raw_records = []
    if not isinstance(raw_records, list):
        raise CanvasError("文档索引格式无效。")
    records: list[dict[str, str]] = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise CanvasError("文档索引格式无效。")
        document_id = validate_document_id(record.get("id"))
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CanvasError("文档标题格式无效。")
        records.append({"id": document_id, "title": title})
    return records


def load_documents_index_unlocked(data_dir: Path) -> dict[str, Any]:
    ensure_data_dir(data_dir)
    try:
        payload = json.loads((data_dir / DOCUMENTS_INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanvasError("文档索引损坏，请从备份恢复。") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
        raise CanvasError("文档索引格式无效。")
    records = document_records(payload, "documents", required=True)
    archived_records = document_records(payload, "archived_documents")
    if {record["id"] for record in records} & {record["id"] for record in archived_records}:
        raise CanvasError("文档索引中存在重复文档。")
    if not records:
        return {"active_id": None, "documents": [], "archived_documents": archived_records}
    active_id = payload.get("active_id")
    if active_id not in {record["id"] for record in records}:
        active_id = records[0]["id"]
    return {
        "active_id": active_id,
        "documents": records,
        "archived_documents": archived_records,
    }


def write_documents_index_unlocked(data_dir: Path, index: dict[str, Any]) -> None:
    atomic_write(
        data_dir / DOCUMENTS_INDEX_NAME,
        json.dumps(index, ensure_ascii=False),
    )


def document_record_unlocked(data_dir: Path, document_id: str | None = None) -> dict[str, str]:
    index = load_documents_index_unlocked(data_dir)
    if not index["documents"]:
        raise CanvasError("没有可用文档。")
    target_id = document_id or index["active_id"]
    validate_document_id(target_id)
    for record in index["documents"]:
        if record["id"] == target_id:
            return record
    raise CanvasError("文档不存在，请刷新标签列表。")


def resolve_document_id_unlocked(data_dir: Path, document_id: str | None = None) -> str:
    return document_record_unlocked(data_dir, document_id)["id"]


def normalize_document_title(title: str) -> str:
    if not isinstance(title, str):
        raise CanvasError("文档标题必须是字符串。")
    normalized = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "-", title).strip(" .")[:80]
    if not normalized:
        raise CanvasError("文档标题不能为空。")
    return normalized


def load_unlocked(data_dir: Path, document_id: str | None = None) -> str:
    target_id = resolve_document_id_unlocked(data_dir, document_id)
    try:
        return document_path(data_dir, target_id).read_text(encoding="utf-8")
    except OSError as error:
        raise CanvasError(f"无法读取文档：{error}") from error


def load_example_unlocked(example_id: str) -> str:
    path = EXAMPLE_FILES.get(example_id)
    if path is None:
        raise CanvasError("未知范例数据。")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CanvasError(f"无法读取范例数据：{error}") from error


def load_review_unlocked(data_dir: Path, document_id: str | None = None) -> dict[str, Any] | None:
    target_id = resolve_document_id_unlocked(data_dir, document_id)
    review_file = review_path(data_dir, target_id)
    if not review_file.exists():
        return None
    try:
        payload = json.loads(review_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_review_unlocked(data_dir: Path, document_id: str | None = None) -> None:
    target_id = resolve_document_id_unlocked(data_dir, document_id)
    try:
        review_path(data_dir, target_id).unlink()
    except FileNotFoundError:
        pass


def revision_for(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def markdown_parser() -> MarkdownIt:
    # Keep raw HTML disabled while enabling the standard pipe-table extension.
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(content: str) -> str:
    markdown = markdown_parser()
    tokens = markdown.parse(content)
    list_stack: list[Token] = []
    list_item_stack: list[Token] = []
    for token in tokens:
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append(token)
            continue
        if token.type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
            continue
        if token.type == "list_item_open":
            list_item_stack.append(token)
            continue
        if token.type == "list_item_close":
            if list_item_stack:
                list_item_stack.pop()
            continue
        if not list_stack or not list_item_stack or token.type != "inline":
            continue
        match = TASK_MARKER_PATTERN.match(token.content)
        children = token.children or []
        if not match or not children or children[0].type != "text":
            continue
        current_list = list_stack[-1]
        current_item = list_item_stack[-1]
        if current_list.type == "bullet_list_open":
            list_attrs = current_list.attrs or {}
            list_attrs["class"] = "task-list"
            current_list.attrs = list_attrs
        item_attrs = current_item.attrs or {}
        checked = match.group(1).lower() == "x"
        item_attrs["class"] = "task-item task-item-completed" if checked else "task-item"
        current_item.attrs = item_attrs
        first_child = children[0]
        first_child.content = first_child.content[match.end() :]
        if not first_child.content:
            children.pop(0)
        token.content = token.content[match.end() :]
        checkbox = Token("html_inline", "", 0)
        checked_attribute = ' checked=""' if checked else ""
        checkbox.content = (
            '<input type="checkbox" class="task-checkbox" '
            f'aria-label="核对清单项"{checked_attribute}>'
        )
        children.insert(0, checkbox)
    return markdown.renderer.render(tokens, markdown.options, {})


def markdown_blocks(content: str) -> list[dict[str, Any]]:
    markdown = markdown_parser()
    lines = content.splitlines(keepends=True)
    blocks: list[dict[str, Any]] = []
    for token in markdown.parse(content):
        if not token.map or token.level != 0:
            continue
        if token.nesting != 1 and token.type not in {"code_block", "fence", "hr"}:
            continue
        start_line, end_line = token.map
        block_content = "".join(lines[start_line:end_line])
        if not block_content.strip():
            continue
        blocks.append(
            {
                "start_line": start_line,
                "end_line": end_line,
                "content": block_content,
                "html": render_markdown(block_content),
            }
        )
    return blocks


def review_state(
    content: str, blocks: list[dict[str, Any]], review: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not review:
        return None
    base_revision = review.get("base_revision")
    result: dict[str, Any] = {
        "base_revision": base_revision,
        "stale": base_revision != revision_for(content),
    }
    if result["stale"]:
        return result
    source = review.get("source")
    replacement = review.get("replacement")
    if not isinstance(source, str) or not source or not isinstance(replacement, str):
        result["unsupported"] = True
        return result
    matches = [
        (index, block)
        for index, block in enumerate(blocks)
        if source in block["content"]
    ]
    if len(matches) != 1:
        result["unsupported"] = True
        return result
    index, block = matches[0]
    result.update(
        {
            "block_index": index,
            "old_html": block["html"],
            "new_html": render_markdown(block["content"].replace(source, replacement, 1)),
        }
    )
    return result


def state_unlocked(data_dir: Path, document_id: str | None = None) -> dict[str, Any]:
    record = document_record_unlocked(data_dir, document_id)
    target_id = record["id"]
    content = load_unlocked(data_dir, target_id)
    blocks = markdown_blocks(content)
    return {
        "document_id": target_id,
        "title": document_title(content),
        "content": content,
        "revision": revision_for(content),
        "html": render_markdown(content),
        "blocks": blocks,
        "review": review_state(content, blocks, load_review_unlocked(data_dir, target_id)),
    }


def documents_state_unlocked(data_dir: Path) -> dict[str, Any]:
    index = load_documents_index_unlocked(data_dir)
    documents = []
    for record in index["documents"]:
        content = load_unlocked(data_dir, record["id"])
        documents.append(
            {
                "id": record["id"],
                "title": document_title(content),
                "has_review": load_review_unlocked(data_dir, record["id"]) is not None,
                "revision": revision_for(content),
            }
        )
    return {
        "active_id": index["active_id"],
        "documents": documents,
        "archived_documents": list(reversed(index["archived_documents"])),
    }


def document_title(content: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*#*\s*$", line)
        if match:
            title = re.sub(r"[*_`~]", "", match.group(1)).strip()
            title = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "-", title)
            title = re.sub(r"\s+", " ", title).strip(" .")[:80]
            return title or "未命名文档"
    return "未命名文档"


def build_pdf(content: str) -> bytes:
    """Render Markdown into a small self-contained PDF for local download."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            HRFlowable,
            ListFlowable,
            ListItem,
            Paragraph,
            Preformatted,
            Spacer,
            SimpleDocTemplate,
            Table,
            TableStyle,
        )
    except ImportError as error:
        raise CanvasError("PDF 导出依赖不可用，请安装 reportlab。") from error

    user_font_dir = Path.home() / "Library" / "Fonts"
    regular_font_candidates = (
        str(user_font_dir / "SourceHanSansCN-Regular.ttf"),
        str(user_font_dir / "NotoSansSC.ttf"),
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        r"C:\\Windows\\Fonts\\msyh.ttc",
    )
    bold_font_candidates = (
        str(user_font_dir / "SourceHanSansCN-Bold(1).ttf"),
        str(user_font_dir / "SourceHanSansCN-Bold.ttf"),
        str(user_font_dir / "NotoSansSC-Bold.ttf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        r"C:\\Windows\\Fonts\\msyhbd.ttc",
    )

    def register_font(name: str, candidates: tuple[str, ...]) -> None:
        if name in pdfmetrics.getRegisteredFontNames():
            return
        for font_path in candidates:
            if not Path(font_path).exists():
                continue
            try:
                pdfmetrics.registerFont(TTFont(name, font_path))
                return
            except Exception:
                continue

    register_font(PDF_FONT_NAME, regular_font_candidates)
    if PDF_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        raise CanvasError("未找到可用的中文字体，无法生成 PDF。")
    if PDF_FONT_BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
        register_font(PDF_FONT_BOLD_NAME, bold_font_candidates)
    if PDF_FONT_BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
        raise CanvasError("未找到可用的中文粗体字体，无法生成 PDF。")
    pdfmetrics.registerFontFamily(
        PDF_FONT_NAME,
        normal=PDF_FONT_NAME,
        bold=PDF_FONT_BOLD_NAME,
        italic=PDF_FONT_NAME,
        boldItalic=PDF_FONT_BOLD_NAME,
    )

    def attr(token: Any, name: str) -> str:
        attrs = token.attrs or {}
        if isinstance(attrs, dict):
            return str(attrs.get(name, ""))
        return str(dict(attrs).get(name, ""))

    def inline_markup(token: Any) -> str:
        parts: list[str] = []
        for child in token.children or []:
            if child.type == "text":
                parts.append(html.escape(child.content).replace("\n", "<br/>"))
            elif child.type == "code_inline":
                code_text = html.escape(child.content).replace("\n", "<br/>")
                parts.append(f'<font backColor="#eef2ff">{code_text}</font>')
            elif child.type == "softbreak" or child.type == "hardbreak":
                parts.append("<br/>")
            elif child.type == "strong_open":
                parts.append("<b>")
            elif child.type == "strong_close":
                parts.append("</b>")
            elif child.type == "em_open":
                parts.append("<i>")
            elif child.type == "em_close":
                parts.append("</i>")
            elif child.type == "s_open":
                parts.append("<strike>")
            elif child.type == "s_close":
                parts.append("</strike>")
            elif child.type == "link_open":
                href = html.escape(attr(child, "href"), quote=True)
                parts.append(f'<link href="{href}" color="#2563eb">')
            elif child.type == "link_close":
                parts.append("</link>")
            elif child.type == "image":
                parts.append(html.escape(attr(child, "alt")))
        return "".join(parts) or " "

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CanvasBody",
        parent=styles["BodyText"],
        fontName=PDF_FONT_NAME,
        fontSize=12,
        leading=21,
        textColor=colors.HexColor("#1f2937"),
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceAfter=12,
    )
    quote = ParagraphStyle(
        "CanvasQuote",
        parent=body,
        leftIndent=0,
        borderPadding=0,
        textColor=colors.HexColor("#475569"),
        spaceAfter=0,
    )
    headings = {
        1: ParagraphStyle("CanvasH1", parent=body, fontName=PDF_FONT_BOLD_NAME, fontSize=24, leading=31, spaceBefore=0, spaceAfter=13.5, textColor=colors.HexColor("#111827")),
        2: ParagraphStyle("CanvasH2", parent=body, fontName=PDF_FONT_BOLD_NAME, fontSize=18, leading=23, spaceBefore=21, spaceAfter=9, textColor=colors.HexColor("#111827")),
        3: ParagraphStyle("CanvasH3", parent=body, fontName=PDF_FONT_BOLD_NAME, fontSize=15, leading=19.5, spaceBefore=15, spaceAfter=6, textColor=colors.HexColor("#1f2937")),
    }
    list_item = ParagraphStyle("CanvasListItem", parent=body, spaceAfter=0)
    code = ParagraphStyle(
        "CanvasCode",
        parent=body,
        fontName=PDF_FONT_NAME,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#e5e7eb"),
        borderPadding=0,
    )

    def story_from_tokens(tokens: list[Any]) -> list[Any]:
        story: list[Any] = []
        content_width = A4[0] - (16 * mm)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == "heading_open" and index + 1 < len(tokens):
                inline = tokens[index + 1]
                level = min(int(token.tag[1:]), 3)
                story.append(Paragraph(inline_markup(inline), headings[level]))
                index += 3
                continue
            if token.type == "paragraph_open" and index + 1 < len(tokens):
                story.append(Paragraph(inline_markup(tokens[index + 1]), body))
                index += 3
                continue
            if token.type == "table_open":
                rows: list[list[Any]] = []
                current_row: list[Any] | None = None
                index += 1
                while index < len(tokens) and tokens[index].type != "table_close":
                    current = tokens[index]
                    if current.type == "tr_open":
                        current_row = []
                    elif current.type == "inline" and current_row is not None:
                        current_row.append(Paragraph(inline_markup(current), body))
                    elif current.type == "tr_close" and current_row is not None:
                        rows.append(current_row)
                        current_row = None
                    index += 1
                if rows:
                    column_count = max(len(row) for row in rows)
                    normalized_rows = [
                        row + [Paragraph(" ", body)] * (column_count - len(row))
                        for row in rows
                    ]
                    table = Table(
                        normalized_rows,
                        colWidths=[content_width / column_count] * column_count,
                        repeatRows=1,
                    )
                    table.setStyle(
                        TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
                                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dfe4ee")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                                ("TOPPADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ]
                        )
                    )
                    table.spaceBefore = 12
                    table.spaceAfter = 12
                    story.append(table)
                index += 1
                continue
            if token.type in {"bullet_list_open", "ordered_list_open"}:
                close_type = "bullet_list_close" if token.type == "bullet_list_open" else "ordered_list_close"
                items: list[Any] = []
                index += 1
                while index < len(tokens) and tokens[index].type != close_type:
                    if tokens[index].type == "list_item_open":
                        index += 1
                        item_flowables: list[Any] = []
                        while index < len(tokens) and tokens[index].type != "list_item_close":
                            if tokens[index].type == "inline":
                                item_flowables.append(Paragraph(inline_markup(tokens[index]), list_item))
                            index += 1
                        items.append(ListItem(item_flowables, leftIndent=8))
                    index += 1
                story.append(ListFlowable(items, bulletType="1" if token.type == "ordered_list_open" else "bullet", leftIndent=30, bulletDedent=15, bulletFontName=PDF_FONT_NAME, bulletFontSize=12, spaceBefore=12, spaceAfter=12))
                index += 1
                continue
            if token.type == "blockquote_open":
                index += 1
                quote_flowables: list[Any] = []
                while index < len(tokens) and tokens[index].type != "blockquote_close":
                    if tokens[index].type == "inline":
                        quote_flowables.append(Paragraph(inline_markup(tokens[index]), quote))
                    index += 1
                if quote_flowables:
                    quote_table = Table(
                        [[Spacer(2.25, 1), quote_flowables]],
                        colWidths=[2.25, content_width - 2.25],
                    )
                    quote_table.setStyle(
                        TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#93c5fd")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                ("LEFTPADDING", (1, 0), (1, 0), 11.25),
                            ]
                        )
                    )
                    quote_table.spaceBefore = 12
                    quote_table.spaceAfter = 12
                    story.append(quote_table)
                index += 1
                continue
            if token.type == "fence" or token.type == "code_block":
                code_table = Table(
                    [[Preformatted(token.content.rstrip("\n"), code)]],
                    colWidths=[content_width],
                )
                code_table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#111827")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10.5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 10.5),
                            ("TOPPADDING", (0, 0), (-1, -1), 10.5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 10.5),
                        ]
                    )
                )
                code_table.spaceBefore = 12
                code_table.spaceAfter = 16
                story.append(code_table)
            elif token.type == "hr":
                story.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#cbd5e1"), spaceBefore=5, spaceAfter=10))
            elif token.type == "inline":
                story.append(Paragraph(inline_markup(token), body))
            index += 1
        return story

    from io import BytesIO

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=8 * mm,
        leftMargin=8 * mm,
        topMargin=7 * mm,
        bottomMargin=12 * mm,
        title="Writing Canvas",
        author="Writing Canvas",
    )
    story = story_from_tokens(markdown_parser().parse(content))
    if not story:
        story = [Paragraph("（空白文档）", body)]

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(PDF_FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def list_documents(data_dir: Path) -> dict[str, Any]:
    with data_lock(data_dir):
        return documents_state_unlocked(data_dir)


def create_document(
    data_dir: Path, content: str = "", title: str | None = None
) -> dict[str, Any]:
    with data_lock(data_dir):
        index = load_documents_index_unlocked(data_dir)
        existing_ids = {
            record["id"]
            for record in index["documents"] + index["archived_documents"]
        }
        document_id = "doc-" + uuid.uuid4().hex[:10]
        while document_id in existing_ids:
            document_id = "doc-" + uuid.uuid4().hex[:10]
        document_title_value = normalize_document_title(title) if title else (
            document_title(content) if content.strip() else "未命名文档"
        )
        atomic_write(document_path(data_dir, document_id), content)
        index["documents"].insert(0, {"id": document_id, "title": document_title_value})
        index["active_id"] = document_id
        write_documents_index_unlocked(data_dir, index)
        return state_unlocked(data_dir, document_id)


def set_active_document(data_dir: Path, document_id: str) -> dict[str, Any]:
    with data_lock(data_dir):
        index = load_documents_index_unlocked(data_dir)
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        record = next(record for record in index["documents"] if record["id"] == target_id)
        index["documents"].remove(record)
        index["documents"].insert(0, record)
        index["active_id"] = target_id
        write_documents_index_unlocked(data_dir, index)
        return documents_state_unlocked(data_dir)


def delete_document(data_dir: Path, document_id: str) -> dict[str, Any]:
    with data_lock(data_dir):
        index = load_documents_index_unlocked(data_dir)
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        record = next(record for record in index["documents"] if record["id"] == target_id)
        content = load_unlocked(data_dir, target_id)
        record["title"] = document_title(content)
        index["documents"].remove(record)
        index["archived_documents"].append(record)
        if index["active_id"] == target_id:
            index["active_id"] = index["documents"][0]["id"] if index["documents"] else None
        write_documents_index_unlocked(data_dir, index)
        return documents_state_unlocked(data_dir)


def restore_document(data_dir: Path, document_id: str) -> dict[str, Any]:
    with data_lock(data_dir):
        index = load_documents_index_unlocked(data_dir)
        target_id = validate_document_id(document_id)
        record = next(
            (record for record in index["archived_documents"] if record["id"] == target_id),
            None,
        )
        if record is None:
            raise CanvasError("归档文档不存在，请刷新列表。")
        if not document_path(data_dir, target_id).is_file():
            raise CanvasError("归档文档文件不存在，请从备份恢复。")
        index["archived_documents"].remove(record)
        index["documents"].insert(0, record)
        if index["active_id"] is None:
            index["active_id"] = target_id
        write_documents_index_unlocked(data_dir, index)
        return documents_state_unlocked(data_dir)


def permanently_delete_document(data_dir: Path, document_id: str) -> dict[str, Any]:
    with data_lock(data_dir):
        index = load_documents_index_unlocked(data_dir)
        target_id = validate_document_id(document_id)
        record = next(
            (record for record in index["archived_documents"] if record["id"] == target_id),
            None,
        )
        if record is None:
            raise CanvasError("只能永久删除归档文档。")
        document_dir = document_path(data_dir, target_id).parent
        if not document_dir.is_dir():
            raise CanvasError("归档文档文件不存在，请从备份恢复。")
        shutil.rmtree(document_dir)
        index["archived_documents"].remove(record)
        write_documents_index_unlocked(data_dir, index)
        return documents_state_unlocked(data_dir)


def read_state(data_dir: Path, document_id: str | None = None) -> dict[str, Any]:
    with data_lock(data_dir):
        return state_unlocked(data_dir, document_id)


def check_revision(current: str, expected: str | None) -> None:
    if expected is not None and expected != current:
        raise CanvasError("文档已被其他操作更新，请重新读取后再试。")


def write_document(
    data_dir: Path,
    content: str,
    expected_revision: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    with data_lock(data_dir):
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        current_content = load_unlocked(data_dir, target_id)
        check_revision(revision_for(current_content), expected_revision)
        atomic_write(document_path(data_dir, target_id), content)
        clear_review_unlocked(data_dir, target_id)
        return state_unlocked(data_dir, target_id)


def write_document_block(
    data_dir: Path,
    start_line: int,
    end_line: int,
    block_content: str,
    expected_revision: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    with data_lock(data_dir):
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        current_content = load_unlocked(data_dir, target_id)
        check_revision(revision_for(current_content), expected_revision)
        lines = current_content.splitlines(keepends=True)
        if start_line < 0 or start_line >= end_line or end_line > len(lines):
            raise CanvasError("编辑块定位已失效，请重新加载后再试。")
        prefix = "".join(lines[:start_line])
        suffix = "".join(lines[end_line:])
        if block_content and suffix and not block_content.endswith(("\n", "\r")):
            block_content += "\n"
        atomic_write(document_path(data_dir, target_id), prefix + block_content + suffix)
        clear_review_unlocked(data_dir, target_id)
        return state_unlocked(data_dir, target_id)


def propose_document(
    data_dir: Path,
    source: str,
    replacement: str,
    expected_revision: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    with data_lock(data_dir):
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        current_content = load_unlocked(data_dir, target_id)
        current_revision = revision_for(current_content)
        check_revision(current_revision, expected_revision)
        if not source or current_content.count(source) != 1:
            raise CanvasError("待修改原文必须在当前文档中恰好出现一次。")
        if sum(source in block["content"] for block in markdown_blocks(current_content)) != 1:
            raise CanvasError("暂不支持跨多个 Markdown 块的修改，请只选择一个段落或列表。")
        existing = load_review_unlocked(data_dir, target_id)
        if existing and existing.get("base_revision") == current_revision:
            raise CanvasError("已有待确认修改，请先接受或撤销。")
        proposed_content = current_content.replace(source, replacement, 1)
        if proposed_content == current_content:
            raise CanvasError("修改建议没有产生内容变化。")
        atomic_write(
            review_path(data_dir, target_id),
            json.dumps(
                {
                    "base_revision": current_revision,
                    "source": source,
                    "replacement": replacement,
                    "content": proposed_content,
                },
                ensure_ascii=False,
            ),
        )
        return state_unlocked(data_dir, target_id)


def accept_review(
    data_dir: Path,
    expected_revision: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    with data_lock(data_dir):
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        current_content = load_unlocked(data_dir, target_id)
        current_revision = revision_for(current_content)
        review = load_review_unlocked(data_dir, target_id)
        if not review:
            raise CanvasError("没有待确认修改。")
        if review.get("base_revision") != current_revision:
            raise CanvasError("修改建议已过期，请重新生成。")
        check_revision(current_revision, expected_revision)
        source = review.get("source")
        replacement = review.get("replacement")
        proposed_content = review.get("content")
        if (
            not isinstance(source, str)
            or not isinstance(replacement, str)
            or not isinstance(proposed_content, str)
            or current_content.count(source) != 1
            or current_content.replace(source, replacement, 1) != proposed_content
        ):
            raise CanvasError("修改建议已失效，请重新生成。")
        atomic_write(document_path(data_dir, target_id), proposed_content)
        clear_review_unlocked(data_dir, target_id)
        return state_unlocked(data_dir, target_id)


def reject_review(data_dir: Path, document_id: str | None = None) -> dict[str, Any]:
    with data_lock(data_dir):
        target_id = resolve_document_id_unlocked(data_dir, document_id)
        clear_review_unlocked(data_dir, target_id)
        return state_unlocked(data_dir, target_id)


def read_json_request(handler: BaseHTTPRequestHandler) -> Any:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as error:
        raise CanvasError("请求长度无效") from error
    if length < 0 or length > MAX_REQUEST_BYTES:
        raise CanvasError("请求内容过大")
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanvasError("请求 JSON 无效") from error


def send_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_download(handler: BaseHTTPRequestHandler, body: bytes, content_type: str, filename: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    suffix = Path(filename).suffix
    ascii_filename = filename if filename.isascii() else f"writing-canvas{suffix}"
    handler.send_header(
        "Content-Disposition",
        f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class CanvasHandler(BaseHTTPRequestHandler):
    server_version = "WritingCanvas/1"

    @property
    def data_dir(self) -> Path:
        return self.server.data_dir  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            document_id = query.get("document_id", [None])[0]
            example_id = query.get("example", [None])[0]
            if path == "/":
                body = ASSET_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/health":
                listing = list_documents(self.data_dir)
                if listing["active_id"] is None:
                    send_json(self, {"ok": True, "document_id": None, "revision": None})
                    return
                state = read_state(self.data_dir, document_id)
                send_json(
                    self,
                    {
                        "ok": True,
                        "document_id": state["document_id"],
                        "revision": state["revision"],
                    },
                )
                return
            if path == "/api/documents":
                send_json(self, list_documents(self.data_dir))
                return
            if path == "/api/state":
                send_json(self, read_state(self.data_dir, document_id))
                return
            if path == "/api/default":
                if example_id is None:
                    raise CanvasError("范例参数不能为空。")
                with data_lock(self.data_dir):
                    content = load_example_unlocked(example_id)
                    send_json(self, {"content": content})
                return
            if path == "/api/export/markdown":
                state = read_state(self.data_dir, document_id)
                basename = document_title(state["content"])
                send_download(self, state["content"].encode("utf-8"), "text/markdown; charset=utf-8", f"{basename}.md")
                return
            if path == "/api/export/pdf":
                state = read_state(self.data_dir, document_id)
                basename = document_title(state["content"])
                send_download(self, build_pdf(state["content"]), "application/pdf", f"{basename}.pdf")
                return
            send_json(self, {"error": "Not found"}, 404)
        except (OSError, CanvasError) as error:
            send_json(self, {"error": str(error)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path not in {
                "/api/documents",
                "/api/documents/delete",
                "/api/documents/restore",
                "/api/documents/permanent-delete",
                "/api/documents/active",
                "/api/document",
                "/api/document/block",
                "/api/review/accept",
                "/api/review/reject",
            }:
                send_json(self, {"error": "Not found"}, 404)
                return
            request = read_json_request(self) if int(self.headers.get("Content-Length", "0")) else {}
            if not isinstance(request, dict):
                raise CanvasError("请求 JSON 格式无效")
            document_id = request.get("document_id")
            if document_id is not None and not isinstance(document_id, str):
                raise CanvasError("document_id 必须是字符串")
            if path == "/api/documents":
                content = request.get("content", "")
                title = request.get("title")
                if not isinstance(content, str) or (title is not None and not isinstance(title, str)):
                    raise CanvasError("新文档内容和标题格式无效")
                send_json(self, create_document(self.data_dir, content, title))
                return
            if path == "/api/documents/delete":
                if not isinstance(document_id, str):
                    raise CanvasError("document_id 必须是字符串")
                send_json(self, delete_document(self.data_dir, document_id))
                return
            if path == "/api/documents/restore":
                if not isinstance(document_id, str):
                    raise CanvasError("document_id 必须是字符串")
                send_json(self, restore_document(self.data_dir, document_id))
                return
            if path == "/api/documents/permanent-delete":
                if not isinstance(document_id, str):
                    raise CanvasError("document_id 必须是字符串")
                send_json(self, permanently_delete_document(self.data_dir, document_id))
                return
            if path == "/api/documents/active":
                if not isinstance(document_id, str):
                    raise CanvasError("document_id 必须是字符串")
                send_json(self, set_active_document(self.data_dir, document_id))
                return
            if path == "/api/review/reject":
                send_json(self, reject_review(self.data_dir, document_id))
                return
            if not isinstance(request.get("base_revision"), str):
                raise CanvasError("base_revision 必须是字符串")
            if path == "/api/review/accept":
                result = accept_review(self.data_dir, request["base_revision"], document_id)
            elif path == "/api/document":
                if not isinstance(request.get("content"), str):
                    raise CanvasError("文档内容必须是字符串")
                result = write_document(self.data_dir, request["content"], request["base_revision"], document_id)
            else:
                if (
                    not isinstance(request.get("content"), str)
                    or not isinstance(request.get("start_line"), int)
                    or not isinstance(request.get("end_line"), int)
                    or isinstance(request.get("start_line"), bool)
                    or isinstance(request.get("end_line"), bool)
                ):
                    raise CanvasError("编辑块内容、起止行和 base_revision 格式无效")
                result = write_document_block(
                    self.data_dir,
                    request["start_line"],
                    request["end_line"],
                    request["content"],
                    request["base_revision"],
                    document_id,
                )
            send_json(self, result)
        except CanvasError as error:
            status = 409 if any(word in str(error) for word in ("版本", "过期", "已有待确认")) else 400
            send_json(self, {"error": str(error)}, status)
        except (OSError, TypeError, ValueError) as error:
            send_json(self, {"error": str(error)}, 500)

    def log_message(self, format: str, *args: Any) -> None:
        return


class CanvasHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], data_dir: Path):
        super().__init__(address, CanvasHandler)
        self.data_dir = data_dir


def service_url(port: int = PORT) -> str:
    return f"http://{HOST}:{port}/"


def health_url(port: int = PORT) -> str:
    return f"http://{HOST}:{port}/api/health"


def is_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(health_url(port), timeout=0.4) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True
    except (OSError, json.JSONDecodeError):
        return False


def ensure_service(data_dir: Path, port: int) -> str:
    ensure_data_dir(data_dir)
    if not is_healthy(port):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "serve",
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
        ]
        try:
            subprocess.Popen(
                command,
                cwd=str(SKILL_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise CanvasError(f"无法启动本地画布服务：{error}") from error
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if is_healthy(port):
                return service_url(port)
            time.sleep(0.05)
        raise CanvasError(f"本地服务未能在 {service_url(port)} 启动；请检查端口是否被占用")
    return service_url(port)


def self_check() -> None:
    asset = ASSET_PATH.read_text(encoding="utf-8")
    assert EXAMPLE_FILES["tesla"].is_file()
    assert load_example_unlocked("tesla").startswith("# 2025年特斯拉年度报告\n")
    required_asset_markers = (
        "function selectCanvasContent(container)",
        "function selectionCovers(container)",
        "async function clearSelectedCanvas()",
        "const stateChanged = app.revision !== next.revision;",
        "function selectionRangeWithinPreview()",
        "const containers = Array.from(preview.querySelectorAll('.canvas-block'))",
        "range.compareBoundaryPoints(Range.END_TO_START, contents) < 0",
        "range.compareBoundaryPoints(Range.START_TO_END, contents) > 0",
        "if (!['UL', 'OL', 'TASK'].includes(value)) return false;",
        "const list = document.createElement(value === 'TASK' ? 'ul' : value.toLowerCase());",
        "list.className = 'task-list';",
        "task-item task-item-completed",
        "if (value === 'P' && nestedList.matches('UL, OL')) {",
        "const paragraphs = document.createDocumentFragment();",
        "await replaceDocument(content, false);",
        "function placeCaretInEmptyParagraph(container)",
        'id="selection-toolbar"',
        'id="selection-quote"',
        "function syncSelectionToolbar()",
        "function applySelectionFormat(value)",
        'data-format="TASK"',
        "function handleCheckboxChange(event)",
        "node.checked ? '[x] ' : '[ ] '",
        "classList.toggle('task-item-completed'",
        "document.addEventListener('selectionchange', syncSelectionToolbar);",
        "function resolveReview(action)",
        "stateRequestId: 0",
        "const stateRequestId = ++app.stateRequestId;",
        "if (stateRequestId !== app.stateRequestId) return;",
        "app.stateRequestId += 1;",
        'id="document-menu-toggle"',
        "function renderDocumentMenu()",
        'id="more-toggle"',
        'id="more-options"',
        "导出为PDF",
        "导出为MD",
        'id="import-markdown"',
        'id="import-markdown-file"',
        'accept=".md,text/markdown"',
        "导入MD",
        'id="import-tesla-example"',
        "导入演示数据",
        'id="create-document"',
        "moreToggleButton.disabled = blocked;",
        "function closeMoreMenu()",
        "document-empty-state-icon",
        "暂无文档，请从文档菜单中新建文档",
        "function clearDocumentState()",
        "copyButton.disabled = !hasDocument;",
        "exportPdfButton.disabled = blocked || !hasDocument;",
        "async function createDocument(content = '# 未命名文档\\n\\n')",
        "async function importMarkdownContent(event)",
        "await file.text()",
        "input.value = '';",
        "function hasEmptyBodySlot()",
        "container.className = 'canvas-block body-placeholder';",
        "body: JSON.stringify({ content })",
        "async function importExampleContent(exampleId)",
        "/api/default?example=",
        "await createDocument(content);",
        "async function archiveDocument(documentId, title)",
        "async function restoreDocument(documentId)",
        "async function permanentlyDeleteDocument(documentId, title)",
        "/api/documents/delete",
        "/api/documents/restore",
        "/api/documents/permanent-delete",
        "archivedDocuments",
        'id="archive-menu-toggle"',
        "function renderArchiveMenu()",
        "archive-menu-options",
        "document-menu-restore",
        "M3 10H2V4.00293",
        "M5.82843 6.99955L8.36396",
        "document-menu-delete",
        "function renderOutline()",
        "暂无可用大纲",
        "document_id: app.documentId",
    )
    missing = [marker for marker in required_asset_markers if marker not in asset]
    assert not missing, missing
    forbidden_asset_markers = (
        "function selectionRangeForContainer",
        "function persistSelectionChanges",
        "event.selectionFormat",
        "selectionBlockType",
        "selectionListType",
        "document-menu-empty",
        "if (app.documents.length > 1) {",
        "当前画布已有内容，确认覆盖替换为范例内容吗？",
        "await replaceDocument(content);",
        "windowFocused",
        "app.title",
        "function lineCount(content)",
        "truncateDocumentTitle",
        "defaultButton",
        "exampleOptions",
        "exportToggleButton",
        "exportOptions",
        "closeExampleMenu",
        "closeExportMenu",
        "document-menu-create",
        "reviewing",
    )
    present = [marker for marker in forbidden_asset_markers if marker in asset]
    assert not present, present
    with tempfile.TemporaryDirectory(prefix="writing-canvas-") as temporary:
        data_dir = Path(temporary)
        initial = "# 标题\n\n保留这句。\n"
        state = write_document(data_dir, initial)
        assert not (data_dir / DOCUMENT_NAME).exists()
        assert state["content"] == initial
        assert "<h1>标题</h1>" in state["html"]
        assert [block["content"] for block in state["blocks"]] == ["# 标题\n", "保留这句。\n"]
        assert document_title(initial) == "标题"
        assert document_title("") == "未命名文档"
        assert state["title"] == "标题"
        assert build_pdf(initial).startswith(b"%PDF-")
        checklist = "- [ ] 未完成\n- [x] 已完成\n"
        checklist_html = render_markdown(checklist)
        assert checklist_html.count('<input type="checkbox"') == 2
        assert '<ul class="task-list">' in checklist_html
        assert '<li class="task-item task-item-completed">' in checklist_html
        assert 'aria-label="核对清单项" checked=""' in checklist_html
        assert "[ ] 未完成" not in checklist_html
        assert "[x] 已完成" not in checklist_html
        assert build_pdf(checklist).startswith(b"%PDF-")
        table_markdown = "| 指标 | 数值 |\n|---|---:|\n| 收入 | 1 |\n"
        assert "<table>" in render_markdown(table_markdown)
        assert build_pdf(table_markdown).startswith(b"%PDF-")
        updated = write_document_block(data_dir, 2, 3, "修改这句。\n", state["revision"])
        assert "修改这句。" in updated["content"]
        assert "保留这句。" not in updated["content"]
        proposed = propose_document(data_dir, "修改这句。\n", "建议修改这句。\n", updated["revision"])
        assert proposed["content"] == updated["content"]
        assert proposed["review"]["block_index"] == 1
        assert "建议修改这句。" in proposed["review"]["new_html"]
        rejected = reject_review(data_dir)
        assert rejected["content"] == updated["content"]
        assert rejected["review"] is None
        propose_document(data_dir, "修改这句。\n", "确认修改这句。\n", updated["revision"])
        accepted = accept_review(data_dir, updated["revision"])
        assert "确认修改这句。" in accepted["content"]
        assert accepted["review"] is None
        before_failed_write = read_state(data_dir)
        try:
            write_document(data_dir, "外部覆盖。\n", before_failed_write["revision"] + "stale")
        except CanvasError:
            pass
        else:
            raise AssertionError("stale write was accepted")
        after_failed_write = read_state(data_dir)
        assert after_failed_write == before_failed_write
    with tempfile.TemporaryDirectory(prefix="writing-canvas-cross-block-") as temporary:
        data_dir = Path(temporary)
        cross_block = "# 标题\n\n第一段。\n\n第二段。\n"
        state = write_document(data_dir, cross_block)
        try:
            propose_document(
                data_dir,
                "# 标题\n\n第一段。\n",
                "# 新标题\n\n新第一段。\n",
                state["revision"],
            )
        except CanvasError as error:
            assert "跨多个 Markdown 块" in str(error)
        else:
            raise AssertionError("cross-block proposal was accepted")
    with tempfile.TemporaryDirectory(prefix="writing-canvas-documents-") as temporary:
        data_dir = Path(temporary)
        initial = "# 主文档\n\n保留主文档。\n"
        main = write_document(data_dir, initial)
        listing = list_documents(data_dir)
        assert listing["active_id"] == MAIN_DOCUMENT_ID
        assert [document["id"] for document in listing["documents"]] == [MAIN_DOCUMENT_ID]
        assert listing["archived_documents"] == []
        draft = create_document(data_dir, "# 未命名文档\n\n")
        assert draft["title"] == "未命名文档"
        body = write_document_block(data_dir, 1, 2, "正文。\n", draft["revision"], draft["document_id"])
        assert body["content"] == "# 未命名文档\n正文。\n"
        cleared = write_document_block(data_dir, 1, 2, "\n", body["revision"], draft["document_id"])
        assert cleared["content"] == "# 未命名文档\n\n"
        assert [document["id"] for document in list_documents(data_dir)["documents"]] == [draft["document_id"], MAIN_DOCUMENT_ID]
        archived = delete_document(data_dir, draft["document_id"])
        assert [document["id"] for document in archived["documents"]] == [MAIN_DOCUMENT_ID]
        assert [document["id"] for document in archived["archived_documents"]] == [draft["document_id"]]
        assert document_path(data_dir, draft["document_id"]).exists()
        restored = restore_document(data_dir, draft["document_id"])
        assert [document["id"] for document in restored["documents"]] == [draft["document_id"], MAIN_DOCUMENT_ID]
        assert restored["archived_documents"] == []
        delete_document(data_dir, draft["document_id"])
        purged = permanently_delete_document(data_dir, draft["document_id"])
        assert purged["archived_documents"] == []
        assert not document_path(data_dir, draft["document_id"]).exists()
        blank = create_document(data_dir)
        assert blank["title"] == "未命名文档"
        assert blank["content"] == ""
        assert [document["id"] for document in list_documents(data_dir)["documents"]] == [blank["document_id"], MAIN_DOCUMENT_ID]
        assert read_state(data_dir, MAIN_DOCUMENT_ID)["content"] == initial
        delete_document(data_dir, blank["document_id"])
        permanently_delete_document(data_dir, blank["document_id"])
        created = create_document(data_dir, "# 第二文档\n\n第二份内容。\n", "第二文档")
        assert created["document_id"] != MAIN_DOCUMENT_ID
        assert created["title"] == "第二文档"
        assert read_state(data_dir, MAIN_DOCUMENT_ID)["content"] == initial
        assert list_documents(data_dir)["active_id"] == created["document_id"]
        assert [document["id"] for document in list_documents(data_dir)["documents"]] == [created["document_id"], MAIN_DOCUMENT_ID]
        archive_first = create_document(data_dir, "# 归档一\n")
        archive_second = create_document(data_dir, "# 归档二\n")
        delete_document(data_dir, archive_second["document_id"])
        archived_order = delete_document(data_dir, archive_first["document_id"])
        assert [document["id"] for document in archived_order["archived_documents"]] == [archive_first["document_id"], archive_second["document_id"]]
        permanently_delete_document(data_dir, archive_first["document_id"])
        permanently_delete_document(data_dir, archive_second["document_id"])
        proposed = propose_document(
            data_dir,
            "第二份内容。\n",
            "第二份修改内容。\n",
            created["revision"],
            created["document_id"],
        )
        assert proposed["review"] is not None
        assert read_state(data_dir, MAIN_DOCUMENT_ID)["review"] is None
        reject_review(data_dir, created["document_id"])
        untitled = write_document(data_dir, "没有一级标题的正文。\n", created["revision"], created["document_id"])
        assert untitled["title"] == "未命名文档"
        untitled_title = next(
            document["title"]
            for document in list_documents(data_dir)["documents"]
            if document["id"] == created["document_id"]
        )
        assert untitled_title == "未命名文档"
        set_active_document(data_dir, MAIN_DOCUMENT_ID)
        assert list_documents(data_dir)["active_id"] == MAIN_DOCUMENT_ID
        assert [document["id"] for document in list_documents(data_dir)["documents"]] == [MAIN_DOCUMENT_ID, created["document_id"]]
        deleted = delete_document(data_dir, created["document_id"])
        assert deleted["active_id"] == MAIN_DOCUMENT_ID
        assert [document["id"] for document in deleted["documents"]] == [MAIN_DOCUMENT_ID]
        assert [document["id"] for document in deleted["archived_documents"]] == [created["document_id"]]
        assert document_path(data_dir, created["document_id"]).exists()
        deleted = permanently_delete_document(data_dir, created["document_id"])
        assert deleted["archived_documents"] == []
        assert not document_path(data_dir, created["document_id"]).exists()
        empty = delete_document(data_dir, MAIN_DOCUMENT_ID)
        assert empty["active_id"] is None
        assert empty["documents"] == []
        assert [document["id"] for document in empty["archived_documents"]] == [MAIN_DOCUMENT_ID]
        assert document_path(data_dir, MAIN_DOCUMENT_ID).exists()
        restored_empty = restore_document(data_dir, MAIN_DOCUMENT_ID)
        assert restored_empty["active_id"] == MAIN_DOCUMENT_ID
        assert read_state(data_dir, MAIN_DOCUMENT_ID)["content"] == initial
        delete_document(data_dir, MAIN_DOCUMENT_ID)
        empty = permanently_delete_document(data_dir, MAIN_DOCUMENT_ID)
        assert empty == {"active_id": None, "documents": [], "archived_documents": []}
        assert not document_path(data_dir, MAIN_DOCUMENT_ID).exists()
        recreated = create_document(data_dir, "# 重建文档\n")
        assert recreated["title"] == "重建文档"
        print("writing-canvas self-check: OK")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Writing Canvas service")
    parser.add_argument(
        "command",
        choices=["init", "list", "create", "read", "write", "propose", "ensure", "serve", "self-check"],
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/Users/suyunian/.codex/writing-canvas-data"))
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--expected-revision")
    parser.add_argument("--document-id")
    parser.add_argument("--title")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "self-check":
            self_check()
            return 0
        data_dir = args.data_dir.expanduser().resolve()
        if args.command == "init":
            with data_lock(data_dir):
                ensure_data_dir(data_dir)
            print(str(data_dir))
            return 0
        if args.command == "list":
            print(json.dumps(list_documents(data_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "create":
            result = create_document(data_dir, sys.stdin.read(), args.title)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "read":
            print(json.dumps(read_state(data_dir, args.document_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "write":
            result = write_document(data_dir, sys.stdin.read(), args.expected_revision, args.document_id)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "propose":
            try:
                request = json.loads(sys.stdin.read())
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CanvasError("提案 JSON 无效") from error
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("source"), str)
                or not isinstance(request.get("replacement"), str)
            ):
                raise CanvasError("提案必须包含 source 和 replacement 字符串")
            result = propose_document(
                data_dir,
                request["source"],
                request["replacement"],
                args.expected_revision,
                args.document_id,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "ensure":
            print(ensure_service(data_dir, args.port))
            return 0
        if args.command == "serve":
            ensure_data_dir(data_dir)
            server = CanvasHTTPServer((HOST, args.port), data_dir)
            print(service_url(args.port), flush=True)
            server.serve_forever()
            return 0
    except (CanvasError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
