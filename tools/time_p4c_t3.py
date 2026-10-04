"""Fair cold-process discovery/import comparison, not a trading hot-path gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_p4c_embed import BINARY, PLUGIN, PYTHON, STANDALONE


def main():
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    assert BINARY.is_file()
    scratch = ROOT / ".ci-local" / "t3-timing"
    scratch.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    results = []
    try:
        for index in range(9):
            directory = scratch / f"synthetic {index} café 🚀"
            directory.mkdir()
            (directory / "fake_plugin.py").write_text(PLUGIN, encoding="utf-8")
            config = {
                "mode": "packaging-proof", "python_home": sys.base_prefix,
                "python_dll": str(BINARY.parent / "python313.dll"),
                "python_executable": str(PYTHON),
                "site_packages": str(ROOT / ".venv" / "Lib" / "site-packages"),
                "engine_source": str(ROOT / "src"), "plugin_paths": [str(directory)],
                "plugin_module": "fake_plugin", "plugin_factory": "probe",
                "plugin_config": {"tag": f"synthetic startup {index}"},
            }
            path = directory / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            commands = {
                "python": [str(PYTHON), "-I", "-S", "-B", "-c", STANDALONE, str(path)],
                "native": [str(BINARY), "--proof", "--config", str(path)],
            }
            samples = {"python": [], "native": []}
            for repeat in range(7):
                order = ("python", "native") if repeat % 2 == 0 else ("native", "python")
                for name in order:
                    start = time.perf_counter()
                    proc = subprocess.run(commands[name], cwd=directory, env=env,
                                          capture_output=True, text=True, encoding="utf-8",
                                          timeout=30, check=True)
                    elapsed = (time.perf_counter() - start) * 1000
                    report = json.loads(proc.stdout)
                    assert report["result"]["answer"] is True
                    if repeat >= 2:
                        samples[name].append(elapsed)
            results.append({name: statistics.median(values) for name, values in samples.items()})
        old = statistics.median(row["python"] for row in results)
        new = statistics.median(row["native"] for row in results)
        print(json.dumps({
            "kind": "synthetic process bootstrap, NOT hot-path certification",
            "samples": results, "python_median_ms": old, "native_median_ms": new,
            "ratio": new / old,
            "python_max_sample_median_ms": max(row["python"] for row in results),
            "native_max_sample_median_ms": max(row["native"] for row in results),
        }, indent=2))
    finally:
        shutil.rmtree(scratch)


if __name__ == "__main__":
    main()
