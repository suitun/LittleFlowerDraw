"""聊天指令的白名单、权限与次数守卫。"""

from dataclasses import dataclass
from typing import Optional

from .command_registry import DRAWING_COMMAND_IDS, command_label
from .command_usage import CommandUsageStore, format_reset_hint
from .role_patterns import render_guard_reply
from .webui_settings import CommandPolicy, WebUISettings, WebUISettingsStore


DRAWING_QUOTA_KEY = "画画"


@dataclass(frozen=True)
class AccessDecision:
    """白名单或权限检查结果。"""

    allowed: bool
    reply: str = ""
    reply_key: str = ""


class CommandGuard:
    """所有聊天指令共用的访问检查；WebUI 不经过这里。"""

    def __init__(self, usage_store: CommandUsageStore, settings_store: WebUISettingsStore) -> None:
        self._usage_store = usage_store
        self._settings_store = settings_store

    @staticmethod
    def is_admin(settings: WebUISettings, user_id: str) -> bool:
        return user_id == settings.access.admin_qq

    @staticmethod
    def policy_for(settings: WebUISettings, command_id: str) -> CommandPolicy:
        for policy in settings.commands.policies:
            if policy.command == command_id:
                return policy
        raise ValueError(f"指令未配置权限: {command_id}")

    @staticmethod
    def quota_scope(command_id: str) -> str:
        return DRAWING_QUOTA_KEY if command_id in DRAWING_COMMAND_IDS else command_id

    @staticmethod
    def quota_reset_days(settings: WebUISettings, command_id: str) -> int:
        if command_id not in DRAWING_COMMAND_IDS:
            return CommandGuard.policy_for(settings, command_id).reset_days
        return max(CommandGuard.policy_for(settings, item).reset_days for item in DRAWING_COMMAND_IDS)

    @staticmethod
    def quota_limit(settings: WebUISettings, command_id: str, user_id: str) -> int:
        """四种绘图指令共享次数；其它指令仍按自己的配置计算。"""
        if command_id in DRAWING_COMMAND_IDS:
            policies = [CommandGuard.policy_for(settings, item) for item in DRAWING_COMMAND_IDS]
            limits = [max(0, policy.limit) for policy in policies]
            if not any(limits):
                return 0
            base = sum(limits)
            extra = sum(
                quota.extra_count
                for quota in settings.user_quotas
                if quota.user_id == user_id and quota.command in DRAWING_COMMAND_IDS
            )
            return base + extra
        policy = CommandGuard.policy_for(settings, command_id)
        if policy.limit <= 0:
            return 0
        extra = sum(
            quota.extra_count
            for quota in settings.user_quotas
            if quota.user_id == user_id and quota.command == command_id
        )
        return policy.limit + extra

    def check_access(self, command_id: str, user_id: str, group_id: str) -> AccessDecision:
        """检查白名单和权限；管理员始终放行。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return AccessDecision(True)
        if settings.access.whitelist_enabled:
            allowed_ids = settings.access.group_whitelist if group_id else settings.access.private_whitelist
            target_id = group_id or user_id
            if target_id not in allowed_ids:
                return AccessDecision(
                    False,
                    render_guard_reply(settings.guard_replies.whitelist_denied, command_label(command_id), ""),
                    "whitelist_denied",
                )
        policy = self.policy_for(settings, command_id)
        if policy.permission == "仅管理员":
            return AccessDecision(
                False,
                render_guard_reply(settings.guard_replies.admin_only, command_label(command_id), ""),
                "admin_only",
            )
        return AccessDecision(True)

    def check_quota(self, command_id: str, user_id: str) -> Optional[str]:
        """检查次数；四种绘图指令共用同一个次数池。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return None
        scope = self.quota_scope(command_id)
        limit = self.quota_limit(settings, command_id, user_id)
        status = self._usage_store.check(user_id, scope, limit, self.quota_reset_days(settings, command_id))
        if status.allowed:
            return None
        return render_guard_reply(
            settings.guard_replies.quota_exceeded,
            "画画" if command_id in DRAWING_COMMAND_IDS else command_label(command_id),
            format_reset_hint(status.reset_at),
        )

    def remaining_quota(self, command_id: str, user_id: str) -> int:
        """返回当前可用次数；-1 表示不限次数。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return -1
        scope = self.quota_scope(command_id)
        limit = self.quota_limit(settings, command_id, user_id)
        if limit <= 0:
            return -1
        status = self._usage_store.check(user_id, scope, limit, self.quota_reset_days(settings, command_id))
        return max(0, limit - status.used)

    def record_success(self, command_id: str, user_id: str) -> int:
        """只在任务成功入队后记一次，返回计数窗口起点。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return 0
        scope = self.quota_scope(command_id)
        limit = self.quota_limit(settings, command_id, user_id)
        return self._usage_store.record_success(user_id, scope, limit, self.quota_reset_days(settings, command_id))

    def refund_task(self, task_id: int, command_id: str, user_id: str, window_start: int) -> bool:
        """任务失败或被取消时按任务 ID 退还一次。"""
        return self._usage_store.refund_once(task_id, user_id, self.quota_scope(command_id), window_start)
