"""小花绘：让小花帮你画。"""

from typing import Any, Dict, Tuple

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础设置。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "palette"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用小花绘")
    config_version: str = Field(default="0.1.0", description="配置结构版本")


class LittleFlowerDrawConfig(PluginConfigBase):
    """小花绘的完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


class LittleFlowerDrawPlugin(MaiBotPlugin):
    """群互动绘画插件。"""

    config_model = LittleFlowerDrawConfig

    async def on_load(self) -> None:
        """在运行时注入配置和上下文后处理加载。"""
        self.ctx.logger.info("小花绘已加载")

    async def on_unload(self) -> None:
        """处理插件卸载。"""
        self.ctx.logger.info("小花绘已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """响应插件自身配置的热重载。"""
        if scope == "self":
            self.ctx.logger.info("小花绘配置已更新：%s", version)

    @Command(
        "little_flower_draw_status",
        description="查看小花绘插件状态",
        pattern=r"^/(?:小花绘|小花画图|lfdraw)\s*$",
    )
    async def show_status(self, stream_id: str, **kwargs: Any) -> Tuple[bool, str, int]:
        """向当前聊天发送插件状态，供首次安装时验证。"""
        message = "小花绘 0.1\n让小花帮你画\n当前为开发骨架，群互动绘画功能待开发。"
        sent = await self.ctx.send.text(message, stream_id)
        if not sent:
            return False, "小花绘状态消息发送失败，请检查 MaiBot 日志。", 2
        return True, message, 2


def create_plugin() -> LittleFlowerDrawPlugin:
    """返回由 MaiBot Runner 托管的插件实例。"""
    return LittleFlowerDrawPlugin()
