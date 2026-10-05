"""T0 Python-tooling mutants: compile each edit, rebuild native artifacts, restore."""
from __future__ import annotations

import hashlib
import subprocess
import sys

from mutate_p4c_t6 import build, ENV, PY, ROOT

CORE = r"tools\p4c_corpus.py"
MUTANTS = (
    ("version-ignored", CORE, 'if type(value["version"]) is not int or value["version"] != VERSION:', "if False:"),
    ("synthetic-relabeled-recorded", CORE, 'if value["provenance"] != "synthetic":', "if False:"),
    ("source-release-ignored", CORE, 'if value["source_release"] != oracles["source_release"]:', "if False:"),
    ("producer-hash-ignored", CORE, 'if value["plugin_sha256"] != hashlib.sha256((ROOT / "tools" / "p4c_corpus_worlds.py").read_bytes()).hexdigest():', "if False:"),
    ("fixture-configuration-ignored", CORE, 'if not isinstance(fixture["inputs"], dict) or fixture["config_sha256"] != digest(fixture["inputs"]):',
     'if not isinstance(fixture["inputs"], dict):'),
    ("observation-sequence-ignored", CORE, 'if type(frame["seq"]) is not int or frame["seq"] != index + 1:', "if False:"),
    ("argument-divergence-accepted", CORE, "if observed != expected:", "if False:"),
    ("host-refusal-returned-as-success", CORE, 'raise decode_error(frame["outcome"]["raise"])', 'return decode_error(frame["outcome"]["raise"])'),
    ("backwards-clock-allowed", CORE, "if previous is not None and returned < previous:", "if False:"),
    ("missing-observations-accepted", CORE, "if self.index != len(self.tape):", "if False:"),
    ("golden-overwrite-allowed", CORE, 'resolved.open("x", encoding="utf-8", newline="\\n")', 'resolved.open("w", encoding="utf-8", newline="\\n")'),
    ("output-confinement-omitted", CORE, "if not resolved.is_relative_to(root) or resolved == root:", "if False:"),
    ("unallowlisted-method-accepted", CORE, "if actor not in METHODS or method not in METHODS[actor]:", "if False:"),
    ("decimal-spelling-normalized", CORE, 'return {"tag": "decimal", "value": str(value)}', 'return {"tag": "decimal", "value": str(value.normalize())}'),
    ("inventory-discrepancy-ignored", CORE, "if missing:", "if False:"),
)
RUNNER = """
import pytest,sys
class Reporter:
    def pytest_runtest_makereport(self,item,call):
        if call.when=='call' and call.excinfo is not None:
            expectation=(issubclass(call.excinfo.type,pytest.fail.Exception)
                         and str(call.excinfo.value).startswith('DID NOT RAISE '))
            assertion=issubclass(call.excinfo.type,AssertionError) or expectation
            kind='ASSERTION' if assertion else 'NON_ASSERTION'
            print('T0_'+kind+'_FAILURE='+item.nodeid,flush=True)
raise SystemExit(pytest.main(sys.argv[1:],plugins=[Reporter()]))
"""


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER, "tests/test_p4c_corpus.py", "-x", "-q",
                           "--tb=short", "-p", "no:cacheprovider"], cwd=ROOT, env=ENV,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


def main():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    selected = set(sys.argv[1:])
    assert selected <= {name for name, *_ in MUTANTS}
    originals = {ROOT / file: (ROOT / file).read_bytes() for _, file, *_ in MUTANTS}
    killed, invalid = [], []
    baseline_green = restored_green = False
    try:
        build()
        proc = tests()
        baseline_green = proc.returncode == 0
        print(f"BASELINE exit={proc.returncode}\n{proc.stdout[-1500:]}", flush=True)
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
                try:
                    changed = text.replace(anchor, replacement)
                    compile(changed, str(source), "exec")
                    source.write_bytes(changed.encode("utf-8"))
                    build()
                    proc = tests()
                    failures = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    if (proc.returncode == 1 and failures and "T0_ASSERTION_FAILURE=" in proc.stdout
                            and "T0_NON_ASSERTION_FAILURE=" not in proc.stdout and "ERROR collecting" not in proc.stdout):
                        killed.append(name)
                        print(f"KILLED {name} {failures[0]}", flush=True)
                    else:
                        invalid.append(name)
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-5000:]}", flush=True)
                except (RuntimeError, SyntaxError, subprocess.TimeoutExpired) as error:
                    invalid.append(name)
                    print(f"INVALID {name}: {error}", flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
        build()
        proc = tests()
        restored_green = proc.returncode == 0
        print(f"RESTORED exit={proc.returncode}\n{proc.stdout[-1500:]}", flush=True)
        for source, original in originals.items():
            assert source.read_bytes() == original
            print("RESTORED_SHA256", source.relative_to(ROOT), hashlib.sha256(original).hexdigest(), flush=True)
    expected = len(selected) if selected else len(MUTANTS)
    print(f"SUMMARY killed={len(killed)} expected={expected} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and len(killed) == expected and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())
