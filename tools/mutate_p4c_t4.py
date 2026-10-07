"""Native event-store hand mutants; artifact rebuilds and assertion-only kills."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
PY=Path(sys.executable).resolve()
ENV=dict(os.environ,PATH=str(Path.home()/".cargo"/"bin")+os.pathsep+os.environ["PATH"],
    PYTHONDONTWRITEBYTECODE="1",PYTHONIOENCODING="utf-8",PYO3_PYTHON=str(PY),
    CARGO_TARGET_DIR=str(ROOT/"crates"/"target"),
    TEMP=str(ROOT/".ci-local"/"temp"),TMP=str(ROOT/".ci-local"/"temp"),TMPDIR=str(ROOT/".ci-local"/"temp"))
ENV.pop("PYTEST_ADDOPTS",None)
RUNNER="""
import pytest,sys
class Reporter:
    def pytest_runtest_makereport(self,item,call):
        if call.when=='call' and call.excinfo is not None:
            category='ASSERTION' if issubclass(call.excinfo.type,AssertionError) else 'NON_ASSERTION'
            print('T4_'+category+'_FAILURE='+item.nodeid,flush=True)
raise SystemExit(pytest.main(sys.argv[1:],plugins=[Reporter()]))
"""
HOST=r"crates\te_host\src\store.rs"
PYHOST=r"crates\te_py\src\store.rs"
MUTANTS=(
    ("open-before-guard",HOST,
     'let guard = SingleInstanceGuard::acquire(sidecar, pid).map_err(OpenError::Lock)?;\n'
     '        // This must remain after guard acquisition, including schema initialization.\n'
     '        let conn = Connection::open(path).map_err(OpenError::Sql)?;',
     'let conn = Connection::open(path).map_err(OpenError::Sql)?;\n'
     '        let guard = SingleInstanceGuard::acquire(sidecar, pid).map_err(OpenError::Lock)?;'),
    ("truncate-journal-lost",HOST,"PRAGMA journal_mode=TRUNCATE;","PRAGMA journal_mode=WAL;"),
    ("durability-weakened",HOST,"PRAGMA synchronous=FULL;","PRAGMA synchronous=NORMAL;"),
    ("foreign-keys-disabled",HOST,"PRAGMA foreign_keys=ON;","PRAGMA foreign_keys=OFF;"),
    ("command-replay-lost",HOST,"if let Some(event) = self.by_command(&command)? {",
     "if let Some(event) = None::<StoredRow> {"),
    ("accounts-alphabetized",HOST,"GROUP BY account ORDER BY MIN(seq)","GROUP BY account ORDER BY account"),
    ("fold-applied-twice",HOST,"host.apply(index, &event)?;",
     "host.apply(index, &event)?; host.apply(index, &event)?;"),
    ("atomic-outbox-lost",HOST,"host.outbox(index, event.seq)?;","let _ = (index, event.seq);"),
    ("batch-commits-per-event",HOST,"host.outbox(index, event.seq)?;",
     'host.outbox(index, event.seq)?; host.commit()?; self.execute("BEGIN IMMEDIATE", &[])?;'),
    ("failure-cache-retained",HOST,"host.drop_account(&account);",
     "let _ = account;"),
    ("baseexception-rollback-changed",HOST,"if in_transaction && host.is_exception(&error)",
     "if in_transaction"),
    ("reader-refresh-lost",PYHOST,"(self.reader && refresh && previous != newest)","false"),
    ("snapshot-cutoff-exclusive",PYHOST,'.into_pyobject(py)?.le(seq)?','.into_pyobject(py)?.lt(seq)?'),
    ("timestamp-precision-lost",PYHOST,'.call_method0("isoformat")?',
     '.call_method1("isoformat",("T","seconds"))?'),
    ("sequence-validation-lost",PYHOST,'if !event.bind(py).getattr("seq")?.is_none() {','if false {'),
)


def build():
    proc=subprocess.run([str(PY),"-B",str(ROOT/"tools"/"build_p4c_t3.py")],
        cwd=ROOT,env=ENV,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=600)
    if proc.returncode:
        raise RuntimeError("rebuild failed\n"+proc.stdout[-1000:]+proc.stderr[-5000:])


def tests(*, guard_only=False):
    targets=([str(ROOT/"tests"/"test_p4c_lock.py")+"::test_refusal_precedes_any_writable_db_open"]
             if guard_only else [str(ROOT/"tests"/"test_p4c_store.py"),
                                str(ROOT/"tests"/"test_p4c_lock.py")+"::test_refusal_precedes_any_writable_db_open"])
    return subprocess.run([str(PY),"-B","-c",RUNNER,*targets,
        "-x","-q","--tb=short","-p","no:cacheprovider"],cwd=ROOT,env=ENV,
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=180)


def main():
    assert PY==(ROOT/".venv"/"Scripts"/"python.exe").resolve()
    assert sys.version_info[:2]==(3,13)
    (ROOT/".ci-local"/"temp").mkdir(parents=True,exist_ok=True)
    selected=set(sys.argv[1:])
    assert selected<={name for name,*_ in MUTANTS}
    originals={ROOT/file:(ROOT/file).read_bytes() for _,file,_,_ in MUTANTS}
    killed,invalid=[],[]
    baseline_green=restored_green=False
    try:
        build()
        baseline=tests()
        baseline_green=baseline.returncode==0
        print(f"BASELINE exit={baseline.returncode}\n{baseline.stdout[-1600:]}",flush=True)
        if baseline_green:
            for name,file,anchor,replacement in MUTANTS:
                if selected and name not in selected: continue
                source=ROOT/file
                text=originals[source].decode("utf-8")
                if "\r\n" in text:
                    anchor=anchor.replace("\n","\r\n"); replacement=replacement.replace("\n","\r\n")
                assert text.count(anchor)==1,(name,text.count(anchor))
                started=time.perf_counter()
                try:
                    source.write_bytes(text.replace(anchor,replacement).encode("utf-8"))
                    build()
                    proc=tests(guard_only=name=="open-before-guard")
                    failure=[line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    if (proc.returncode==1 and failure and "T4_ASSERTION_FAILURE=" in proc.stdout
                        and "T4_NON_ASSERTION_FAILURE=" not in proc.stdout and "ERROR collecting" not in proc.stdout):
                        killed.append(name)
                        print(f"KILLED {name} {time.perf_counter()-started:.3f}s {failure[0]}",flush=True)
                    else:
                        invalid.append(name)
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-4000:]}",flush=True)
                except (RuntimeError,subprocess.TimeoutExpired) as error:
                    invalid.append(name); print(f"INVALID {name}: {error}",flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source,content in originals.items(): source.write_bytes(content)
        build()  # Unconditional, even when baseline/mutant/anchor verification failed.
        restored=tests()
        restored_green=restored.returncode==0
        print(f"RESTORED exit={restored.returncode}\n{restored.stdout[-1600:]}",flush=True)
        for source,content in originals.items():
            assert source.read_bytes()==content
            print("RESTORED_SHA256",source.relative_to(ROOT),hashlib.sha256(content).hexdigest(),flush=True)
    print(f"SUMMARY killed={len(killed)} invalid={invalid}",flush=True)
    return 0 if baseline_green and restored_green and not invalid else 1


if __name__=="__main__":
    raise SystemExit(main())
