"""发布代码与本机运行资料分离；显式配置错误时禁止回退到空目录。"""

import os
from pathlib import Path


def get_runtime_root() -> Path:
    configured = os.environ.get("AFTERSALES_RUNTIME_ROOT")
    if configured is None:
        return Path(__file__).resolve().parents[3]
    root = Path(configured)
    if not configured.strip() or not root.is_absolute():
        raise ValueError("运行资料目录必须是明确的绝对路径")
    root = root.resolve(strict=True)
    if not (root / ".runtime").is_dir():
        raise ValueError("运行资料目录缺少 .runtime，禁止使用空目录")
    return root
