"""角色库的声明式 WebUI 配置。"""

from typing import List

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

from .role_patterns import (
    normalize_reply_variants,
    validate_image_format_reply,
    validate_image_reason_reply,
    validate_image_size_reply,
    validate_list_reply,
    validate_plain_reply,
    validate_reply,
    validate_trigger_groups,
)


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "基础设置"
    __ui_icon__ = "palette"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用小花绘")
    config_version: str = Field(default="0.6.0", description="配置结构版本")


class KeywordConfig(PluginConfigBase):
    __ui_label__ = "角色库触发词"
    __ui_icon__ = "message-circle"
    __ui_order__ = 1

    register_templates: List[str] = Field(
        default_factory=lambda: ["小花小花这是 {角色名}"],
        description="登记/覆盖触发模板。每条必须包含一次 {角色名}；模板中的空格可省略，可添加多条别名。",
    )
    view_templates: List[str] = Field(
        default_factory=lambda: ["小花小花我要看{角色名}"],
        description="查看触发模板。每条必须包含一次 {角色名}；保存配置后立即按新模板匹配。",
    )
    list_templates: List[str] = Field(
        default_factory=lambda: ["小花记得谁"],
        description="角色列表触发模板。不能包含占位符；可添加多条别名。",
    )
    help_templates: List[str] = Field(
        default_factory=lambda: ["小花绘帮助"],
        description="帮助触发模板。不能包含占位符；可添加多条别名。",
    )
    ranking_templates: List[str] = Field(
        default_factory=lambda: ["小猪排行榜"],
        description="角色绘图次数排行榜触发模板。不能包含占位符；可添加多条别名。",
    )

    @model_validator(mode="after")
    def validate_templates(self) -> "KeywordConfig":
        validate_trigger_groups(
            self.register_templates,
            self.view_templates,
            self.list_templates,
            self.help_templates,
            self.ranking_templates,
        )
        return self


