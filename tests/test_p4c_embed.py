"""Release packaging proof only: synthetic plugins, no jobs or writable ledger."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PLUGIN = """
import importlib, sys
import trade_engine, trade_engine_rs
IMPORTS = 1
def probe(config):
    if config.get("refuse"):
        raise ValueError("fake refusal: café 🚀")
    if config.get("exit"):
        raise SystemExit(7)
    import ctypes
    from ctypes import wintypes
    modules = (wintypes.HMODULE * 1024)()
    needed = wintypes.DWORD()
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                                       wintypes.LPWSTR, wintypes.DWORD]
    process = ctypes.windll.kernel32.GetCurrentProcess()
    assert ctypes.windll.psapi.EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed))
    loaded = []
    for handle in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        buffer = ctypes.create_unicode_buffer(32768)
        assert ctypes.windll.psapi.GetModuleFileNameExW(process, handle, buffer, len(buffer))
        loaded.append(buffer.value)
    lock = None
    if config.get("lock_file"):
        owner, contender = trade_engine_rs.LedgerLock(), trade_engine_rs.LedgerLock()
        lock = [owner.acquire(config["lock_file"], "synthetic"), owner.held,
                contender.acquire(config["lock_file"], "synthetic")]
        owner.release()
        lock.extend([owner.held, contender.acquire(config["lock_file"], "synthetic")])
        contender.release()
    return {"answer": trade_engine_rs.calendar_is_session("2026-10-02"), "lock": lock,
            "imports": IMPORTS, "tag": config.get("tag"), "loaded": loaded,
            "engine": trade_engine.__file__,
            "same": trade_engine_rs is importlib.import_module("trade_engine_rs"),
            "modules": sorted(n for n in sys.modules if n.startswith("trade_engine_rs")),
            "isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
            "bytecode": sys.dont_write_bytecode}
"""
STANDALONE = """
import importlib, json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path[:] = [cfg["python_home"] + "\\\\Lib", cfg["python_home"] + "\\\\DLLs",
               cfg["site_packages"], cfg["engine_source"], *cfg["plugin_paths"]]
try:
    m = importlib.import_module(cfg["plugin_module"])
    result = getattr(m, cfg["plugin_factory"])(cfg["plugin_config"])
    print(json.dumps({"result": result}, ensure_ascii=True))
except BaseException as exc:
    print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=True))
    sys.exit(2)
