"""绘图关键词模板：单角色替换、双角色替换和多人替换的匹配。"""

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import re


EXTRA_FIELD = "{额外提示词}"
ROLE_FIELD = "{角色名}"
ROLE_ONE_FIELD = "{角色名1}"
ROLE_TWO_FIELD = "{角色名2}"
ROLE_LIST_FIELD = "{角色名列表}"

DRAWING_KIND_LABELS = {
    "replace": "单角色替换",
    "together": "双角色替换",
    "group": "多人替换",
}

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class DrawingCandidate:
    """尚未解析角色图片的绘图请求。"""

    kind: str
    role_names: List[str]
    roles_text: str
    extra_prompt: str


def _validate_placeholders(template: str, allowed: Set[str]) -> None:
    fields = _PLACEHOLDER.findall(template)
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("绘图模板包含未知占位符: " + "、".join(sorted(unknown)))
    for field in allowed:
        if fields.count(field) > 1:
            raise ValueError(f"绘图模板只能包含一次 {{{field}}}")


def _split_extra(template: str) -> Tuple[str, bool]:
    if template.count(EXTRA_FIELD) > 1:
        raise ValueError("绘图模板只能包含一个 {额外提示词}")
    if EXTRA_FIELD not in template:
        return template, False
    if not template.endswith(EXTRA_FIELD):
        raise ValueError("{额外提示词} 必须放在模板末尾")
    base = template[: -len(EXTRA_FIELD)]
    if not base or not base[-1].isspace():
        raise ValueError("{额外提示词} 前需要一个空格")
    return base.rstrip(), True


def _literal(value: str) -> str:
    return "".join(r"\s*" if part.isspace() else re.escape(part) for part in re.split(r"(\s+)", value))


def _optional_extra(has_extra: bool) -> str:
    return r"(?:\s+(?P<extra>.*))?" if has_extra else ""


def _compile(template: str, kind: str) -> re.Pattern[str]:
    if kind == "replace":
        _validate_placeholders(template, {"角色名", "额外提示词"})
        if template.count(ROLE_FIELD) != 1:
            raise ValueError("单角色替换模板必须且只能包含一个 {角色名}")
        base, has_extra = _split_extra(template)
        prefix, suffix = base.split(ROLE_FIELD)
        return re.compile(r"\A" + _literal(prefix) + r"(?P<role>\S+)" + _literal(suffix) + _optional_extra(has_extra) + r"\Z")

    if kind == "together":
        _validate_placeholders(template, {"角色名1", "角色名2", "额外提示词"})
        if template.count(ROLE_ONE_FIELD) != 1 or template.count(ROLE_TWO_FIELD) != 1:
            raise ValueError("双角色替换模板必须各包含一次 {角色名1} 和 {角色名2}")
        base, has_extra = _split_extra(template)
        pair = ROLE_ONE_FIELD + ROLE_TWO_FIELD
        if pair not in base:
            raise ValueError("{角色名1} 和 {角色名2} 必须相邻")
        prefix, suffix = base.split(pair)
        return re.compile(r"\A" + _literal(prefix) + r"(?P<roles>.+?)" + _literal(suffix) + _optional_extra(has_extra) + r"\Z")

    if kind == "group":
        _validate_placeholders(template, {"角色名列表", "额外提示词"})
        if template.count(ROLE_LIST_FIELD) != 1:
            raise ValueError("多人替换模板必须且只能包含一个 {角色名列表}")
        base, has_extra = _split_extra(template)
        prefix, suffix = base.split(ROLE_LIST_FIELD)
        return re.compile(
            r"\A" + _literal(prefix) + r"(?P<roles>(?:@\S+)+)" + _literal(suffix) + _optional_extra(has_extra) + r"\Z"
        )

    raise ValueError(f"未知绘图类型: {kind}")


def validate_templates(templates: List[str], kind: str) -> None:
    if not templates:
        raise ValueError(f"{DRAWING_KIND_LABELS[kind]}至少需要一个触发模板")
    for template in templates:
        _compile(template, kind)


def _candidate(match: re.Match[str], kind: str) -> DrawingCandidate:
    extra = (match.groupdict().get("extra") or "").strip()
    if kind == "replace":
        return DrawingCandidate(kind, [match.group("role").strip()], "", extra)
    roles_text = match.group("roles").strip()
    if kind == "group":
        names = [part.strip() for part in roles_text.split("@") if part.strip()]
        return DrawingCandidate(kind, names, roles_text, extra)
    return DrawingCandidate(kind, [], roles_text, extra)


def match_drawing_request(
    text: str,
    replace_templates: List[str],
    together_templates: List[str],
    group_templates: List[str],
) -> Optional[DrawingCandidate]:
    """按模板字面长度优先匹配，避免短别名截获更长模板。"""
    rules = [
        (len(template), "replace", template)
        for template in replace_templates
    ] + [
        (len(template), "together", template)
        for template in together_templates
    ] + [
        (len(template), "group", template)
        for template in group_templates
    ]
    rules.sort(key=lambda item: item[0], reverse=True)
    normalized = text.strip()
    for _, kind, template in rules:
        match = _compile(template, kind).fullmatch(normalized)
        if match is not None:
            return _candidate(match, kind)
    return None
