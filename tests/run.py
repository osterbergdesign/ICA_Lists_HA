"""Tiny test runner (no pytest needed): python3 tests/run.py [filter]

Runs every test_* function in tests/test_*.py; async tests get their own loop.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
import sys
import traceback

HERE = Path(__file__).parent
ROOT = HERE.parent
COMPONENT = ROOT / "custom_components" / "ica_lists"


def load_module(name: str, path: Path):
    """Import a single file as a module (skips the package __init__)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    sys.path.insert(0, str(HERE))
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = 0
    for path in sorted(HERE.glob("test_*.py")):
        module = load_module(path.stem, path)
        for name, func in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_") or func.__module__ != module.__name__:
                continue
            label = f"{path.stem}.{name}"
            if wanted and wanted not in label:
                continue
            try:
                if inspect.iscoroutinefunction(func):
                    asyncio.run(func())
                else:
                    func()
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {label}")
                traceback.print_exc()
            else:
                passed += 1
                print(f"ok   {label}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
