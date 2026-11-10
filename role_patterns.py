"""角色库触发模板与回复模板，不依赖聊天平台。"""

from dataclasses import dataclass
from typing import Any, List, Optional, Pattern, Sequence, Set, Union

import random
import re
import string
import unicodedata

from .trigger_syntax import normalize_template_key, split_mention_requirement


ROLE_FIELD = "{角色名}"
REPLY_FIELDS = {"角色名"}
LIST_FIELDS = {"角色数量", "角色列表"}
GUARD_FIELDS = {"指令", "重置时间"}
DRAWING_FIELDS = {"类型", "角色数量"}
DRAWING_COUNT_FIELDS = {"最少", "最多"}
DRAWING_ERROR_FIELDS = {"错误"}
TASK_FIELDS = {"任务ID", "排队数"}
TASK_FAIL_FIELDS = {"任务ID", "错误"}
IMAGE_REASON_FIELDS = {"原因"}
IMAGE_SIZE_FIELDS = {"实际大小", "大小限制"}
IMAGE_FORMAT_FIELDS = {"格式"}


@dataclass(frozen=True)
class RoleRequest:
    action: str
    role_name: str


def compile_trigger(template: str) -> Pattern[str]:
    """编译模板；开头的 @ 只表示触发时必须 @ 机器人，不参与文字匹配。"""
    _, template = split_mention_requirement(template)
    if template.count(ROLE_FIELD) != 1:
        raise ValueError("触发模板必须且只能包含一个 {角色名}")
    prefix, suffix = template.split(ROLE_FIELD)
    if not prefix.strip():
        raise ValueError("角色名前必须有触发关键词")
    if "{" in prefix + suffix or "}" in prefix + suffix:
        raise ValueError("触发模板只支持 {角色名} 占位符")

    return re.compile(r"\A\s*" + _literal(prefix) + r"(?P<role_name>.*?)" + _literal(suffix) + r"\s*\Z", re.DOTALL)


def _literal(value: str) -> str:
    return "".join(r"\s*" if part.isspace() else re.escape(part) for part in re.split(r"(\s+)", value))


def compile_list_trigger(template: str) -> Pattern[str]:
    """编译无占位符的角色列表指令模板，支持可选前置 @。"""
    _, body = split_mention_requirement(template)
    if not body:
        raise ValueError("角色列表触发词不能为空")
    if "{" in body or "}" in body:
        raise ValueError("角色列表触发词不支持占位符")
    return re.compile(r"\A\s*" + _literal(body) + r"\s*\Z", re.DOTALL)


def validate_trigger_groups(
    register: List[str],
    view: List[str],
    list_templates: Optional[List[str]] = None,
    help_templates: Optional[List[str]] = None,
) -> None:
    """禁止空规则集及两个操作使用相同模板。"""
    list_templates = list_templates or []
    help_templates = help_templates or []
    if not list_templates:
        raise ValueError("角色列表至少需要一个触发模板")
    if not help_templates:
        raise ValueError("帮助指令至少需要一个触发模板")
    seen = set()
    for templates, compiler in (
        (register, compile_trigger),
        (view, compile_trigger),
        (list_templates, compile_list_trigger),
        (help_templates, compile_list_trigger),
    ):
        if not templates:
            raise ValueError("登记、查看、角色列表和帮助各需要至少一个触发模板")
        for template in templates:
            compiler(template)
            key = normalize_template_key(template)
            if key in seen:
                raise ValueError("登记、查看、角色列表和帮助触发模板不能重复")
            seen.add(key)


def match_request(
    text: str,
    register: List[str],
    view: List[str],
    list_templates: Optional[List[str]] = None,
    help_templates: Optional[List[str]] = None,
    *,
    mentioned_bot: bool = False,
) -> Optional[RoleRequest]:
    """优先匹配文字更具体的模板，避免较短别名截获较长别名。"""
    rules = [
        (action, template)
        for action, templates in (("register", register), ("view", view))
        for template in templates
    ]
    rules.extend(("list", template) for template in (list_templates or []))
    rules.extend(("help", template) for template in (help_templates or []))
    rules.sort(key=lambda item: len(normalize_template_key(item[1])), reverse=True)
    for action, template in rules:
        requires_at, body = split_mention_requirement(template)
        if requires_at and not mentioned_bot:
            continue
        if action in {"list", "help"}:
            match = compile_list_trigger(body).fullmatch(text)
            if match is not None:
                return RoleRequest(action, "")
            continue
        match = compile_trigger(body).fullmatch(text)
        if match is not None:
            return RoleRequest(action, unicodedata.normalize("NFC", match.group("role_name").strip()))
    return None


