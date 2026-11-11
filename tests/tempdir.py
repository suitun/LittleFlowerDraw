"""把单元测试临时目录集中到仓库的 temp/tests 下，避免污染插件根目录。"""

from pathlib import Path
from tempfile import TemporaryDirectory as _TemporaryDirectory
from typing import Any


_TEMP_ROOT = Path(__file__).resolve().parent.parent / "temp" / "tests"
_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def TemporaryDirectory(*args: Any, **kwargs: Any) -> Any:
    kwargs["dir"] = _TEMP_ROOT
    return _TemporaryDirectory(*args, **kwargs)
