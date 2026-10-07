"""Build native release + private extension; assemble the CPython DLL bundle."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def build(*, native_only=False):
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() != python.resolve() or sys.version_info[:2] != (3, 13):
        raise RuntimeError("build requires this worktree's private Python 3.13")
    env = dict(os.environ, PYO3_PYTHON=str(python),
               CARGO_TARGET_DIR=str(ROOT / "crates" / "target"),
               PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"])
    subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "crates" / "Cargo.toml"),
                    "-p", "te_runtime", "--release"], cwd=ROOT, env=env, check=True)
    release = ROOT / "crates" / "target" / "release"
    shutil.copy2(Path(sys.base_prefix) / "python313.dll", release / "python313.dll")
    if not (release / "te.exe").is_file():
        raise RuntimeError("missing mandatory release executable")
    if native_only:
        return
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall",
                    "-q", str(ROOT / "crates" / "te_py")], cwd=ROOT, env=env, check=True)
    code = ("import pathlib,trade_engine,trade_engine_rs,sys;"
            "print(sys.version);print(trade_engine.__file__);"
            "p=pathlib.Path(trade_engine_rs.trade_engine_rs.__file__);print(p);"
            f"assert p.resolve().is_relative_to(pathlib.Path({str(ROOT / '.venv')!r}));"
            f"assert pathlib.Path(trade_engine.__file__).resolve().is_relative_to(pathlib.Path({str(ROOT / 'src')!r}))")
    subprocess.run([str(python), "-B", "-c", code], cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--native-only"]):
        raise SystemExit("usage: build_p4c_t3.py [--native-only]")
    build(native_only=bool(sys.argv[1:]))
