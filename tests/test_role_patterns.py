"""真实触发模板、配置校验与文案格式化的边界测试。"""

from importlib import import_module
from pathlib import Path
from types import ModuleType

import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "little_flower_draw_pattern_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
patterns = import_module(f"{PACKAGE}.role_patterns")
settings = import_module(f"{PACKAGE}.settings")


class RolePatternTests(unittest.TestCase):
    def test_default_templates_accept_optional_space_and_keep_name(self):
        config = settings.KeywordConfig()
        for text in ("小花小花这是 角色1", "小花小花这是角色1", " 小花小花这是 角色1 "):
            request = patterns.match_request(text, config.register_templates, config.view_templates)
            self.assertEqual((request.action, request.role_name), ("register", "角色1"))
        request = patterns.match_request("小花小花我要看 角色1", config.register_templates, config.view_templates)
        self.assertEqual((request.action, request.role_name), ("view", "角色1"))
        self.assertIsNone(patterns.match_request("有人说小花小花这是角色1", config.register_templates, config.view_templates))

    def test_aliases_are_literal_and_support_suffix(self):
        register = ["记.[{角色名}]", "记 {角色名}", "记住 {角色名}"]
        view = ["看看 {角色名}"]
        request = patterns.match_request("记.[小花]", register, view)
        self.assertEqual(request.role_name, "小花")
        self.assertIsNone(patterns.match_request("记X[小花]", [register[0]], view))
        self.assertEqual(patterns.match_request("记住 小花", register, view).role_name, "小花")

    def test_names_are_single_line_and_bounded(self):
        self.assertFalse(patterns.valid_role_name("", 64))
        self.assertFalse(patterns.valid_role_name("小花\n坏名字", 64))
        self.assertFalse(patterns.valid_role_name("花" * 65, 64))
        self.assertTrue(patterns.valid_role_name("../小花", 64))  # 名称是数据库键，不是文件路径。

    def test_invalid_trigger_configuration_is_rejected(self):
        for value in ([], ["没有占位符"], ["{角色名}"], ["登记{角色名}{角色名}"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.KeywordConfig(register_templates=value)
        with self.assertRaises(ValueError):
            settings.KeywordConfig(register_templates=["登记 {角色名}"], view_templates=["登记{角色名}"])

    def test_reply_placeholders_are_validated_and_names_are_not_evaluated(self):
        text = patterns.render_reply("记住{角色名}啦 {{喵}}", "{别的变量}")
        self.assertEqual(text, "记住{别的变量}啦 {喵}")
        for text in ("{不存在}", "{角色名.__class__}", "{角色名[0]}", "{角色名!r}", "{图片数量}", "{"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                settings.ReplyConfig(registered=text)

    def test_plugin_config_only_keeps_core_section(self):
        from maibot_sdk.config import generate_plugin_config_schema

        config = settings.LittleFlowerDrawConfig(plugin={"enabled": True, "config_version": "0.1.0"})
        self.assertTrue(config.plugin.enabled)
        self.assertEqual(settings.KeywordConfig().register_templates, ["小花小花这是 {角色名}"])
        schema = generate_plugin_config_schema(settings.LittleFlowerDrawConfig)
        self.assertIn("plugin", schema["sections"])
        self.assertNotIn("keywords", schema["sections"])
        self.assertNotIn("replies", schema["sections"])
        first, second = settings.KeywordConfig(), settings.KeywordConfig()
        first.register_templates.append("记住{角色名}")
        self.assertNotIn("记住{角色名}", second.register_templates)


if __name__ == "__main__":
    unittest.main()