def valid_role_name(name: str, max_length: int) -> bool:
    return bool(name) and len(name) <= max_length and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in name)


ReplyValue = Union[str, Sequence[str]]


def pick_reply(value: ReplyValue) -> str:
    """从一条或多条回复中随机抽一条；兼容旧的单字符串配置。"""
    if isinstance(value, str):
        return value
    variants = list(value)
    if not variants:
        raise ValueError("回复配置至少需要一条内容")
    return random.choice(variants)


def normalize_reply_variants(value: Any, max_items: int = 50) -> List[str]:
    """把旧版单字符串或 WebUI 列表统一成字符串列表。"""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("回复配置必须是字符串或字符串列表")
    if len(values) > max_items:
        raise ValueError(f"每种回复最多配置 {max_items} 条")
    result: List[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("回复内容必须是字符串")
        if len(item) > 2000:
            raise ValueError("单条回复不能超过 2000 字")
        result.append(item)
    return result


def _validate_template_fields(template: str, allowed: Set[str], message: str) -> str:
    for _, field, format_spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in allowed or format_spec or conversion):
            raise ValueError(message)
    return template


def validate_reply(template: str) -> str:
    """只允许简单命名占位符，拒绝属性访问、格式化指令和拼错的变量。"""
    return _validate_template_fields(template, REPLY_FIELDS, "回复模板仅支持 {角色名}，不支持格式化指令")


def validate_list_reply(template: str) -> str:
    return _validate_template_fields(
        template, LIST_FIELDS, "角色列表回复仅支持 {角色数量}、{角色列表}，不支持格式化指令"
    )


def validate_plain_reply(template: str) -> str:
    return _validate_template_fields(template, set(), "这条回复不支持任何占位符")


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


def validate_image_reason_reply(template: str) -> str:
    return _validate_template_fields(template, IMAGE_REASON_FIELDS, "图片错误提示仅支持 {原因}，不支持格式化指令")


def validate_image_size_reply(template: str) -> str:
    return _validate_template_fields(template, IMAGE_SIZE_FIELDS, "图片大小提示仅支持 {实际大小}、{大小限制}，不支持格式化指令")


def validate_image_format_reply(template: str) -> str:
    return _validate_template_fields(template, IMAGE_FORMAT_FIELDS, "图片格式提示仅支持 {格式}，不支持格式化指令")


def render_reply(template: ReplyValue, role_name: str = "") -> str:
    return pick_reply(template).format_map({"角色名": role_name})


def render_list_reply(template: ReplyValue, count: int, names: str) -> str:
    return pick_reply(template).format_map({"角色数量": count, "角色列表": names})


def render_guard_reply(template: ReplyValue, command_name: str, reset_time: str) -> str:
    return pick_reply(template).format_map({"指令": command_name, "重置时间": reset_time})


def render_drawing_reply(template: ReplyValue, drawing_type: str, role_count: int) -> str:
    return pick_reply(template).format_map({"类型": drawing_type, "角色数量": role_count})


def render_drawing_count_reply(template: ReplyValue, minimum: int, maximum: int) -> str:
    return pick_reply(template).format_map({"最少": minimum, "最多": maximum})


def render_drawing_error_reply(template: ReplyValue, error: str) -> str:
    return pick_reply(template).format_map({"错误": error})


def render_task_reply(template: ReplyValue, task_id: int, queue_count: int = 0) -> str:
    return pick_reply(template).format_map({"任务ID": task_id, "排队数": queue_count})


def render_task_fail_reply(template: ReplyValue, task_id: int, error: str) -> str:
    return pick_reply(template).format_map({"任务ID": task_id, "错误": error})


def render_image_reason_reply(template: ReplyValue, reason: str) -> str:
    return pick_reply(template).format_map({"原因": reason})


def render_image_size_reply(template: ReplyValue, actual: str, limit: str) -> str:
    return pick_reply(template).format_map({"实际大小": actual, "大小限制": limit})


def render_image_format_reply(template: ReplyValue, image_format: str) -> str:
    return pick_reply(template).format_map({"格式": image_format})
