"""角色库触发模板与回复模板，不依赖聊天平台。"""

from dataclasses import dataclass
from typing import List, Optional, Pattern, Set

import re
import string
import unicodedata


ROLE_FIELD = "{角色名}"
REPLY_FIELDS = {"角色名"}
GUARD_FIELDS = {"指令", "重置时间"}
DRAWING_FIELDS = {"类型", "角色数量"}
DRAWING_COUNT_FIELDS = {"最少", "最多"}
DRAWING_ERROR_FIELDS = {"错误"}
TASK_FIELDS = {"任务ID", "排队数"}
TASK_FAIL_FIELDS = {"任务ID", "错误"}


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


def _validate_template_fields(template: str, allowed: Set[str], message: str) -> str:
    for _, field, format_spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in allowed or format_spec or conversion):
            raise ValueError(message)
    return template


def validate_reply(template: str) -> str:
    """只允许简单命名占位符，拒绝属性访问、格式化指令和拼错的变量。"""
    return _validate_template_fields(template, REPLY_FIELDS, "回复模板仅支持 {角色名}，不支持格式化指令")


def validate_guard_reply(template: str) -> str:
    """权限提示只允许指令名和重置时间两个占位符。"""
    return _validate_template_fields(template, GUARD_FIELDS, "权限提示仅支持 {指令}、{重置时间}，不支持格式化指令")


def validate_drawing_reply(template: str) -> str:
    return _validate_template_fields(template, DRAWING_FIELDS, "绘图提示仅支持 {类型}、{角色数量}，不支持格式化指令")


def validate_drawing_count_reply(template: str) -> str:
    return _validate_template_fields(template, DRAWING_COUNT_FIELDS, "人数提示仅支持 {最少}、{最多}，不支持格式化指令")


def validate_drawing_error_reply(template: str) -> str:
    return _validate_template_fields(template, DRAWING_ERROR_FIELDS, "绘图错误提示仅支持 {错误}，不支持格式化指令")


def validate_task_reply(template: str) -> str:
    return _validate_template_fields(template, TASK_FIELDS, "任务提示仅支持 {任务ID}、{排队数}，不支持格式化指令")


def validate_task_fail_reply(template: str) -> str:
    return _validate_template_fields(template, TASK_FAIL_FIELDS, "任务失败提示仅支持 {任务ID}、{错误}，不支持格式化指令")


def render_reply(template: str, role_name: str = "") -> str:
    return template.format_map({"角色名": role_name})


def render_guard_reply(template: str, command_name: str, reset_time: str) -> str:
    return template.format_map({"指令": command_name, "重置时间": reset_time})


def render_drawing_reply(template: str, drawing_type: str, role_count: int) -> str:
    return template.format_map({"类型": drawing_type, "角色数量": role_count})


def render_drawing_count_reply(template: str, minimum: int, maximum: int) -> str:
    return template.format_map({"最少": minimum, "最多": maximum})


def render_drawing_error_reply(template: str, error: str) -> str:
    return template.format_map({"错误": error})


def render_task_reply(template: str, task_id: int, queue_count: int = 0) -> str:
    return template.format_map({"任务ID": task_id, "排队数": queue_count})


def render_task_fail_reply(template: str, task_id: int, error: str) -> str:
    return template.format_map({"任务ID": task_id, "错误": error})
