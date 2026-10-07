"""Nine paired frozen/native measurements over populated synthetic books."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/"tests"))
from frozen_p4c.store import Ledger as FrozenLedger
from p4c_store_fixture import populated_book,hot_events
from trade_engine.ledger.store import Ledger
from trade_engine.ledger.codec import canon


def timed(fn):
    start=time.perf_counter_ns()
    value=fn()
    return (time.perf_counter_ns()-start)/1e6,value


def main():
    assert Path(sys.executable).resolve() == (ROOT/".venv"/"Scripts"/"python.exe").resolve()
    root=ROOT/".ci-local"/"t4-timing"/str(uuid.uuid4())
    root.mkdir(parents=True)
    samples={key:{"frozen":[],"native":[]} for key in ("append_fold","fold_all","open_close")}
    seed=populated_book()
    hot=list(hot_events())
    try:
        for sample in range(9):
            states={}
            # Alternate order so startup/OS-cache noise cannot consistently favor one.
            classes=[("frozen",FrozenLedger),("native",Ledger)]
            if sample%2: classes.reverse()
            for name,cls in classes:
                path=root/f"{sample}-{name}.db"
                with cls(path) as ledger:
                    ledger.extend(seed)
                    for account in ledger.accounts():
                        state=ledger.state(account)
                        assert len(state.positions)==120 and len(state.orders)==120
                    def append_fold():
                        for event in hot:
                            ledger.append(event)
                            ledger.state(event.account)
                            ledger.fold_handle(event.account)
                    duration,_=timed(append_fold)
                    samples["append_fold"][name].append(duration)
                    duration,full=timed(ledger.fold)
                    samples["fold_all"][name].append(duration)
                    states[name]=canon(full)
                duration,_=timed(lambda: open_close(cls,path))
                samples["open_close"][name].append(duration)
            assert states["frozen"]==states["native"]
    finally:
        shutil.rmtree(root)
    report={"samples":9,"seed_events":len(seed),"hot_events_per_sample":len(hot),
            "accounts":3,"positions_orders_per_account":120,"timings":{}}
    for key,values in samples.items():
        old,new=statistics.median(values["frozen"]),statistics.median(values["native"])
        report["timings"][key]={"frozen_ms":values["frozen"],"native_ms":values["native"],
            "frozen_median_ms":old,"native_median_ms":new,"ratio":new/old}
    print(json.dumps(report,indent=2))
    assert report["timings"]["append_fold"]["ratio"]<=1.25, report
    return 0


def open_close(cls,path):
    for _ in range(15):
        with cls(path) as ledger:
            assert ledger.count()==1780


if __name__=="__main__":
    raise SystemExit(main())
