from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_main_prefers_current_checkout_package() -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = (
        "from pathlib import Path; "
        "import main, wechat_decrypt_tool; "
        "print(Path(wechat_decrypt_tool.__file__).resolve())"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == (
        ROOT / "src" / "wechat_decrypt_tool" / "__init__.py"
    ).resolve()
