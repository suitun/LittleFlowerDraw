"""角色库的声明式 WebUI 配置。"""

from typing import List

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

from .role_patterns import validate_reply, validate_trigger_groups


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "基础设置"
    __ui_icon__ = "palette"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用小花绘")
    config_version: str = Field(default="0.3.0", description="配置结构版本")


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

    @model_validator(mode="after")
    def validate_templates(self) -> "KeywordConfig":
        validate_trigger_groups(self.register_templates, self.view_templates)
        return self


class ReplyConfig(PluginConfigBase):
    __ui_label__ = "角色库回复文案"
    __ui_icon__ = "heart"
    __ui_order__ = 2

    registered: str = Field(default="小花记住{角色名}了喵～", max_length=2000, description="首次入库成功回复，支持 {角色名}。")
    overwritten: str = Field(default="{角色名}的新模样，小花也记住了喵～", max_length=2000, description="同名覆盖成功回复。")
    view: str = Field(default="找到{角色名}啦，给你看看喵～", max_length=2000, description="查看图片前的文字，留空只发图片。")
    not_found: str = Field(default="小花还不认识{角色名}呢，先把 TA 介绍给小花吧喵～", max_length=2000, description="没有找到该角色时的回复。")
    missing_image: str = Field(default="想记住{角色名}的话，要附上图片，或引用一条有图的消息哦喵～", max_length=2000, description="登记消息和引用目标都没有可用图片时的回复。")
    invalid_name: str = Field(default="这个名字小花有点记不住，换个短一点的单行名字吧喵～", max_length=2000, description="角色名为空、过长或含控制字符时的回复。")
    invalid_image: str = Field(default="这些图片暂时收不进图库喵，请检查图片格式、数量和大小后再试～", max_length=2000, description="图片损坏、格式不支持或超过限制时的回复。")
    busy: str = Field(default="小花正忙着整理图库呢，稍等一下再叫小花吧喵～", max_length=2000, description="等待处理的请求过多时的回复。")
    error: str = Field(default="小花整理图库时遇到一点问题，稍后再试试好不好喵～", max_length=2000, description="存储、引用查询或发送失败时的回复，具体原因写入日志。")

    @field_validator("*")
    @classmethod
    def validate_templates(cls, value: str) -> str:
        return validate_reply(value)


class LibraryConfig(PluginConfigBase):
    __ui_label__ = "角色库限制"
    __ui_icon__ = "images"
    __ui_order__ = 3

    max_name_length: int = Field(default=64, ge=1, le=256, description="角色名最多字符数。角色库全局共享，所有群和用户都可查看、登记及覆盖同名角色。")
    max_image_mb: int = Field(default=8, ge=1, le=8, description="角色图片最大 MB；同时受主程序消息大小和图片处理设置限制。")
    max_pixels: int = Field(default=40_000_000, ge=1, le=80_000_000, description="角色图片最大像素数（宽乘高）。")


class LittleFlowerDrawConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    keywords: KeywordConfig = Field(default_factory=KeywordConfig)
    replies: ReplyConfig = Field(default_factory=ReplyConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
