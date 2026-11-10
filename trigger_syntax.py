"""触发模板的公共语法：可选的前置 @ 表示必须先 @ 机器人。"""

from typing import Tuple

import re


AT_MARKER = "@"


def split_mention_requirement(template: str) -> Tuple[bool, str]:
    """拆出模板开头的 @ 标记，返回（是否需要 @ 机器人，正文模板）。"""
    stripped = template.strip()
    if not stripped.startswith(AT_MARKER):
        return False, stripped
    body = stripped[1:].lstrip()
    if not body:
        raise ValueError("模板开头的 @ 后必须还有触发关键词")
    return True, body


def normalize_template_key(template: str) -> str:
    """用于查重时忽略前置 @ 和空白差异。"""
    _, body = split_mention_requirement(template)
    return re.sub(r"\s+", "", body)
