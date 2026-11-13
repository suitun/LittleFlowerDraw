"""由 WebUI 管理的插件设置，独立于 config.toml。"""

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import json
import re
import threading
import tomllib

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

from .command_registry import COMMAND_IDS, COMMAND_SPECS
from .drawing_patterns import validate_templates as validate_drawing_templates
from .role_patterns import (
    normalize_reply_variants,
    validate_drawing_count_reply,
    validate_drawing_error_reply,
    validate_drawing_reply,
    validate_guard_reply,
    validate_reply,
    validate_task_fail_reply,
    validate_task_reply,
)
from .settings import KeywordConfig, LibraryConfig, ReplyConfig


CommandId = Literal.__getitem__(COMMAND_IDS)


_LEGACY_DRAWING_PROMPTS = {
    "replace_prompt": "把图1中的角色替换为图2中的角色，保持姿势、构图、背景和其他人物不变。",
    "together_prompt": "把图1中的两个角色分别替换为图2和图3中的角色，保持姿势、构图、背景和其他人物不变。",
    "group_prompt": "把图1中的角色依次替换为后续图片中的角色，保持姿势、构图、背景和其他人物不变。",
}
_PREVIOUS_DRAWING_PROMPTS = {
    "replace_prompt": (
        "图1是必须保留的底图，严格保留图1的构图、镜头、裁切、视角、姿势、背景、画风和画面比例。"
        "图2只是角色外观参考，不是底图。只把图1中的人物替换成图2中的角色，替换脸部、发型、发色、服装和配色；"
        "不要改变图1的构图，不要增加画面外内容。如果图1是头像、大头、半身或局部特写，输出必须保持同样的裁切和比例，"
        "绝对不要扩成全身，也不要生成三视图、角色设定图或全身立绘。如果图2是多视角、三视图或角色设定图，只能提取角色外观，"
        "不要复制图2的分栏、姿势、背景或版式。"
    ),
    "together_prompt": (
        "图1是必须保留的底图，严格保留图1的构图、镜头、裁切、视角、姿势、背景、画风和画面比例。"
        "图2和图3分别是两个角色的外观参考，不是底图。只把图1中的两个人物分别替换成图2和图3中的角色，保留图1的一切构图信息；"
        "如果参考图是多视角、三视图或角色设定图，只提取角色外观，不要复制参考图的版式、分栏、姿势或背景。"
        "不要扩图、不要新增画面外内容、不要生成角色设定图或全身立绘。"
    ),
    "group_prompt": (
        "图1是必须保留的底图，严格保留图1的构图、镜头、裁切、视角、姿势、背景、画风和画面比例。"
        "从图2开始依次是每个角色的外观参考，不是底图。只替换图1中的对应人物，保留图1的所有构图信息；"
        "如果角色参考图是多视角、三视图或角色设定图，只提取角色外观，不要复制参考图的版式、分栏、姿势或背景。"
        "不要扩图、不要新增画面外内容、不要生成角色设定图或全身立绘。"
    ),
}
_DRAWING_PROMPTS = {
    "replace_prompt": (
        "图1是底图，图2是角色外观参考。把图1中的人物替换成图2的角色，认准图2的发型、发色、服装和配色，不要换成别的角色。"
        "保留图1的构图、镜头、姿势、背景和裁切；图2是三视图或设定图时只参考角色外观，不要照搬版式。"
        "二次元画风，光影、色彩和细节可以适当发挥，但不要改变画面主体。"
    ),
    "together_prompt": (
        "图1是底图，图2和图3分别是两个角色的外观参考。把图1中的两个人物分别替换成图2和图3的角色，"
        "认准各自的发型、发色、服装和配色。保留图1的构图、镜头、姿势、背景和裁切；参考图是三视图时只取外观，"
        "不要照搬版式。二次元画风，光影和色彩可以适当发挥。"
    ),
    "group_prompt": (
        "图1是底图，后续图片依次是角色外观参考。把图1中的人物按顺序替换成这些角色，认准各自的发型、发色、服装和配色。"
        "保留图1的构图、镜头、姿势、背景和裁切；参考图是三视图时只取外观，不要照搬版式。"
        "二次元画风，光影和色彩可以适当发挥。"
    ),
}


