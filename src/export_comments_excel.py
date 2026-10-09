"""Export top-level Douyin comments from comments.json to an Excel workbook."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


HEADERS = (
    "序号",
    "层级",
    "评论文字",
    "作者",
    "抖音号",
    "点赞数",
    "回复数",
    "父评论 ID",
    "根评论 ID",
    "发布时间",
    "IP 属地",
    "是否带图",
    "评论 ID",
)


def _comments_from_json(json_path: Path) -> list[dict[str, Any]]:
    with json_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    comments = payload.get("comments")
    if not isinstance(comments, list):
        raise ValueError("文件中未找到 comments 列表")
    return [comment for comment in comments if isinstance(comment, dict)]


def _comment_row(index: int, comment: dict[str, Any]) -> tuple[Any, ...]:
    author = comment.get("author")
    nickname = author.get("nickname", "") if isinstance(author, dict) else ""
    douyin_id = str(author.get("unique_id") or "").strip() if isinstance(author, dict) else ""
    images = comment.get("images")
    has_images = isinstance(images, list) and any(
        isinstance(image, dict) and image.get("file") for image in images
    )
    return (
        index,
        int(comment.get("level") or 1),
        str(comment.get("text") or ""),
        str(nickname or ""),
        douyin_id or "未获取",
        int(comment.get("likes") or 0),
        int(comment.get("reply_count") or 0),
        str(comment.get("parent_comment_id") or ""),
        str(comment.get("root_comment_id") or comment.get("id") or ""),
        str(comment.get("create_time_iso") or ""),
        str(comment.get("ip_label") or ""),
        "是" if has_images else "否",
        str(comment.get("id") or ""),
    )


def export_comments_to_excel(json_path: Path, output_path: Path) -> int:
    """Write comments from *json_path* into an .xlsx file and return row count."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError as error:
        raise RuntimeError(
            "缺少 openpyxl。请在项目根目录执行：python -m pip install -e ."
        ) from error

    comments = _comments_from_json(json_path)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评论"
    sheet.freeze_panes = "A2"
    sheet.append(HEADERS)

    header_fill = PatternFill("solid", fgColor="0F766E")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for index, comment in enumerate(comments, start=1):
        sheet.append(_comment_row(index, comment))

    for row in sheet.iter_rows(min_row=2):
        row[2].alignment = Alignment(wrap_text=True, vertical="top")
        # Account identifiers are text, including numeric IDs and leading zeros.
        row[4].number_format = "@"
        row[4].data_type = "s"
        for cell in row:
            if cell.column != 3:
                cell.alignment = Alignment(vertical="top")

    widths = {"A": 8, "B": 8, "C": 60, "D": 20, "E": 24, "F": 12, "G": 12, "H": 24, "I": 24, "J": 24, "K": 14, "L": 12, "M": 24}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    sheet.auto_filter.ref = f"A1:M{max(2, len(comments) + 1)}"
    if comments:
        table = Table(displayName="DouyinComments", ref=f"A1:M{len(comments) + 1}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return len(comments)


def _read_json_path_from_console() -> Path:
    entered = input("请输入 comments.json 文件路径，然后按回车：").strip().strip('"')
    if not entered:
        raise ValueError("未输入 comments.json 文件路径")
    return Path(entered).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将抖音 comments.json 导出为 Excel")
    parser.add_argument("json_path", nargs="?", type=Path, help="comments.json 路径")
    parser.add_argument("--output", "-o", type=Path, help="Excel 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path = args.json_path or _read_json_path_from_console()
    json_path = json_path.expanduser().resolve()
    if not json_path.is_file():
        print(f"找不到 JSON 文件：{json_path}", file=sys.stderr)
        return 2

    output_path = (args.output or json_path.with_name("comments.xlsx")).expanduser()
    if output_path.suffix.lower() != ".xlsx":
        output_path = output_path.with_suffix(".xlsx")
    output_path = output_path.resolve()

    try:
        count = export_comments_to_excel(json_path, output_path)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"导出失败：{error}", file=sys.stderr)
        return 1

    print(f"导出完成：{count} 条评论")
    print(f"Excel 文件：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
