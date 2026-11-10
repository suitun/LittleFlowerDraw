import unittest
from importlib import import_module
from pathlib import Path
from types import ModuleType

import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "little_flower_draw_drawing_pattern_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
drawing_patterns = import_module(f"{PACKAGE}.drawing_patterns")
match_drawing_request = drawing_patterns.match_drawing_request


REPLACE = ["变{角色名} {额外提示词}", "{角色名}穿 {额外提示词}"]
TOGETHER = ["{角色名1}{角色名2}一起穿 {额外提示词}"]
GROUP = ["多人穿{角色名列表} {额外提示词}"]


def match(text):
    return match_drawing_request(text, REPLACE, TOGETHER, GROUP)


class DrawingPatternTests(unittest.TestCase):
    def test_replace_templates_parse_role_and_optional_extra(self) -> None:
        for text, role, extra in (
            ("变角色A 红色外套", "角色A", "红色外套"),
            ("变角色A", "角色A", ""),
            ("角色A穿 制服", "角色A", "制服"),
            ("角色A穿", "角色A", ""),
        ):
            with self.subTest(text=text):
                result = match(text)
                self.assertIsNotNone(result)
                self.assertEqual((result.kind, result.role_names, result.extra_prompt), ("replace", [role], extra))

    def test_together_template_keeps_combined_role_text(self) -> None:
        result = match("角色A角色B一起穿 同框")
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "together")
        self.assertEqual(result.roles_text, "角色A角色B")
        self.assertEqual(result.extra_prompt, "同框")

    def test_group_template_parses_at_separated_roles(self) -> None:
        result = match("多人穿@角色A@角色B@角色C 游乐园")
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "group")
        self.assertEqual(result.role_names, ["角色A", "角色B", "角色C"])
        self.assertEqual(result.extra_prompt, "游乐园")

    def test_extra_prompt_is_optional(self) -> None:
        self.assertEqual(match("多人穿@角色A@角色B@角色C").extra_prompt, "")
        self.assertEqual(match("角色A角色B一起穿").extra_prompt, "")

    def test_longer_template_wins(self) -> None:
        result = match_drawing_request("变角色A 红色外套", ["变{角色名} {额外提示词}", "变{角色名}"], [], [])
        self.assertIsNotNone(result)
        self.assertEqual(result.role_names, ["角色A"])
        self.assertEqual(result.extra_prompt, "红色外套")

    def test_invalid_templates_are_rejected(self) -> None:
        cases = [
            (["变{角色名1}"], "together", "各包含"),
            (["变{角色名}{角色名}"], "replace", "只能"),
            (["变{角色名} {额外提示词}尾巴"], "replace", "末尾"),
            (["变{未知}"], "replace", "未知"),
            ([], "replace", "至少"),
        ]
        for templates, kind, message in cases:
            with self.subTest(templates=templates):
                with self.assertRaisesRegex(ValueError, message):
                    drawing_patterns.validate_templates(templates, kind)

    def test_free_drawing_captures_prompt_and_allows_empty(self) -> None:
        result = match_drawing_request("小花画 星空", [], [], [], ["小花画{提示词}"])
        self.assertIsNotNone(result)
        self.assertEqual((result.kind, result.free_prompt), ("free", "星空"))
        empty = match_drawing_request("小花画", [], [], [], ["小花画{提示词}"])
        self.assertIsNotNone(empty)
        self.assertEqual(empty.free_prompt, "")

    def test_optional_at_prefix_requires_a_bot_mention(self) -> None:
        templates = ["@变{角色名} {额外提示词}"]
        self.assertIsNone(match_drawing_request("变角色A 外套", templates, [], []))
        result = match_drawing_request("变角色A 外套", templates, [], [], mentioned_bot=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.role_names, ["角色A"])

    def test_plain_text_does_not_match(self) -> None:
        self.assertIsNone(match("今天吃什么"))
        self.assertIsNone(match("小花小花这是 角色A"))


if __name__ == "__main__":
    unittest.main()