class ReplyConfig(PluginConfigBase):
    __ui_label__ = "角色库回复文案"
    __ui_icon__ = "heart"
    __ui_order__ = 2

    at_replies: List[str] = Field(default_factory=list, description="触发时 @ 回复人的回复类型。")
    registered: List[str] = Field(default_factory=lambda: ["小花记住{角色名}了喵～"], description="首次入库成功回复，支持 {角色名}。")
    overwritten: List[str] = Field(default_factory=lambda: ["{角色名}的新模样，小花也记住了喵～"], description="同名覆盖成功回复。")
    view: List[str] = Field(default_factory=lambda: ["找到{角色名}啦，给你看看喵～"], description="查看图片前的文字，留空只发图片。")
    list_reply: List[str] = Field(
        default_factory=lambda: ["小花记得 {角色数量} 个角色喵～\n{角色列表}"],
        description="角色列表回复，支持 {角色数量}、{角色列表}。",
    )
    list_empty_reply: List[str] = Field(
        default_factory=lambda: ["小花现在还没记住任何角色呢，先介绍一个给小花吧喵～"],
        description="角色库为空时的列表回复，不支持占位符。",
    )
    help_title: List[str] = Field(
        default_factory=lambda: ["小花绘帮助"],
        description="帮助图片标题，不支持占位符。",
    )
    help_footer: List[str] = Field(
        default_factory=lambda: ["带图或引用图片，就能让小花帮忙喵～"],
        description="帮助图片底部提示，不支持占位符。",
    )
    not_found: List[str] = Field(default_factory=lambda: ["小花还不认识{角色名}呢，先把 TA 介绍给小花吧喵～"], description="没有找到该角色时的回复。")
    missing_image: List[str] = Field(default_factory=lambda: ["想记住{角色名}的话，要附上图片，或引用一条有图的消息哦喵～"], description="登记消息和引用目标都没有可用图片时的回复。")
    invalid_name: List[str] = Field(default_factory=lambda: ["这个名字小花有点记不住，换个短一点的单行名字吧喵～"], description="角色名为空、过长或含控制字符时的回复。")
    invalid_image: List[str] = Field(default_factory=lambda: ["这张图片小花收不进去喵：{原因}"], description="其他图片错误的兜底回复，支持 {原因}。")
    image_too_large: List[str] = Field(default_factory=lambda: ["这张图片有 {实际大小}，超过小花能记住的 {大小限制} 啦，压缩一下再发喵～"], description="图片体积超限回复，支持 {实际大小}、{大小限制}。")
    image_invalid_format: List[str] = Field(default_factory=lambda: ["这张图片是 {格式} 格式，小花只记得住 PNG、JPEG、WebP 和 GIF 喵～"], description="图片格式不支持回复，支持 {格式}。")
    image_corrupt: List[str] = Field(default_factory=lambda: ["图片好像损坏了，小花看不清，请换一张图片喵～"], description="图片损坏或无法解码时的回复。")
    busy: List[str] = Field(default_factory=lambda: ["小花正忙着整理图库呢，稍等一下再叫小花吧喵～"], description="等待处理的请求过多时的回复。")
    error: List[str] = Field(default_factory=lambda: ["小花整理图库时遇到一点问题，稍后再试试好不好喵～"], description="存储、引用查询或发送失败时的回复，具体原因写入日志。")

    @field_validator("*", mode="before")
    @classmethod
    def normalize_variants(cls, value: object) -> List[str]:
        return normalize_reply_variants(value)

    @field_validator(
        "registered",
        "overwritten",
        "not_found",
        "missing_image",
        "invalid_name",
        "busy",
        "error",
    )
    @classmethod
    def validate_role_templates(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @field_validator("view")
    @classmethod
    def validate_view_templates(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values]

    @field_validator("invalid_image")
    @classmethod
    def validate_invalid_image(cls, values: List[str]) -> List[str]:
        return [validate_image_reason_reply(value) for value in values if value.strip()]

    @field_validator("list_reply")
    @classmethod
    def validate_list(cls, values: List[str]) -> List[str]:
        return [validate_list_reply(value) for value in values if value.strip()]

    @field_validator("list_empty_reply")
    @classmethod
    def validate_list_empty(cls, values: List[str]) -> List[str]:
        return [validate_plain_reply(value) for value in values if value.strip()]

    @field_validator("help_title", "help_footer")
    @classmethod
    def validate_help_text(cls, values: List[str]) -> List[str]:
        return [validate_plain_reply(value) for value in values if value.strip()]

    @field_validator("image_too_large")
    @classmethod
    def validate_image_too_large(cls, values: List[str]) -> List[str]:
        return [validate_image_size_reply(value) for value in values if value.strip()]

    @field_validator("image_invalid_format")
    @classmethod
    def validate_image_invalid_format(cls, values: List[str]) -> List[str]:
        return [validate_image_format_reply(value) for value in values if value.strip()]

    @field_validator("image_corrupt")
    @classmethod
    def validate_image_corrupt(cls, values: List[str]) -> List[str]:
        return [validate_reply(value) for value in values if value.strip()]

    @model_validator(mode="after")
    def ensure_all_replies_present(self) -> "ReplyConfig":
        for name in (
            "registered",
            "overwritten",
            "view",
            "not_found",
            "missing_image",
            "invalid_name",
            "invalid_image",
            "image_too_large",
            "image_invalid_format",
            "image_corrupt",
            "busy",
            "error",
            "list_reply",
            "list_empty_reply",
            "help_title",
            "help_footer",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} 至少需要一条回复")
        return self

    @field_validator("at_replies")
    @classmethod
    def validate_at_replies(cls, values: List[str]) -> List[str]:
        allowed = {
            "registered",
            "overwritten",
            "view",
            "not_found",
            "missing_image",
            "invalid_name",
            "invalid_image",
            "image_too_large",
            "image_invalid_format",
            "image_corrupt",
            "busy",
            "error",
            "list_reply",
            "list_empty_reply",
            "help_title",
            "help_footer",
        }
        unknown = [value for value in values if value not in allowed]
        if unknown:
            raise ValueError("未知的角色库回复类型: " + "、".join(unknown))
        return list(dict.fromkeys(values))


class LibraryConfig(PluginConfigBase):
    __ui_label__ = "角色库限制"
    __ui_icon__ = "images"
    __ui_order__ = 3

    max_name_length: int = Field(default=64, ge=1, le=256, description="角色名最多字符数。角色库全局共享，所有群和用户都可查看、登记及覆盖同名角色。")
    max_image_mb: int = Field(default=8, ge=1, le=32, description="角色图片最大 MB；同时受主程序消息大小和图片处理设置限制。")


class LittleFlowerDrawConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
