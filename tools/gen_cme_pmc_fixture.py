"""Generate the pandas_market_calendars oracle fixtures for crates/te_core/tests/cme_pmc_oracle.rs.

Equity   -> cme_pmc_fixture.csv          from CME_Equity        (P6b)
Energy   -> cme_pmc_fixture_energy.csv   from CMEGlobex_CL      (P6C; pmc builds every CMEGlobex_* energy/metals
Metals   -> cme_pmc_fixture_metals.csv   from CMEGlobex_GC       calendar from one shared rule set)
"""

from pathlib import Path
import pandas_market_calendars as mcal

OUT_DIR = Path(__file__).resolve().parent.parent / "crates" / "te_core" / "tests" / "fixtures"
TARGETS = [
    ("CME_Equity", "cme_pmc_fixture.csv", "2006-01-01"),
    ("CMEGlobex_CL", "cme_pmc_fixture_energy.csv", "2009-01-01"),
    ("CMEGlobex_GC", "cme_pmc_fixture_metals.csv", "2009-01-01"),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, fname, start in TARGETS:
        schedule = mcal.get_calendar(name).schedule(start_date=start, end_date="2027-12-31")
        out_file = OUT_DIR / fname
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            f.write(f"# pandas_market_calendars=={mcal.__version__} calendar={name}\n")
            f.write("date,market_open_utc,market_close_utc\n")
            for idx, row in schedule.iterrows():
                d = idx.strftime("%Y-%m-%d")
                open_utc = row["market_open"].strftime("%Y-%m-%dT%H:%M:%SZ")
                close_utc = row["market_close"].strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(f"{d},{open_utc},{close_utc}\n")
        print(f"Wrote {len(schedule)} rows to {out_file}")


if __name__ == "__main__":
    main()
