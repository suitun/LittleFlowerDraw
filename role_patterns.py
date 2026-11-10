"""角色库触发模板与回复模板，不依赖聊天平台。"""

from dataclasses import dataclass
from typing import List, Optional, Pattern

import re
import string
import unicodedata


ROLE_FIELD = "{角色名}"
REPLY_FIELDS = {"角色名", "图片数量"}


@dataclass(frozen=True)
class RoleRequest:
    action: str
    role_name: str


def compile_trigger(template: str) -> Pattern[str]:
    """将带角色名占位符的文字模板编译成完整消息匹配。"""
    template = template.strip()
    if template.count(ROLE_FIELD) != 1:
        raise ValueError("触发模板必须且只能包含一个 {角色名}")
    prefix, suffix = template.split(ROLE_FIELD)
    if not prefix.strip():
        raise ValueError("角色名前必须有触发关键词")
    if "{" in prefix + suffix or "}" in prefix + suffix:
        raise ValueError("触发模板只支持 {角色名} 占位符")

    def literal(value: str) -> str:
        return "".join(r"\s*" if part.isspace() else re.escape(part) for part in re.split(r"(\s+)", value))

    return re.compile(r"\A\s*" + literal(prefix) + r"(?P<role_name>.*?)" + literal(suffix) + r"\s*\Z", re.DOTALL)


def validate_trigger_groups(register: List[str], view: List[str]) -> None:
    """禁止空规则集及两个操作使用相同模板。"""
    seen = set()
    for templates in (register, view):
        if not templates:
            raise ValueError("登记和查看各需要至少一个触发模板")
        for template in templates:
            compile_trigger(template)
            key = re.sub(r"\s+", "", template)
            if key in seen:
                raise ValueError("登记和查看触发模板不能重复")
            seen.add(key)


def match_request(text: str, register: List[str], view: List[str]) -> Optional[RoleRequest]:
    """优先匹配文字更具体的模板，避免较短别名截获较长别名。"""
    rules = [(action, template) for action, templates in (("register", register), ("view", view)) for template in templates]
    rules.sort(key=lambda item: len(item[1].replace(ROLE_FIELD, "")), reverse=True)
    for action, template in rules:
        match = compile_trigger(template).fullmatch(text)
        if match is not None:
            return RoleRequest(action, unicodedata.normalize("NFC", match.group("role_name").strip()))
    return None


def valid_role_name(name: str, max_length: int) -> bool:
    return bool(name) and len(name) <= max_length and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in name)


def validate_reply(template: str) -> str:
    """只允许简单命名占位符，拒绝属性访问、格式化指令和拼错的变量。"""
    for _, field, format_spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in REPLY_FIELDS or format_spec or conversion):
            raise ValueError("回复模板仅支持 {角色名}、{图片数量}，不支持格式化指令")
    return template


def render_reply(template: str, role_name: str = "", image_count: int = 0) -> str:
    return template.format_map({"角色名": role_name, "图片数量": image_count})
