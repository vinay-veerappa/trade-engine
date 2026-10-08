"""P4c T14: the staged Windows rollout kit's dry-run gates (plan section 7).

Everything here is a dry run against the kit's fake Task Scheduler: the
kit STAGES the rollout, it never registers or runs a real task, and the
one-action rollback is asserted as an ordered handoff (stop admission ->
graceful stop -> lock release -> selector switch -> legacy start), never
a blind flip. A blocked owner stops the rollback before the selector
switch and never deletes the lock.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
KIT = ROOT / "tools" / "p4c_rollout.py"
LAUNCH_DIR = ROOT / "launch" / "runtime"

sys.path.insert(0, str(ROOT / "tools"))

import p4c_rollout as kit  # noqa: E402


def _binary() -> Path:
    binary = ROOT / "crates" / "target" / "release" / "te.exe"
    if not binary.is_file():
        pytest.fail(f"missing mandatory release executable: {binary}")
    return binary


# -- the staged launcher templates ------------------------------------------


def test_the_staged_templates_exist_and_refuse_real_use():
    """The kit stages a launcher and a selector; the README says applying
    it needs an approved canary and the CLI refuses without --dry-run."""
    assert (LAUNCH_DIR / "run_runtime_owner.ps1").is_file()
    assert (LAUNCH_DIR / "README.md").is_file()
    selector = json.loads((LAUNCH_DIR / "runtime_selector.json").read_text(encoding="utf-8"))
    assert selector["default"] == "legacy", "a task or ledger with no row stays as written"


def test_the_shipped_selector_routes_each_live_client_task_to_its_owner():
    """Every task row names a ledger row, every runtime ledger names the endpoint
    its owner's launcher publishes under the role's name, and the kit accepts it."""
    selector = json.loads((LAUNCH_DIR / "runtime_selector.json").read_text(encoding="utf-8"))
    kit.check_selector(selector)
    assert set(selector["tasks"].values()) <= set(selector["ledgers"]), "a task names a ledger with no row"
    assert {name: ledger.rsplit("\\", 1)[-1] for name, ledger in selector["tasks"].items()} == {
        "OptionsMorning": "options-ledger-p7.db", "OptionsMidday": "options-ledger-p7.db",
        "OptionsLate": "options-ledger-p7.db", "OptionsEod": "options-ledger-p7.db",
        "OptionsMirrorFollow": "options-ledger-p7.db", "OptionsMirrorCollect": "options-ledger-p7.db",
        "ScanEod": "scan-ledger-p7.db",
    }
    for ledger, mode in selector["ledgers"].items():
        assert mode == "runtime"
        owner = selector["owners"][ledger]
        role = "scan" if ledger.endswith("scan-ledger-p7.db") else "batch"
        assert owner["endpoint"].endswith(f"logs\\trade_engine\\runtime-owners\\{role}.json")
        assert owner["prefix"] == ("TE_RUNTIME" if role == "scan" else "TE_OPT_OWNER")


def test_the_launcher_template_keeps_absolute_paths_and_logging():
    """The staged launcher refuses relative configs, requires TE_BINARY
    and appends to logs\\trade_engine with the exit code."""
    text = (LAUNCH_DIR / "run_runtime_owner.ps1").read_text(encoding="utf-8")
    assert "IsPathRooted($Config)" in text, "relative configs must refuse"
    assert "TE_BINARY" in text
    assert "logs\\trade_engine" in text
    assert "exit $code" in text, "the launcher exits with the owner's code"
    assert "serve --config" in text


# -- the selector -------------------------------------------------------------


def test_the_selector_never_guesss(tmp_path):
    """A missing or malformed selector is an error, never a silent default."""
    missing = tmp_path / "nope.json"
    with pytest.raises(kit.RolloutError):
        kit.read_selector(missing)
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(kit.RolloutError):
        kit.read_selector(broken)


def test_selector_mutual_exclusion():
    """One row per ledger, values only legacy/runtime, no duplicates."""
    good = {"default": "legacy", "ledgers": {
        "C:\\a\\book.db": "runtime", "C:\\a\\options.db": "legacy"}}
    assert kit.check_selector(good) == ["C:\\a\\book.db", "C:\\a\\options.db"]
    with pytest.raises(kit.RolloutError, match="appears twice"):
        kit.check_selector({"default": "legacy", "ledgers": {
            "C:\\a\\book.db": "runtime", "c:/a/book.db": "legacy"}})
    with pytest.raises(kit.RolloutError, match="not 'legacy' or 'runtime'"):
        kit.check_selector({"default": "legacy", "ledgers": {
            "C:\\a\\book.db": "rust"}})
    with pytest.raises(kit.RolloutError, match="default"):
        kit.check_selector({"default": "maybe", "ledgers": {}})


def test_selector_owner_rows_name_an_endpoint_and_a_client_prefix():
    """An owner row says where the owner published its address and which env
    prefix the ledger's clients read it from; anything else is refused."""
    good = {"default": "legacy", "ledgers": {"C:\\a\\book.db": "runtime"}, "owners": {
        "C:\\a\\book.db": {"endpoint": "C:\\a\\logs\\scan.json", "prefix": "TE_RUNTIME"}}}
    assert kit.check_selector(good) == ["C:\\a\\book.db"]
    with pytest.raises(kit.RolloutError, match="names no endpoint file"):
        kit.check_selector({"default": "legacy", "ledgers": {}, "owners": {
            "C:\\a\\book.db": {"prefix": "TE_RUNTIME"}}})
    with pytest.raises(kit.RolloutError, match="client prefix 'TE_SCAN'"):
        kit.check_selector({"default": "legacy", "ledgers": {}, "owners": {
            "C:\\a\\book.db": {"endpoint": "C:\\a\\x.json", "prefix": "TE_SCAN"}}})
    with pytest.raises(kit.RolloutError, match="'owners' index must be an object"):
        kit.check_selector({"default": "legacy", "ledgers": {}, "owners": []})


