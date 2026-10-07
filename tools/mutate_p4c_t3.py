"""Packaging-control mutants: rebuild both artifacts, assertion kills only."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
           PYO3_PYTHON=str(PY), CARGO_TARGET_DIR=str(ROOT / "crates" / "target"),
           TMPDIR=str(ROOT / ".ci-local" / "temp"), TEMP=str(ROOT / ".ci-local" / "temp"),
           TMP=str(ROOT / ".ci-local" / "temp"))
ENV.pop("PYTEST_ADDOPTS", None)
RUNNER = """
import pytest, sys
class Reporter:
    def pytest_runtest_makereport(self, item, call):
        if call.when == 'call' and call.excinfo is not None and issubclass(call.excinfo.type, AssertionError):
            print('P4C_ASSERTION_KILL=' + item.nodeid, flush=True)
raise SystemExit(pytest.main(sys.argv[1:], plugins=[Reporter()]))
"""
CONFIG = "crates/te_runtime/src/config.rs"
BOOT = "crates/te_runtime/src/python.rs"
MODULE = "crates/te_py/src/lib.rs"
MUTANTS = (
    ("live-mode-allowed", CONFIG, 'if self.mode != "packaging-proof" {', 'if false {'),
    ("home-stdlib-check-lost", CONFIG,
     'if !self\n            .python_home\n            .join("Lib")\n'
     '            .join("encodings")\n            .join("__init__.py")\n            .is_file()\n'
     '            || !self.python_home.join("DLLs").is_dir()\n',
     'if false\n'),
    ("dll-path-check-lost", CONFIG, 'if self.python_dll != executable.parent().unwrap().join("python313.dll") {',
     'if false {'),
    ("site-packages-check-lost", CONFIG, 'if self.site_packages != prefix.join("Lib").join("site-packages") {',
     'if false {'),
    ("venv-version-check-lost", CONFIG, '|| !version.is_some_and(|v| v.starts_with("3.13."))',
     '|| !version.is_some_and(|_| true)'),
    ("reserved-module-allowed", CONFIG, 'if self.plugin_module == "trade_engine_rs" || self.plugin_module == "trade_engine" {',
     'if false {'),
    ("config-object-check-lost", CONFIG, 'if !self.plugin_config.is_object() {', 'if false {'),
    ("environment-isolation-lost", BOOT, 'ffi::PyConfig_InitIsolatedConfig(raw.as_mut_ptr());',
     'ffi::PyConfig_InitPythonConfig(raw.as_mut_ptr());'),
    ("site-startup-enabled", BOOT, 'raw.site_import = 0;', 'raw.site_import = 1;'),
    ("bytecode-writes-enabled", BOOT, 'raw.write_bytecode = 0;', 'raw.write_bytecode = 1;'),
    ("plugin-search-path-lost", CONFIG, 'paths.extend(self.plugin_paths.clone());',
     'paths.extend(Vec::<PathBuf>::new());'),
    ("builtin-registration-lost", MODULE, 'pyo3::append_to_inittab!(trade_engine_rs);', 'let _ = ();'),
    ("builtin-version-wrong", MODULE, 'm.add("__version__", env!("CARGO_PKG_VERSION"))?;',
     'm.add("__version__", "0.0.0")?;'),
    ("reinitialization-guard-lost", BOOT, 'if ffi::Py_IsInitialized() != 0 {', 'if false {'),
    ("plugin-provenance-guard-lost", "crates/te_runtime/src/proof.py",
     'if spec is not None and (spec.origin is None or Path(spec.origin).resolve() not in allowed_files):',
     'if False:'),
)


def build():
    proc = subprocess.run([str(PY), "-B", str(ROOT / "tools" / "build_p4c_t3.py")],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode:
        raise RuntimeError("artifact rebuild failed\n" + proc.stdout[-2000:] + proc.stderr[-5000:])


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER,
                           str(ROOT / "tests" / "test_p4c_embed.py"),
                           "-x", "-q", "--tb=short", "-p", "no:cacheprovider"],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def main():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert sys.version_info[:2] == (3, 13)
    (ROOT / ".ci-local" / "temp").mkdir(parents=True, exist_ok=True)
    selected = set(sys.argv[1:])
    assert selected <= {name for name, *_ in MUTANTS}
    originals = {ROOT / file: (ROOT / file).read_bytes() for _, file, _, _ in MUTANTS}
    kills, invalid = [], []
    baseline_green = restored_green = False
    try:
        build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode}\n{baseline.stdout[-2000:]}", flush=True)
        if baseline_green:
            for name, file, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                source = ROOT / file
                text = originals[source].decode("utf-8")
                if "\r\n" in text:
                    anchor = anchor.replace("\n", "\r\n")
                    replacement = replacement.replace("\n", "\r\n")
                assert text.count(anchor) == 1, (name, text.count(anchor))
                start = time.perf_counter()
                try:
                    source.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    build()
                    proc = tests()
                    failure = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    if (proc.returncode == 1 and failure and "P4C_ASSERTION_KILL=" in proc.stdout
                            and "ERROR collecting" not in proc.stdout):
                        kills.append((name, failure[0]))
                        print(f"KILLED {name} {time.perf_counter() - start:.3f}s {failure[0]}", flush=True)
                    else:
                        invalid.append(name)
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-4000:]}", flush=True)
                except RuntimeError as exc:
                    invalid.append(name)
                    print(f"INVALID {name}: {exc}", flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source, content in originals.items():
            source.write_bytes(content)
        build()
        restored = tests()
        restored_green = restored.returncode == 0
        print(f"restored exit={restored.returncode}\n{restored.stdout[-2000:]}", flush=True)
        for source, content in originals.items():
            assert source.read_bytes() == content
            print(f"restored SHA256 {source.relative_to(ROOT)} {hashlib.sha256(content).hexdigest()}", flush=True)
    print(f"killed={len(kills)} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())
