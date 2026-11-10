"""聊天指令注册表：新增指令时只需要在这里登记一次。"""

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class CommandSpec:
    """一个可配置权限的聊天指令。"""

    command_id: str
    label: str
    description: str


COMMAND_SPECS: Tuple[CommandSpec, ...] = (
    CommandSpec("登记角色", "登记角色", "用附图或引用图片登记、覆盖角色"),
    CommandSpec("查看角色", "查看角色", "查看角色当前保存的图片"),
)

ACTION_COMMANDS: Dict[str, str] = {
    "register": "登记角色",
    "view": "查看角色",
}

COMMAND_IDS: Tuple[str, ...] = tuple(spec.command_id for spec in COMMAND_SPECS)


def command_label(command_id: str) -> str:
    for spec in COMMAND_SPECS:
        if spec.command_id == command_id:
            return spec.label
    raise ValueError(f"未登记的指令: {command_id}")