class GuardReplyConfig(PluginConfigBase):
    whitelist_denied: List[str] = Field(
        default_factory=lambda: ["这里还不是小花能营业的地方呢，先请管理员把这里加进白名单吧喵～"],
        description="群聊或私聊不在白名单、且白名单已开启时的回复。",
    )
    admin_only: List[str] = Field(
        default_factory=lambda: ["这个指令只有管理员才能用哦，小花不能偷偷听别人的话喵～"],
        description="非管理员使用仅管理员指令时的回复。",
    )
    quota_exceeded: List[str] = Field(
        default_factory=lambda: ["「{指令}」的次数用完啦，{重置时间}后再来找小花玩吧喵～"],
        description="次数用完时的回复，支持 {指令}、{重置时间}。",
    )
    at_replies: List[str] = Field(default_factory=list, description="触发时 @ 回复人的权限提示类型。")

    @field_validator("*", mode="before")
    @classmethod
    def normalize_variants(cls, value: object) -> List[str]:
        return normalize_reply_variants(value)

    @field_validator("whitelist_denied", "admin_only", "quota_exceeded")
    @classmethod
    def validate_templates(cls, values: List[str]) -> List[str]:
        result = [validate_guard_reply(value) for value in values if value.strip()]
        if not result:
            raise ValueError("权限提示至少需要一条非空内容")
        return result

    @field_validator("at_replies")
    @classmethod
    def validate_at_replies(cls, values: List[str]) -> List[str]:
        allowed = {"whitelist_denied", "admin_only", "quota_exceeded"}
        unknown = [value for value in values if value not in allowed]
        if unknown:
            raise ValueError("未知的权限提示类型: " + "、".join(unknown))
        return list(dict.fromkeys(values))


class AccessConfig(PluginConfigBase):
    admin_qq: str = Field(default="2685275264", description="唯一管理员的 QQ 号；管理员绕过白名单和次数限制。")
    whitelist_enabled: bool = Field(
        default=False,
        description="关闭时所有群聊和私聊可用；开启后只允许下方白名单中的群聊或私聊。",
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="允许使用指令的 QQ 群号；开启白名单后，留空表示没有群聊可用。",
    )
    private_whitelist: List[str] = Field(
        default_factory=list,
        description="允许使用指令的 QQ 号；开启白名单后，留空表示没有私聊可用。",
    )

    @field_validator("admin_qq", mode="before")
    @classmethod
    def validate_admin_qq(cls, value: object) -> str:
        value = str(value).strip()
        if not re.fullmatch(r"\d{5,12}", value):
            raise ValueError("管理员 QQ 必须是 5-12 位数字")
        return value

    @field_validator("group_whitelist", "private_whitelist", mode="before")
    @classmethod
    def validate_whitelist(cls, values: List[object]) -> List[str]:
        result: List[str] = []
        for item in values:
            value = str(item).strip()
            if not re.fullmatch(r"\d{5,12}", value):
                raise ValueError("白名单只能填写 5-12 位数字 QQ 号或群号")
            if value not in result:
                result.append(value)
        if len(result) > 200:
            raise ValueError("白名单最多填写 200 项")
        return result


class CommandPolicy(PluginConfigBase):
    command: CommandId = Field(default=COMMAND_IDS[0], description="指令")
    permission: Literal["仅管理员", "所有人"] = Field(default="所有人", description="权限")
    limit: int = Field(default=0, ge=0, le=1_000_000, description="每人每指令的次数上限；0 表示不限次数。")
    reset_days: int = Field(default=1, ge=1, le=365, description="次数重置间隔（天），在到期日的 0 点重置。")

    @field_validator("command", mode="before")
    @classmethod
    def migrate_legacy_ranking(cls, value: object) -> object:
        return "排行榜" if value == "小猪排行榜" else value


def default_command_policies() -> List[CommandPolicy]:
    return [CommandPolicy(command=spec.command_id) for spec in COMMAND_SPECS]


class CommandPolicyConfig(PluginConfigBase):
    policies: List[CommandPolicy] = Field(
        default_factory=default_command_policies,
        description="每个指令一行；次数按每人每指令统计，跨群共享。",
    )

    @model_validator(mode="after")
    def ensure_all_commands(self) -> "CommandPolicyConfig":
        by_command = {}
        for policy in self.policies:
            if policy.command in by_command:
                raise ValueError(f"指令权限配置重复: {policy.command}")
            by_command[policy.command] = policy
        defaults = {policy.command: policy for policy in default_command_policies()}
        unknown = set(by_command) - set(defaults)
        if unknown:
            raise ValueError("存在未登记的指令配置: " + "、".join(sorted(unknown)))
        self.policies[:] = [by_command.get(spec.command_id, defaults[spec.command_id]) for spec in COMMAND_SPECS]
        return self


