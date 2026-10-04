"""Generate CME Equity trading hours fixture from pandas_market_calendars."""

from pathlib import Path
import pandas_market_calendars as mcal


def main() -> None:
    cal = mcal.get_calendar("CME_Equity")
    schedule = cal.schedule(start_date="2006-01-01", end_date="2027-12-31")

    out_dir = Path(__file__).resolve().parent.parent / "crates" / "te_core" / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "cme_pmc_fixture.csv"

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        f.write(f"# pandas_market_calendars=={mcal.__version__}\n")
        f.write("date,market_open_utc,market_close_utc\n")
        for idx, row in schedule.iterrows():
            d = idx.strftime("%Y-%m-%d")
            open_utc = row["market_open"].strftime("%Y-%m-%dT%H:%M:%SZ")
            close_utc = row["market_close"].strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{d},{open_utc},{close_utc}\n")
    print(f"Wrote {len(schedule)} rows to {out_file}")


if __name__ == "__main__":
    main()