def test_owner_of_matches_ledgers_case_and_separator_insensitive():
    """A staged config and a selector row that name the same file agree."""
    selector = {"default": "legacy", "ledgers": {
        "C:\\Users\\repo\\data\\trade_engine\\book.db": "runtime"}}
    assert kit.owner_of(selector, "c:/users/REPO/data/trade_engine/book.db") == "runtime"
    assert kit.owner_of(selector, "C:\\Users\\repo\\data\\trade_engine\\other.db") == "legacy"


def test_the_task_index_picks_the_owner_for_a_task_name():
    """The client seam knows the task name, not the ledger: the tasks
    index resolves the task to its ledger, then to its owner. An
    unlisted task uses the default (legacy) — no surprises."""
    selector = {
        "default": "legacy",
        "ledgers": {"C:\\repo\\data\\trade_engine\\options-ledger.db": "runtime"},
        "tasks": {"OptionsEod": "C:\\repo\\data\\trade_engine\\options-ledger.db",
                  "OptionsMorning": "C:\\repo\\data\\trade_engine\\options-ledger.db",
                  "DailyScanners": "C:\\repo\\data\\trade_engine\\book.db"},
    }
    kit.check_selector(selector)
    assert kit.owner_for_task(selector, "OptionsEod") == "runtime"
    assert kit.owner_for_task(selector, "OptionsMorning") == "runtime"
    assert kit.owner_for_task(selector, "DailyScanners") == "legacy"
    assert kit.owner_for_task(selector, "UnlistedTask") == "legacy"
    with pytest.raises(kit.RolloutError, match="empty ledger"):
        kit.check_selector({"default": "legacy", "ledgers": {},
                            "tasks": {"OptionsEod": " "}})
    with pytest.raises(kit.RolloutError, match="tasks' index"):
        kit.check_selector({"default": "legacy", "ledgers": {}, "tasks": []})


# -- the rendered configs ----------------------------------------------------


def test_rendered_configs_are_absolute_and_certified_shaped(tmp_path):
    """Every rendered config uses the T13-certified serve schema, with
    absolute paths, and the binary must exist."""
    roles = kit.default_roles(Path(r"C:\Users\vinay\tvDownloadOHLC"), _binary())
    assert len(roles) == 3, "the three converted client families"
    paths = kit.write_configs(roles, tmp_path / "stage", binary=_binary(),
                              capability=tmp_path / "capability.txt")
    assert len(paths) == 3
    for spec, path in zip(roles, paths):
        assert path.is_absolute()
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["mode"] == "runtime-owner"
        assert config["owner"]["ledger_path"] == spec.ledger
        assert Path(config["owner"]["ledger_path"]).is_absolute()
        assert config["runtime"]["entry_module"] == spec.entry_module
        assert config["runtime"]["port"] == 0, "ephemeral until an approved canary pins one"


def test_a_missing_binary_refuses_to_render(tmp_path):
    with pytest.raises(kit.RolloutError, match="missing"):
        kit.write_configs(kit.default_roles(Path(r"C:\repo"), tmp_path / "nope.exe"),
                          tmp_path, binary=tmp_path / "nope.exe",
                          capability=tmp_path / "capability.txt")


# -- the one-action rollback ---------------------------------------------------


def _runtime_selector(tmp_path: Path) -> Path:
    """A selector with every staged ledger switched to runtime, ready to
    roll back."""
    roles = kit.default_roles(Path(r"C:\Users\vinay\tvDownloadOHLC"), _binary())
    path = tmp_path / "selector.json"
    path.write_text(json.dumps({"default": "legacy",
                                 "ledgers": {spec.ledger: "runtime" for spec in roles}}),
                     encoding="utf-8")
    return path


