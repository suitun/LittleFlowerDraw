"""聊天指令的白名单、权限与次数守卫。"""

from dataclasses import dataclass
from typing import Optional

from .command_registry import command_label
from .command_usage import CommandUsageStore, format_reset_hint
from .role_patterns import render_guard_reply
from .webui_settings import CommandPolicy, WebUISettings, WebUISettingsStore


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
    def quota_limit(settings: WebUISettings, command_id: str, user_id: str) -> int:
        """基础次数加上该用户在这一指令上的额外次数；0 表示不限。"""
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
        """检查次数；返回 None 表示可以执行，否则返回可爱提示。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return None
        policy = self.policy_for(settings, command_id)
        limit = self.quota_limit(settings, command_id, user_id)
        status = self._usage_store.check(user_id, command_id, limit, policy.reset_days)
        if status.allowed:
            return None
        return render_guard_reply(
            settings.guard_replies.quota_exceeded,
            command_label(command_id),
            format_reset_hint(status.reset_at),
        )

    def record_success(self, command_id: str, user_id: str) -> int:
        """只在任务成功入队后记一次，返回计数窗口起点。"""
        settings = self._settings_store.current()
        if self.is_admin(settings, user_id):
            return 0
        policy = self.policy_for(settings, command_id)
        limit = self.quota_limit(settings, command_id, user_id)
        return self._usage_store.record_success(user_id, command_id, limit, policy.reset_days)

    def refund_task(self, task_id: int, command_id: str, user_id: str, window_start: int) -> bool:
        """任务失败或被取消时按任务 ID 退还一次。"""
        return self._usage_store.refund_once(task_id, user_id, command_id, window_start)
