"""Tests for version reporting and CLI."""

import io
from contextlib import redirect_stdout
from pathlib import Path

import trade_engine
from trade_engine.cli import main


def test_cli_version_outputs_version_and_resolved_path() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = main(["--version"])
    output = buf.getvalue().strip()

    assert ret == 0
    assert "trade-engine 0.1.0" in output
    expected_path = str(Path(trade_engine.__file__).resolve().parent)
    assert expected_path in output