"""


@pytest.fixture
def package(tmp_path):
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve(), "private Python 3.13 required"
    import trade_engine
    import trade_engine_rs
    assert Path(trade_engine.__file__).resolve().is_relative_to(ROOT / "src")
    assert Path(trade_engine_rs.trade_engine_rs.__file__).resolve().is_relative_to(ROOT / ".venv")
    home = Path(sys.base_prefix)
    plugins = tmp_path / "space café 🚀 plugins"
    plugins.mkdir()
    (plugins / "fake_plugin.py").write_text(PLUGIN, encoding="utf-8")
    config = {
        "mode": "packaging-proof", "python_home": str(home),
        "python_dll": str(BINARY.parent / "python313.dll"), "python_executable": str(PYTHON),
        "site_packages": str(ROOT / ".venv" / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"), "plugin_paths": [str(plugins)],
        "plugin_module": "fake_plugin", "plugin_factory": "probe",
        "plugin_config": {"tag": "synthetic café 🚀"},
    }
    path = tmp_path / "config café 🚀 space.json"
    cwd = tmp_path / "unrelated cwd"
    cwd.mkdir()
    return config, path, cwd, plugins


def run(package, *, standalone=False, env=None):
    config, path, cwd, _ = package
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    command = ([str(PYTHON), "-I", "-S", "-B", "-c", STANDALONE, str(path)]
               if standalone else [str(BINARY), "--proof", "--config", str(path)])
    proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)
    output = proc.stdout if proc.returncode == 0 or standalone else proc.stderr
    assert output.strip(), (proc.returncode, proc.stdout, proc.stderr)
    try:
        result = json.loads(output)
    except ValueError:
        assert False, (proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode, result


def refusal(package, message):
    code, result = run(package)
    assert code == 2, result
    assert result == {"error": {"type": "RuntimeConfigError", "message": message}}


def test_release_builtin_private_paths_and_repeat(package):
    cfg, _, cwd, _ = package
    poison = dict(os.environ, PYTHONHOME=str(cwd), PYTHONPATH=str(cwd),
                  PYTHONUSERBASE=str(cwd))
    code, report = run(package, env=poison)
    assert code == 0, report
    result = report["result"]
    assert result["answer"] is True
    assert result["same"] and result["imports"] == 1
    assert result["modules"] == ["trade_engine_rs"]
    assert result["isolated"] == 1 and result["no_site"] == 1 and result["bytecode"]
    assert report["module_origin"] == "built-in"
    assert report["module_file"] is None
    assert report["module_version"] == "0.1.0"
    assert report["builtin_module_count"] == 1
    assert report["native_executable"] == str(BINARY)
    assert report["python_dll"] == cfg["python_dll"]
    assert report["python_version"].startswith("3.13.")
    assert report["python_home"] == cfg["python_home"]
    assert report["python_executable"] == cfg["python_executable"]
    assert report["python_prefix"] == cfg["python_home"]
    assert report["search_paths"] == [str(Path(cfg["python_home"]) / "Lib"),
                                     str(Path(cfg["python_home"]) / "DLLs"),
                                     cfg["site_packages"], cfg["engine_source"],
                                     *cfg["plugin_paths"]]
    assert report["plugin_file"] == str(Path(cfg["plugin_paths"][0]) / "fake_plugin.py")
    assert report["reinitialize"] == {"type": "RuntimeConfigError",
                                      "message": "interpreter already initialized"}
    assert Path(result["engine"]).resolve().is_relative_to(ROOT / "src")
    assert not any("trade_engine_rs" in p and p.endswith(".pyd") for p in result["loaded"])
    assert any(Path(p) == Path(cfg["python_dll"]) for p in result["loaded"])
    assert not list(package[3].glob("__pycache__"))
    code, old = run(package, standalone=True, env=poison)
    assert code == 0
    for key in ("answer", "imports", "tag", "engine", "same", "isolated", "no_site", "bytecode"):
        assert result[key] == old["result"][key]


@pytest.mark.parametrize("kind", ["missing", "corrupt", "factory", "not_callable", "refusal", "exit", "alias"])
def test_plugin_import_refusals_match_private_python(package, kind):
    cfg, _, _, plugins = package
    if kind == "missing":
        cfg["plugin_module"] = "absent_plugin"
    elif kind == "corrupt":
        (plugins / "fake_plugin.py").write_text("def broken(:\n", encoding="utf-8")
    elif kind == "factory":
        cfg["plugin_factory"] = "absent"
    elif kind == "not_callable":
        cfg["plugin_factory"] = "IMPORTS"
    elif kind == "refusal":
        cfg["plugin_config"]["refuse"] = True
    elif kind == "exit":
        cfg["plugin_config"]["exit"] = True
    else:
        (plugins / "fake_plugin.py").write_text(
            "import importlib\nimportlib.import_module('trade_engine_rs.not_a_module')\n",
            encoding="utf-8")
    code, embedded = run(package)
    old_code, standalone = run(package, standalone=True)
    assert code == old_code == 2
    # Built-in is not a package; the installed extension wrapper is a package.
    if kind == "alias":
        assert embedded["error"] == {"type": "ModuleNotFoundError",
            "message": "No module named 'trade_engine_rs.not_a_module'; 'trade_engine_rs' is not a package"}
        assert standalone["error"]["type"] == "ModuleNotFoundError"
    elif kind == "not_callable":
        assert embedded["error"] == {"type": "TypeError",
                                     "message": "configured plugin factory is not callable"}
        assert standalone["error"] == {"type": "TypeError", "message": "'int' object is not callable"}
    else:
        assert embedded == standalone


@pytest.mark.parametrize(("field", "value", "message"), [
    ("mode", "live", "unsupported mode: live"),
    ("python_home", "relative", "python_home must be an absolute directory"),
    ("python_dll", "relative.dll", "python_dll must be an absolute file"),
    ("python_executable", "python.exe", "python_executable must be an absolute file"),
    ("site_packages", "relative", "site_packages must be an absolute directory"),
    ("engine_source", "relative", "engine_source must be an absolute directory"),
    ("plugin_paths", [], "plugin_paths must not be empty"),
    ("plugin_module", "trade_engine_rs", "plugin_module is reserved"),
    ("plugin_module", "bad.module", "plugin_module must be a plain module identifier"),
    ("plugin_factory", "", "plugin_factory must be a plain identifier"),
    ("plugin_config", [], "plugin_config must be an object"),
])
def test_config_refusals(package, field, value, message):
    package[0][field] = value
    refusal(package, message)


@pytest.mark.parametrize("field", ["python_home", "python_dll", "python_executable",
                                  "site_packages", "engine_source"])
def test_missing_paths_refuse(package, field):
    cfg, path, _, _ = package
    cfg[field] = str(path.parent / "missing")
    noun = "file" if field in ("python_dll", "python_executable") else "directory"
    refusal(package, f"{field} must be an absolute {noun}")


def test_wrong_home_dll_and_venv(package):
    cfg, path, _, _ = package
    original = dict(cfg)
    home = path.parent / "wrong home"
    home.mkdir()
    cfg["python_home"] = str(home)
    refusal(package, "python_home lacks Python 3.13 standard library")
    cfg.update(original)
    dll = home / "python313.dll"
    dll.write_bytes(b"corrupt")
    cfg["python_dll"] = str(dll)
    refusal(package, "python_dll must be bundled beside te.exe")
    cfg.update(original)
    cfg["site_packages"] = str(home)
    refusal(package, "site_packages must belong to the configured private venv")
    cfg.update(original)
    cfg["python_executable"] = str(Path(sys.base_prefix) / "python.exe")
    refusal(package, "python_executable must belong to a private venv")


def test_plugin_path_and_source_provenance(package):
    cfg, path, _, _ = package
    cfg["plugin_paths"] = [str(path.parent / "missing")]
    refusal(package, "plugin_path must be an absolute directory")
    cfg["plugin_paths"] = [str(ROOT / "src")]
    code, report = run(package)
    assert code == 2 and report["error"]["type"] == "ModuleNotFoundError"
    cfg["engine_source"] = str(path.parent)
    refusal(package, "engine_source lacks trade_engine")


def test_unconfigured_plugin_cannot_use_stdlib_fallback(package):
    package[0]["plugin_module"] = "json"
    code, report = run(package)
    assert code == 2
    assert report["error"] == {"type": "RuntimeError",
                              "message": "plugin source is outside configured plugin_paths"}


def test_unconfigured_plugin_is_refused_before_execution(package):
    cfg, path, _, plugins = package
    (plugins / "fake_plugin.py").unlink()
    source = path.parent / "unconfigured source"
    (source / "trade_engine").mkdir(parents=True)
    (source / "trade_engine" / "__init__.py").write_text("")
    marker = source / "executed"
    (source / "fake_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unexpected')\n")
    cfg["engine_source"] = str(source)
    code, report = run(package)
    assert code == 2
    assert report["error"] == {"type": "RuntimeError",
                              "message": "plugin source is outside configured plugin_paths"}
    assert not marker.exists()


def test_fake_venv_wrong_version_and_home(package):
    cfg, path, _, _ = package
    prefix = path.parent / "fake private venv"
    (prefix / "Scripts").mkdir(parents=True)
    (prefix / "Lib" / "site-packages").mkdir(parents=True)
    shutil.copy2(PYTHON, prefix / "Scripts" / "python.exe")
    cfg["python_executable"] = str(prefix / "Scripts" / "python.exe")
    cfg["site_packages"] = str(prefix / "Lib" / "site-packages")
    refusal(package, "python_executable must belong to a private venv")
    for home, version in ((cfg["python_home"], "3.14.0"), (str(prefix), "3.13.15")):
        (prefix / "pyvenv.cfg").write_text(f"home = {home}\nversion = {version}\n")
        refusal(package, "private venv must use configured Python 3.13 home")


def test_private_native_bundle_independent_of_path_and_missing_dll(package):
    cfg, path, cwd, _ = package
    # Never modify the real runtime bundle or an installed Python DLL.
    bundle = path.parent / "space native 🚀 bundle"
    bundle.mkdir()
    exe = bundle / "te.exe"
    dll = bundle / "python313.dll"
    shutil.copy2(BINARY, exe)
    shutil.copy2(BINARY.parent / "python313.dll", dll)
    cfg["python_dll"] = str(dll)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
               PYTHONHOME=str(cwd), PYTHONPATH=str(cwd))
    command = [str(exe), "--proof", "--config", str(path)]
    proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["module_origin"] == "built-in"
    dll.unlink()
    import ctypes
    # Inherited error mode suppresses OS loader dialogs for synthetic bad bundles.
    old = ctypes.windll.kernel32.SetErrorMode(0x1 | 0x2 | 0x8000)
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=30)
        assert proc.returncode & 0xFFFFFFFF == 0xC0000135, proc.returncode
        dll.write_bytes(b"not a PE DLL")
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=30)
        assert proc.returncode & 0xFFFFFFFF == 0xC000012F, proc.returncode
    finally:
        ctypes.windll.kernel32.SetErrorMode(old)


def test_unicode_home_source_and_site_packages(package):
    cfg, path, _, plugins = package
    home = path.parent / "home café 🚀 space"
    home.mkdir()
    original = Path(cfg["python_home"])
    shutil.copytree(original / "Lib", home / "Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test"))
    shutil.copytree(original / "DLLs", home / "DLLs")
    shutil.copy2(original / "python313.dll", home / "python313.dll")
    prefix = path.parent / "venv café 🚀 space"
    (prefix / "Scripts").mkdir(parents=True)
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    shutil.copy2(PYTHON, prefix / "Scripts" / "python.exe")
    (prefix / "pyvenv.cfg").write_text(f"home = {home}\nversion = {sys.version.split()[0]}\n",
                                     encoding="utf-8")
    source = path.parent / "source café 🚀 space"
    shutil.copytree(ROOT / "src" / "trade_engine", source / "trade_engine",
                    ignore=shutil.ignore_patterns("__pycache__"))
    cfg.update(python_home=str(home), python_executable=str(prefix / "Scripts" / "python.exe"),
               site_packages=str(site), engine_source=str(source))
    code, report = run(package)
    assert code == 0, report
    assert report["python_home"] == str(home)
    assert report["python_executable"] == cfg["python_executable"]
    assert report["result"]["engine"] == str(source / "trade_engine" / "__init__.py")
    assert report["plugin_file"] == str(plugins / "fake_plugin.py")
    (home / "python313.dll").write_bytes(b"wrong DLL")
    refusal(package, "bundled Python DLL differs from configured home")


def test_missing_corrupt_config_and_cli(package):
    _, path, cwd, _ = package
    for contents in (None, "{broken", '{"mode":"packaging-proof","unexpected":true}'):
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        proc = subprocess.run([str(BINARY), "--proof", "--config", str(path)], cwd=cwd,
                              capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 2
        error = json.loads(proc.stderr)["error"]
        assert error["type"] == "RuntimeConfigError"
        assert error["message"].startswith("config ")
    for args in ([], ["serve"], ["--proof", "--config", "relative.json"]):
        proc = subprocess.run([str(BINARY), *args], cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 2
        assert json.loads(proc.stderr)["error"]["type"] == "RuntimeConfigError"


def test_embedded_does_not_need_installed_extension(package):
    import trade_engine_rs
    extension = Path(trade_engine_rs.trade_engine_rs.__file__)
    # A fresh child sees no .pyd; this process already loaded its ordinary extension.
    moved = extension.with_suffix(".pyd.disabled")
    try:
        extension.rename(moved)
        code, report = run(package)
        assert code == 0 and report["module_origin"] == "built-in"
        code, report = run(package, standalone=True)
        assert code == 2 and report["error"]["type"] == "ModuleNotFoundError"
        extension.write_bytes(b"not a native extension")
        code, report = run(package)
        assert code == 0 and report["module_origin"] == "built-in"
        code, report = run(package, standalone=True)
        assert code == 2 and report["error"]["type"] == "ImportError"
    finally:
        if moved.exists():
            extension.unlink(missing_ok=True)
            moved.rename(extension)


def test_builtin_lock_on_synthetic_sidecar_without_ledger(package):
    cfg, _, _, plugins = package
    cfg["plugin_config"]["lock_file"] = str(plugins / "synthetic ledger.lock")
    code, report = run(package)
    old_code, old = run(package, standalone=True)
    assert code == old_code == 0
    assert report["result"]["lock"] == old["result"]["lock"] == [True, True, False, False, True]
    assert not list(plugins.glob("*.db"))
