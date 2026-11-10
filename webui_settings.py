"""由 WebUI 管理的插件设置，独立于 config.toml。"""

from pathlib import Path
from typing import List, Literal, Optional

import json
import re
import threading
import tomllib

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

from .command_registry import COMMAND_IDS, COMMAND_SPECS
from .role_patterns import validate_guard_reply


CommandId = Literal.__getitem__(COMMAND_IDS)


class GuardReplyConfig(PluginConfigBase):
    whitelist_denied: str = Field(
        default="这里还不是小花能营业的地方呢，先请管理员把这里加进白名单吧喵～",
        max_length=2000,
        description="群聊或私聊不在白名单、且白名单已开启时的回复。",
    )
    admin_only: str = Field(
        default="这个指令只有管理员才能用哦，小花不能偷偷听别人的话喵～",
        max_length=2000,
        description="非管理员使用仅管理员指令时的回复。",
    )
    quota_exceeded: str = Field(
        default="「{指令}」的次数用完啦，{重置时间}后再来找小花玩吧喵～",
        max_length=2000,
        description="次数用完时的回复，支持 {指令}、{重置时间}。",
    )

    @field_validator("*")
    @classmethod
    def validate_templates(cls, value: str) -> str:
        return validate_guard_reply(value)


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


class WebUISettings(PluginConfigBase):
    """WebUI 设置文件的结构；后续绘图模块可以继续在这里扩展。"""

    access: AccessConfig = Field(default_factory=AccessConfig)
    commands: CommandPolicyConfig = Field(default_factory=CommandPolicyConfig)
    guard_replies: GuardReplyConfig = Field(default_factory=GuardReplyConfig)


def load_legacy_settings(config_path: Path) -> Optional[WebUISettings]:
    """从旧版 config.toml 读取曾经放在插件配置里的设置。"""
    if not config_path.is_file():
        return None
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    sections = {key: raw[key] for key in ("access", "commands", "guard_replies") if key in raw}
    if not sections:
        return None
    return WebUISettings.model_validate(
        {
            "access": sections.get("access", {}),
            "commands": sections.get("commands", {}),
            "guard_replies": sections.get("guard_replies", {}),
        }
    )


class WebUISettingsStore:
    """读写 data_dir 下的 webui_settings.json。"""

    FILE_NAME = "webui_settings.json"

    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / self.FILE_NAME
        self._lock = threading.Lock()
        self._settings: Optional[WebUISettings] = None

    def load(self, legacy: Optional[WebUISettings] = None) -> WebUISettings:
        with self._lock:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                settings = WebUISettings.model_validate(data)
            else:
                settings = legacy if legacy is not None else WebUISettings()
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