class DrawingConfig(PluginConfigBase):
    replace_templates: List[str] = Field(
        default_factory=lambda: ["变{角色名} {额外提示词}", "{角色名}穿 {额外提示词}"],
        description="单角色替换触发模板，每行一条；必须包含 {角色名}，可选 {额外提示词}。",
    )
    together_templates: List[str] = Field(
        default_factory=lambda: ["{角色名1}{角色名2}一起穿 {额外提示词}"],
        description="双角色替换触发模板，每行一条；必须包含 {角色名1}{角色名2}，可选 {额外提示词}。",
    )
    group_templates: List[str] = Field(
        default_factory=lambda: ["多人穿{角色名列表} {额外提示词}"],
        description="多人替换触发模板，每行一条；必须包含 {角色名列表}，角色用 @ 分隔。",
    )
    free_templates: List[str] = Field(
        default_factory=lambda: ["小花画{提示词}"],
        description="自由绘画触发模板，每行一条；必须包含 {提示词}，提示词可以为空。",
    )
    fallback_prompt: str = Field(
        default="发挥创意",
        max_length=8000,
        description="自由绘画没有填写提示词时使用的兜底提示词。",
    )
    replace_prompt: str = Field(
        default=_DRAWING_PROMPTS["replace_prompt"],
        max_length=8000,
        description="单角色替换的基础提示词，额外提示词会接在后面。",
    )
    together_prompt: str = Field(
        default=_DRAWING_PROMPTS["together_prompt"],
        max_length=8000,
        description="双角色替换的基础提示词。",
    )
    group_prompt: str = Field(
        default=_DRAWING_PROMPTS["group_prompt"],
        max_length=8000,
        description="多人替换的基础提示词。",
    )
    extra_prompt_prefix: str = Field(
        default="注意用户额外需求：",
        max_length=80,
        description="有额外提示词时加在基础提示词和额外需求之间的引导语。",
    )
    min_group_roles: int = Field(default=3, ge=3, le=8, description="多人替换最少角色数。")
    max_group_roles: int = Field(default=8, ge=3, le=8, description="多人替换最多角色数。")
    missing_reference_reply: List[str] = Field(
        default_factory=lambda: ["画图需要一张参考图哦，请附图或引用一条有图的消息喵～"],
        description="绘图消息没有参考图时的回复。",
    )
    missing_role_reply: List[str] = Field(
        default_factory=lambda: ["小花还不认识「{角色名}」呢，先把 TA 登记进角色库吧喵～"],
        description="绘图引用的角色不存在时的回复。",
    )
    group_count_reply: List[str] = Field(
        default_factory=lambda: ["多人替换需要 {最少}-{最多} 个角色喵～"],
        description="多人替换角色数量超出范围时的回复。",
    )
    api_missing_reply: List[str] = Field(
        default_factory=lambda: ["绘图 API 还没配置好，请先在「小花绘 → 绘图 API」填写 URL 和 Token 喵～"],
        description="绘图 API 未配置时的回复。",
    )
    api_error_reply: List[str] = Field(
        default_factory=lambda: ["绘图 API 调用失败：{错误}"],
        description="绘图 API 调用失败时的回复，支持 {错误}。",
    )
    completion_reply: List[str] = Field(
        default_factory=lambda: ["任务 #{任务ID} 画好啦喵～"],
        description="任务成功时的短回复；只显示任务编号和短文案。",
    )
    submitted_reply: List[str] = Field(
        default_factory=lambda: ["任务 #{任务ID} 收到啦喵～前面还有 {排队数} 个。"],
        description="任务入队后的回复，支持 {任务ID}、{排队数}。",
    )
    active_task_reply: List[str] = Field(
        default_factory=lambda: ["你还有任务 #{任务ID} 在画喵～等它完成再来吧。"],
        description="用户已有进行中任务时的回复，支持 {任务ID}。",
    )
    queue_full_reply: List[str] = Field(
        default_factory=lambda: ["排队的小花太多啦，稍后再来喵～"],
        description="任务队列已满时的回复。",
    )
    duplicate_task_reply: List[str] = Field(
        default_factory=lambda: ["这个任务已经提交过啦喵～"],
        description="重复消息再次提交任务时的回复。",
    )
    task_failed_reply: List[str] = Field(
        default_factory=lambda: ["任务 #{任务ID} 画失败了喵…{错误}"],
        description="任务失败时的回复，支持 {任务ID}、{错误}。",
    )
    image_rejected_reply: List[str] = Field(
        default_factory=lambda: ["任务 #{任务ID} 这张图小花画不了喵，换一张参考图再试试吧～"],
        description="接口返回 400（图片不合规）时的短回复，支持 {任务ID}。",
    )
    at_replies: List[str] = Field(default_factory=list, description="触发时 @ 回复人的绘图任务回复类型。")

    @field_validator(
        "missing_reference_reply",
        "missing_role_reply",
        "group_count_reply",
        "api_missing_reply",
        "api_error_reply",
        "completion_reply",
        "submitted_reply",
        "active_task_reply",
        "queue_full_reply",
        "duplicate_task_reply",
        "task_failed_reply",
        "image_rejected_reply",
        mode="before",
    )
    @classmethod
    def normalize_reply_fields(cls, value: object) -> List[str]:
        return normalize_reply_variants(value)

    @field_validator("at_replies", mode="before")
    @classmethod
    def normalize_at_replies(cls, value: object) -> List[str]:
        return normalize_reply_variants(value, max_items=32)

    @field_validator("at_replies")
    @classmethod
    def validate_at_replies(cls, values: List[str]) -> List[str]:
        allowed = {
            "missing_reference_reply",
            "missing_role_reply",
            "group_count_reply",
            "api_missing_reply",
            "api_error_reply",
            "completion_reply",
            "submitted_reply",
            "active_task_reply",
            "queue_full_reply",
            "duplicate_task_reply",
            "task_failed_reply",
            "image_rejected_reply",
        }
        unknown = [value for value in values if value not in allowed]
        if unknown:
            raise ValueError("未知的绘图任务回复类型: " + "、".join(unknown))
        return list(dict.fromkeys(values))

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_prompts(cls, data: object) -> object:
        if isinstance(data, dict):
            updated = dict(data)
            for field, replacement in _DRAWING_PROMPTS.items():
                if updated.get(field) in {_LEGACY_DRAWING_PROMPTS[field], _PREVIOUS_DRAWING_PROMPTS[field]}:
                    updated[field] = replacement
            return updated
        return data

    @field_validator("replace_templates")
    @classmethod
    def validate_replace_templates(cls, values: List[str]) -> List[str]:
        validate_drawing_templates(values, "replace")
        return values

    @field_validator("together_templates")
    @classmethod
    def validate_together_templates(cls, values: List[str]) -> List[str]:
        validate_drawing_templates(values, "together")
        return values

    @field_validator("group_templates")
    @classmethod
    def validate_group_templates(cls, values: List[str]) -> List[str]:
        validate_drawing_templates(values, "group")
        return values

    @field_validator("free_templates")
    @classmethod
    def validate_free_templates(cls, values: List[str]) -> List[str]:
        validate_drawing_templates(values, "free")
        return values

    @field_validator("missing_role_reply")
    @classmethod
    def validate_missing_role(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @field_validator("group_count_reply")
    @classmethod
    def validate_group_count(cls, values: List[str]) -> List[str]:
        return [validate_drawing_count_reply(value) for value in values if value.strip()]

    @field_validator("api_missing_reply")
    @classmethod
    def validate_api_missing(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @field_validator("api_error_reply")
    @classmethod
    def validate_api_error(cls, values: List[str]) -> List[str]:
        return [validate_drawing_error_reply(value) for value in values if value.strip()]

    @field_validator("completion_reply")
    @classmethod
    def validate_completion(cls, values: List[str]) -> List[str]:
        return [validate_task_reply(value) for value in values if value.strip()]

    @field_validator("submitted_reply", "active_task_reply")
    @classmethod
    def validate_task_template(cls, values: List[str]) -> List[str]:
        return [validate_task_reply(value) for value in values if value.strip()]

    @field_validator("queue_full_reply")
    @classmethod
    def validate_queue_full(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @field_validator("duplicate_task_reply")
    @classmethod
    def validate_duplicate_task(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @field_validator("task_failed_reply")
    @classmethod
    def validate_task_failed(cls, values: List[str]) -> List[str]:
        return [validate_task_fail_reply(value) for value in values if value.strip()]

    @field_validator("image_rejected_reply")
    @classmethod
    def validate_image_rejected(cls, values: List[str]) -> List[str]:
        return [validate_task_reply(value) for value in values if value.strip()]

    @field_validator("missing_reference_reply")
    @classmethod
    def validate_missing_reference(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @model_validator(mode="after")
    def ensure_replies_present(self) -> "DrawingConfig":
        for name in (
            "missing_reference_reply",
            "missing_role_reply",
            "group_count_reply",
            "api_missing_reply",
            "api_error_reply",
            "completion_reply",
            "submitted_reply",
            "active_task_reply",
            "queue_full_reply",
            "duplicate_task_reply",
            "task_failed_reply",
            "image_rejected_reply",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} 至少需要一条回复")
        return self

    @model_validator(mode="after")
    def validate_group_range(self) -> "DrawingConfig":
        if self.min_group_roles > self.max_group_roles:
            raise ValueError("多人替换的最少角色数不能大于最多角色数")
        return self


class UserQuota(PluginConfigBase):
    """给指定 QQ 的某项指令增加额外次数。"""

    user_id: str = Field(..., min_length=5, max_length=20, pattern=r"^\d{5,20}$", description="QQ 号")
    command: str = Field(..., min_length=1, max_length=64, description="指令名称")
    extra_count: int = Field(default=1, ge=1, le=100000, description="在原有次数上额外增加的次数")
    note: str = Field(default="", max_length=200, description="备注，仅用于说明")

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if value not in COMMAND_IDS:
            raise ValueError(f"未知指令: {value}")
        return value

    @field_validator("command", mode="before")
    @classmethod
    def migrate_legacy_ranking(cls, value: object) -> object:
        return "排行榜" if value == "小猪排行榜" else value


class TaskQueueConfig(PluginConfigBase):
    max_concurrent_tasks: int = Field(default=1, ge=1, le=8, description="同时执行的绘图任务数")
    task_interval_seconds: int = Field(
        default=5,
        ge=0,
        le=300,
        description="上一张画完后等待多少秒再开始下一张，避免中转站连续请求。",
    )
    max_waiting_tasks: int = Field(default=20, ge=0, le=1000, description="最多排队任务数，0 表示不限")
    task_timeout_seconds: int = Field(default=600, ge=30, le=3600, description="单个任务执行超时秒数")
    poll_interval_seconds: int = Field(default=2, ge=1, le=60, description="队列轮询间隔秒数")
    history_retention_days: int = Field(default=30, ge=0, le=3650, description="历史元数据保留天数，0 表示不自动清理")
    output_retention_days: int = Field(default=30, ge=0, le=3650, description="生成图片保留天数，0 表示不自动清理")
    queue_paused: bool = Field(default=False, description="暂停领取新任务；当前执行中的任务继续完成")


class WebUISettings(PluginConfigBase):
    """WebUI 设置文件的结构；后续绘图模块可以继续在这里扩展。"""

    keywords: KeywordConfig = Field(default_factory=KeywordConfig)
    replies: ReplyConfig = Field(default_factory=ReplyConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    drawing: DrawingConfig = Field(default_factory=DrawingConfig)
    task_queue: TaskQueueConfig = Field(default_factory=TaskQueueConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    commands: CommandPolicyConfig = Field(default_factory=CommandPolicyConfig)
    guard_replies: GuardReplyConfig = Field(default_factory=GuardReplyConfig)
    user_quotas: List[UserQuota] = Field(default_factory=list, description="指定 QQ 的指令额外次数")

    @model_validator(mode="after")
    def validate_unique_user_quotas(self) -> "WebUISettings":
        seen: set[tuple[str, str]] = set()
        for quota in self.user_quotas:
            key = (quota.user_id, quota.command)
            if key in seen:
                raise ValueError(f"用户 {quota.user_id} 的「{quota.command}」额外次数重复")
            seen.add(key)
        return self


def load_legacy_settings(config_path: Path) -> Optional[Dict[str, Any]]:
    """从旧版 config.toml 读取曾经放在插件配置里的设置。"""
    if not config_path.is_file():
        return None
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    sections = {
        key: raw[key]
        for key in ("keywords", "replies", "library", "drawing", "task_queue", "access", "commands", "guard_replies")
        if key in raw
    }
    return sections or None


class WebUISettingsStore:
    """读写 data_dir 下的 webui_settings.json。"""

    FILE_NAME = "webui_settings.json"

    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / self.FILE_NAME
        self._lock = threading.Lock()
        self._settings: Optional[WebUISettings] = None

    def load(self, legacy: Optional[Dict[str, Any]] = None) -> WebUISettings:
        with self._lock:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                changed = False
                if legacy:
                    for key, value in legacy.items():
                        if key not in data:
                            data[key] = value
                            changed = True
                settings = WebUISettings.model_validate(data)
                if not changed and data != settings.model_dump(mode="json"):
                    changed = True
                if changed:
                    self._write(settings)
            else:
                settings = WebUISettings.model_validate(legacy or {})
                self._write(settings)
            self._settings = settings
            return settings.model_copy(deep=True)

    def current(self) -> WebUISettings:
        with self._lock:
            if self._settings is None:
                raise RuntimeError("WebUI 设置尚未加载")
            return self._settings.model_copy(deep=True)

    def save(self, settings: WebUISettings) -> None:
        with self._lock:
            self._write(settings)
            self._settings = settings

    def _write(self, settings: WebUISettings) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
