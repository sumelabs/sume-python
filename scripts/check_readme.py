#!/usr/bin/env python3
"""Type-check every ```python block in README.md with mypy, so the examples cannot rot.

A block may fail only when the README shows its error: the ```text block right
after it must contain the message mypy prints. Needs mypy and the package installed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Names a block may use from the text around it.
STUBS = {
    "sume": "import sume",
    "asyncio": "import asyncio",
    "Any": "from typing import Any",
    "request": "request: Any = None",
    "app": "app: Any = None",
    "signing_secret": 'signing_secret = ""',
    "png_bytes": 'png_bytes = b""',
    "job": 'job = sume.job("job_example")',
}
NEEDS = {"request": ["Any"], "app": ["Any"], "job": ["sume"]}


def prelude(code: str) -> list[str]:
    wanted: list[str] = []
    for name in STUBS:
        used = re.search(rf"\b{name}\b", code)
        defined = re.search(
            rf"^\s*(import {name}\b|from \S+ import .*\b{name}\b|{name}\s*[:=]|def {name}\b)", code, re.M
        )
        if used and not defined:
            for need in [*NEEDS.get(name, []), name]:
                if need not in wanted:
                    wanted.append(need)
    order = list(STUBS)
    return [STUBS[name] for name in sorted(wanted, key=order.index)]


def main() -> int:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```(\w+)\n(.*?)```", readme, re.S)
    shown: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        files: list[str] = []
        for i, (lang, code) in enumerate(blocks):
            if lang != "python":
                continue
            name = f"block{len(files):02d}.py"
            body = code
            # A snippet with a top-level await runs inside an async function.
            if re.search(r"^(await |async with |.*= await )", code, re.M):
                body = "async def _example() -> None:\n" + textwrap.indent(code, "    ")
            Path(tmp, name).write_text("\n".join(prelude(code)) + "\n" + body, encoding="utf-8")
            files.append(name)
            if i + 1 < len(blocks) and blocks[i + 1][0] == "text":
                shown[name] = blocks[i + 1][1]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--check-untyped-defs",
                "--ignore-missing-imports",
                "--no-error-summary",
                *files,
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
    unexpected = []
    for line in run.stdout.splitlines():
        match = re.match(r"^(block\d+\.py):\d+: error: (.*)$", line)
        if match and match.group(2) not in shown.get(match.group(1), ""):
            unexpected.append(line)
    if unexpected or run.returncode not in (0, 1):
        print("README.md examples do not type-check:\n" + "\n".join(unexpected or [run.stdout, run.stderr]))
        return 1
    print(f"README.md: {len(files)} Python examples type-check; shown errors match mypy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
