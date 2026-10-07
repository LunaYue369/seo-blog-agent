"""pytest 根配置 —— 唯一职责：把仓库根目录放进 sys.path。

为什么需要这个文件：pytest 在 `tests/` 里没有 `__init__.py` 时，插入 sys.path 的是
**测试文件所在目录**（`tests/`），不是仓库根目录。于是 `import config` / `import eval`
会 ModuleNotFoundError。把 conftest.py 放在根上，pytest 会把根目录也加进 sys.path，
测试就能像正常运行时那样 import 业务模块。

⚠️ 这里不放任何 fixture、不做任何 API 调用、不读 .env —— 保证 `pytest` 在 CI 里
（没有 key、没有网络）也能收集并跑完 tests/。这是 golden set + CI 能落地的前提。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
