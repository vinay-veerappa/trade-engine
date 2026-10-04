"""Embedded packaging plumbing only; deliberately no trading or provider jobs."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys

config = json.loads(config_json)
native = importlib.import_module("trade_engine_rs")
if native.__spec__.origin != "built-in" or getattr(native, "__file__", None) is not None:
    raise RuntimeError("trade_engine_rs must be the built-in module")
if native is not importlib.import_module("trade_engine_rs"):
    raise RuntimeError("mandatory module import identity changed")
if native.__version__ != "0.1.0":
    raise RuntimeError("trade_engine_rs version mismatch")
if sys.builtin_module_names.count("trade_engine_rs") != 1:
    raise RuntimeError("mandatory built-in must be registered exactly once")
engine = importlib.import_module("trade_engine")
expected_engine = Path(config["engine_source"]) / "trade_engine" / "__init__.py"
if Path(engine.__file__).resolve() != expected_engine.resolve():
    raise RuntimeError("trade_engine source provenance mismatch")
allowed_files = [(Path(root) / (config["plugin_module"] + ".py")).resolve()
                 for root in config["plugin_paths"]]
spec = importlib.util.find_spec(config["plugin_module"])
if spec is not None and (spec.origin is None or Path(spec.origin).resolve() not in allowed_files):
    raise RuntimeError("plugin source is outside configured plugin_paths")
plugin = importlib.import_module(config["plugin_module"])
plugin_file = Path(plugin.__file__).resolve()
if plugin_file not in allowed_files:
    raise RuntimeError("plugin source is outside configured plugin_paths")
if plugin is not importlib.import_module(config["plugin_module"]):
    raise RuntimeError("plugin import identity changed")
factory = getattr(plugin, config["plugin_factory"])
if not callable(factory):
    raise TypeError("configured plugin factory is not callable")
result = factory(config["plugin_config"])
modules = sorted(name for name in sys.modules if name.startswith("trade_engine_rs"))
if modules != ["trade_engine_rs"]:
    raise RuntimeError("multiple mandatory module instances loaded")
report_json = json.dumps({
    "result": result,
    "module_origin": native.__spec__.origin,
    "module_file": getattr(native, "__file__", None),
    "module_version": native.__version__,
    "builtin_module_count": sys.builtin_module_names.count("trade_engine_rs"),
    "python_version": sys.version,
    "python_home": sys.base_prefix,
    "python_prefix": sys.prefix,
    "python_executable": sys.executable,
    "search_paths": sys.path,
    "plugin_file": str(plugin_file),
    "build_python": build_python,
}, ensure_ascii=True)