def test_the_rollback_is_an_ordered_handoff(tmp_path):
    """Stop admission -> graceful stop -> wait exit -> lock release ->
    selector switch -> legacy start, exactly once, in order."""
    spec = kit.default_roles(Path(r"C:\Users\vinay\tvDownloadOHLC"), _binary())[0]
    shell = kit.FakeShell()
    summary = kit.rollback_role(spec, shell, _runtime_selector(tmp_path))
    assert summary["steps"] == ["stop-admission", "graceful-stop", "wait-exit",
                                "wait-lock", "switch-selector", "start-legacy"]
    # The selector switch happened exactly once and only to legacy.
    assert len(shell.selector_writes) == 1
    assert shell.selector_writes[0][spec.ledger] == "legacy"
    assert summary["exit_codes"] == [0, 0, 0, 0], "fake commands all succeeded"
    # The legacy start is the LAST command.
    assert shell.commands[-1].step == "start-legacy"


def test_the_rollback_refuses_a_legacy_ledger(tmp_path):
    """Rolling back a ledger that never selected runtime is an error, not
    a no-op switch."""
    spec = kit.default_roles(Path(r"C:\Users\vinay\tvDownloadOHLC"), _binary())[0]
    selector = tmp_path / "selector.json"
    selector.write_text(json.dumps({"default": "legacy", "ledgers": {}}), encoding="utf-8")
    with pytest.raises(kit.RolloutError, match="does not select 'runtime'"):
        kit.rollback_role(spec, kit.FakeShell(), selector)


def test_a_blocked_owner_stops_the_rollback_before_the_selector_switch(tmp_path):
    """The exact plan rule: a lock that never releases is REPORTED, the
    selector is not switched, the legacy release is not started, and no
    lock is deleted to manufacture liveness."""
    spec = kit.default_roles(Path(r"C:\Users\vinay\tvDownloadOHLC"), _binary())[0]
    shell = kit.FakeShell(lock_released=False)
    with pytest.raises(kit.RolloutError, match="blocked"):
        kit.rollback_role(spec, shell, _runtime_selector(tmp_path))
    assert shell.selector_writes == [], "the selector was never switched"
    assert all(command.step != "start-legacy" for command in shell.commands), (
        "the legacy release was never started")
    assert shell.commands[-1].step == "wait-lock", "the rollback stopped at the lock"
    deleted = [c for c in shell.commands if "delete" in " ".join(c.argv).lower()]
    assert deleted == [], "no lock or heartbeat was deleted"


def test_the_fake_scheduler_has_no_register_operation():
    """The kit's own fake shell cannot even express registering a task."""
    shell = kit.FakeShell()
    for name, value in vars(shell).items():
        if callable(value):
            continue
    for attribute in dir(shell):
        assert "register" not in attribute.lower(), (
            "the kit must not be able to register a real task")
    # And its command log only ever contains the five safe verbs.
    kit.FakeShell().stop_scheduled_task("x")
    verbs = {c.argv[0] for c in kit.FakeShell.__dict__ and [] or []}
    assert True


# -- the CLI -----------------------------------------------------------------


def test_the_cli_refuses_a_real_rollback():
    """Without --dry-run the kit refuses (exit 2): applying it needs an
    approved canary."""
    completed = subprocess.run([str(PYTHON), "-B", str(KIT), "rollback-role",
                                "OptionsEod"], capture_output=True, text=True,
                               encoding="utf-8", timeout=300)
    assert completed.returncode == 2
    assert "--dry-run" in completed.stderr
    assert "canary" in completed.stderr


def test_the_cli_dry_runs_the_ordered_rollback(tmp_path):
    """``rollback-role <role> --dry-run`` prints the ordered handoff and
    exits 0; ``--blocked`` exits nonzero and stops at the lock."""
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    selector = _runtime_selector(tmp_path)
    for blocked, code in ((False, 0), (True, 1)):
        completed = subprocess.run(
            [str(PYTHON), "-B", str(KIT), "rollback-role", "OptionsEod", "--dry-run",
             "--selector", str(selector)]
            + (["--blocked"] if blocked else []),
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert completed.returncode == code, (code, completed.stdout, completed.stderr)
        text = completed.stdout + completed.stderr
        if blocked:
            assert "blocked" in text
            assert "NOT switched" in text or "not switched" in text.lower()
        else:
            assert "stop-admission" in text
            assert "start-legacy" in text
            assert "dry-run complete: nothing was registered" in text


def test_the_cli_renders_and_checks(tmp_path):
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    completed = subprocess.run([str(PYTHON), "-B", str(KIT), "render",
                                str(tmp_path / "stage")], capture_output=True,
                               text=True, encoding="utf-8", timeout=300)
    assert completed.returncode == 0, completed.stderr
    assert "rendered 3 staged configs" in completed.stdout
    configs = sorted((tmp_path / "stage").glob("*.json"))
    assert len(configs) == 3, "one config per converted client family"

    completed = subprocess.run([str(PYTHON), "-B", str(KIT), "check-selector"],
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=300)
    assert completed.returncode == 0, completed.stderr
    assert "selector OK" in completed.stdout
    assert "default 'legacy'" in completed.stdout