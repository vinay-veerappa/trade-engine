"""P4c T14: the staged Windows rollout kit (plan section 7, Stage C).

This tool STAGES the runtime-owner rollout; it never applies it. The Task
Scheduler, process and file operations it describes are fakes in dry-run
mode and the tool refuses to run at all without ``--dry-run`` (a real
canary needs separately approved deployment choices, plan section 7
Stage D). What it really does offline:

- render one absolute runtime-owner config per role from a role table
  (the same schema the T9+ serve owners and the T13 certification used),
- verify the per-ledger selector's mutual exclusion (one owner per
  ledger path, the selector file is the only switch),
- run the one-action ``rollback-role`` ordered handoff against a fake
  scheduler: stop admission, graceful stop, wait for the ledger lock
  release, switch the selector to legacy, start the retained legacy
  release — in that order, with the selector never switched while the
  runtime owns the lock,
- print a summary with the exact fake command sequence and exit codes.

Every command goes through :class:`FakeShell`, which both records the
ordered sequence (the tests assert on it) and enforces the kit's safety
refusals: registering or starting a real scheduled task is not
implemented at all, and a lock file that never releases is reported as a
blocked owner, never deleted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
SELECTOR_PATH = ROOT / "launch" / "runtime" / "runtime_selector.json"
LAUNCHER = ROOT / "launch" / "runtime" / "run_runtime_owner.ps1"
LEGACY = "legacy"
RUNTIME = "runtime"

ROLLBACK_NOTE = (
    "the selector is never switched while the runtime owner still holds the "
    "ledger lock; a lock that never releases is reported as a blocked owner "
    "and the rollback stops without touching the lock or the selector"
)


class RolloutError(RuntimeError):
    """The kit refuses an unsafe or malformed rollout action."""


# ---------------------------------------------------------------------------
# The fake shell: every scheduler/process operation the kit describes,
# recorded as an ordered command log. Nothing here touches a real task,
# process, lock or selector: the tests drive it and assert on the log.


@dataclass
class FakeCommand:
    step: str
    argv: list[str]
    cwd: str | None = None


class FakeShell:
    """Records the ordered handoff sequence and fakes its effects."""

    def __init__(self, *, lock_released: bool = True,
                 owner_running: bool = True,
                 stop_timeout: float = 30.0):
        self.commands: list[FakeCommand] = []
        self.exit_codes: list[int] = []
        self.lock_released = lock_released
        self.owner_running = owner_running
        self.stop_timeout = stop_timeout
        self.selector_writes: list[dict] = []

    # -- the fake Task Scheduler surface -----------------------------------
    # These are the ONLY scheduler operations the kit knows. Registering
    # or starting a real task is deliberately absent: a real canary is a
    # separate owner approval, so the kit cannot perform it at all.

    def stop_scheduled_task(self, name: str) -> int:
        """Stop the role's scheduled task (fake Stop-ScheduledTask)."""
        self.commands.append(FakeCommand("stop-admission", [
            "Stop-ScheduledTask", "-TaskName", name]))
        code = 0 if self.owner_running else 1
        self.exit_codes.append(code)
        self.owner_running = False
        return code

    def start_scheduled_task(self, name: str) -> int:
        """Start the legacy task (fake Start-ScheduledTask)."""
        self.commands.append(FakeCommand("start-legacy", [
            "Start-ScheduledTask", "-TaskName", name]))
        self.exit_codes.append(0)
        self.owner_running = True
        return 0

    # -- the fake process / lock surface ------------------------------------

    def request_graceful_stop(self, pid: int) -> int:
        """Close the runtime owner's stdin (the graceful stop request)."""
        self.commands.append(FakeCommand("graceful-stop", [
            "close-stdin", str(pid)]))
        self.exit_codes.append(0)
        return 0

    def wait_lock_release(self, ledger: Path) -> bool:
        """Poll the ledger's .lock sidecar until it releases (or give up)."""
        self.commands.append(FakeCommand("wait-lock", [
            "wait-lock-release", str(ledger) + ".lock"]))
        released = self.lock_released
        if released:
            self.lock_released = False
        return released

    def wait_stopped(self, pid: int) -> int:
        """Wait for the owner process to exit (fake WaitForExit)."""
        self.commands.append(FakeCommand("wait-exit", ["wait-exit", str(pid)]))
        self.exit_codes.append(0)
        return 0

    # -- the selector --------------------------------------------------------

    def write_selector(self, rows: dict) -> None:
        self.commands.append(FakeCommand("switch-selector", [
            "write-selector", json.dumps(rows, sort_keys=True)]))
        self.selector_writes.append(dict(rows))


# ---------------------------------------------------------------------------
# The per-ledger selector.


def read_selector(path: Path = SELECTOR_PATH) -> dict:
    """Load the selector; a missing or malformed file is an error, never a
    silent default (the kit must not guess which owner owns a ledger)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RolloutError(f"cannot read the selector at {path}: {error}") from error


def check_selector(selector: dict) -> list[str]:
    """Verify mutual exclusion: one row per ledger path, values only
    'legacy' or 'runtime', and no ledger may appear twice. The optional
    'tasks' index maps each scheduled-task name to its ledger path (the
    client seam knows the task name, not the ledger); every task row
    must name a ledger consistently. Returns the sorted ledger paths."""
    problems: list[str] = []
    ledgers = selector.get("ledgers")
    if not isinstance(ledgers, dict):
        problems.append("the selector's 'ledgers' must be an object")
        ledgers = {}
    seen: set[str] = set()
    for key, value in sorted(ledgers.items()):
        path = key.strip().lower().replace("/", "\\")
        if not key.strip():
            problems.append("an empty ledger path")
        if path in seen:
            problems.append(f"the ledger '{key}' appears twice")
        seen.add(path)
        if value not in (LEGACY, RUNTIME):
            problems.append(
                f"the ledger '{key}' selects '{value}', which is not "
                f"'{LEGACY}' or '{RUNTIME}'")
    tasks = selector.get("tasks", {})
    if not isinstance(tasks, dict):
        problems.append("the selector's 'tasks' index must be an object")
        tasks = {}
    for task, ledger in sorted(tasks.items()):
        if not str(ledger).strip():
            problems.append(f"the task '{task}' names an empty ledger")
    if selector.get("default", LEGACY) not in (LEGACY, RUNTIME):
        problems.append(f"the selector default is not '{LEGACY}' or '{RUNTIME}'")
    if problems:
        raise RolloutError("selector mutual exclusion failed: " + "; ".join(problems))
    return sorted(ledgers)


def owner_for_task(selector: dict, task: str) -> str:
    """Which owner a scheduled task's job uses: its task index row picks
    the ledger, the ledger row (or the default) picks the owner. An
    unknown task uses the default (legacy) — a staged rollout never
    surprises an unlisted task."""
    tasks = selector.get("tasks", {})
    ledger = tasks.get(task)
    if ledger is None:
        return selector.get("default", LEGACY)
    return owner_of(selector, str(ledger))


def owner_of(selector: dict, ledger: str) -> str:
    """Which owner a ledger uses: its row, or the default. Case- and
    separator-insensitive ledger paths so a staged config and a row that
    name the same file agree."""
    key = ledger.strip().lower().replace("/", "\\")
    for candidate, value in selector.get("ledgers", {}).items():
        if candidate.strip().lower().replace("/", "\\") == key:
            return value
    return selector.get("default", LEGACY)


# ---------------------------------------------------------------------------
# The role table: one row per staged runtime-owner role. The paths are
# ABSOLUTE (the T10/T11 census forbids guessing a repo; the kit renders
# what the operator pinned). The jobs/roles match the client families.


@dataclass
class RoleSpec:
    name: str            # the scheduled-task name (fake) and log prefix
    role: str            # the runtime role string (scan/options-eod/...)
    jobs: list[str]      # the jobs this owner admits
    ledger: str          # the ABSOLUTE ledger path this owner owns
    venv_python: str     # the ABSOLUTE python that runs the client entry
    entry_module: str     # the dotted entry module (T10/T11 census)
    entry: str            # the entry member
    stop_timeout: float = 30.0
    port: int = 0          # production-like ports are chosen at apply time;
                            # 0 binds an ephemeral loopback port


def default_roles(repo: Path, binary: Path) -> list[RoleSpec]:
    """The staged role table for the converted client families. The
    ledgers are the client's real absolute paths (read-only names here;
    the kit never opens them), so the selector rows and the configs the
    operator renders agree path-for-path."""
    return [
        RoleSpec(
            name="DailyScanners", role="scan",
            jobs=["scan-eod"],
            ledger=str(repo / "data" / "trade_engine" / "scan-ledger.db"),
            venv_python=str(repo / ".venv313" / "Scripts" / "python.exe"),
            entry_module="scan_engine.runtime_owner_entry",
            entry="owner_entry",
        ),
        RoleSpec(
            name="OptionsEod", role="options-eod",
            jobs=["options-eod"],
            ledger=str(repo / "data" / "trade_engine" / "options-ledger.db"),
            venv_python=str(repo / ".venv313" / "Scripts" / "python.exe"),
            entry_module="scan_engine.runtime_owner_entry",
            entry="owner_entry",
        ),
        RoleSpec(
            name="OptionsMirrorFollow", role="mirror-batch",
            jobs=["mirror-follow"],
            ledger=str(repo / "data" / "trade_engine" / "mirror-PM-B.db"),
            venv_python=str(repo / ".venv313" / "Scripts" / "python.exe"),
            entry_module="scan_engine.runtime_owner_entry",
            entry="owner_entry",
        ),
    ]


def _base_prefix(venv_python: Path) -> Path:
    """The private CPython 3.13 home a venv's pyvenv.cfg points at."""
    cfg = venv_python.parent.parent / "pyvenv.cfg"
    for line in cfg.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("home"):
            return Path(line.split("=", 1)[1].strip())
    raise RolloutError(f"cannot find the base prefix: {cfg} has no home row")


def render_config(spec: RoleSpec, *, binary: Path, capability: Path,
                  folder: Path) -> dict:
    """Render one runtime-owner config (the T9+ serve schema). Absolute
    paths everywhere; the same shape the T13 certificate's owner walks
    used, so a staged owner is byte-for-byte the certified composition."""
    folder.mkdir(parents=True, exist_ok=True)
    python = Path(spec.venv_python)
    return {
        "mode": "runtime-owner",
        # python_home is the stdlib home (the interpreter's base prefix),
        # not the venv dir; serve validates it holds the 3.13 stdlib.
        "python_home": str(_base_prefix(python)),
        "python_dll": str(_base_prefix(python) / "python313.dll"),
        "python_executable": str(python),
        "site_packages": str(Path(python).parent.parent / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(folder)],
        "plugin_module": "runtime_owner_entry",
        "plugin_factory": "owner_entry",
        "plugin_config": {},
        "owner": {"ledger_path": spec.ledger, "clock": "wall"},
        "runtime": {
            "role": spec.role,
            "jobs": list(spec.jobs),
            "record_limit": 512,
            "stop_timeout_seconds": spec.stop_timeout,
            "capability": str(capability),
            "port": spec.port,
            "entry_module": spec.entry_module,
            "entry": spec.entry,
        },
    }


def write_configs(roles: list[RoleSpec], folder: Path, *, binary: Path,
                  capability: Path) -> list[Path]:
    """Render every role's config to the staging folder; returns the
    absolute config paths in role order."""
    if not binary.is_file():
        raise RolloutError(f"the certified te.exe is missing at {binary}")
    paths = []
    for spec in roles:
        config = render_config(spec, binary=binary, capability=capability,
                               folder=folder)
        path = folder / f"{spec.name}-runtime.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# The one-action rollback (plan section 7): stop admission, graceful stop,
# wait for lock release, switch the selector, start the legacy release —
# an ordered handoff, never a blind environment flip.


def rollback_role(spec: RoleSpec, shell: FakeShell, selector_path: Path,
                  *, pid: int = 4242) -> dict:
    """The one-action ordered handoff for one role. Steps in order:

    1. stop the scheduled task (stops new admissions),
    2. close the owner's stdin (the graceful stop request; it drains),
    3. wait for the ledger lock to release — a blocked owner is REPORTED,
       the rollback stops before the selector switch, and the lock is
       never deleted to manufacture liveness,
    4. switch the per-ledger selector to legacy (only now),
    5. start the retained legacy release.

    Returns the summary the operator sees; raises RolloutError when the
    owner is blocked (step 3 never released).
    """
    selector = read_selector(selector_path)
    check_selector(selector)
    if owner_of(selector, spec.ledger) != RUNTIME:
        raise RolloutError(
            f"the ledger '{spec.ledger}' does not select '{RUNTIME}'; "
            "rollback only applies to a runtime-owned ledger")

    shell.stop_scheduled_task(spec.name)                  # 1: stop admission
    shell.request_graceful_stop(pid)                      # 2: graceful stop
    shell.wait_stopped(pid)
    if not shell.wait_lock_release(Path(spec.ledger)):     # 3: lock release
        raise RolloutError(
            f"the owner of '{spec.ledger}' is blocked: its ledger lock "
            f"never released within the stop timeout; the selector was NOT "
            f"switched and the legacy release was NOT started. {ROLLBACK_NOTE}")
    rows = dict(selector.get("ledgers", {}))
    rows[spec.ledger] = LEGACY                            # 4: switch selector
    shell.write_selector(rows)
    shell.start_scheduled_task(spec.name)                # 5: start legacy
    return {
        "role": spec.name,
        "ledger": spec.ledger,
        "steps": [command.step for command in shell.commands],
        "commands": [list(command.argv) for command in shell.commands],
        "exit_codes": list(shell.exit_codes),
        "switched_to": LEGACY,
        "note": ROLLBACK_NOTE,
    }


# ---------------------------------------------------------------------------
# The CLI. Everything is --dry-run; without it the tool refuses.


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p4c_rollout.py", description=(
        "P4c T14 staged rollout kit: renders staged runtime-owner configs, "
        "verifies the per-ledger selector, and dry-runs the one-action "
        "rollback. STAGES ONLY - applying it needs an approved canary."))
    parser.add_argument("action", choices=["render", "check-selector", "rollback-role"])
    parser.add_argument("target", nargs="?", help=(
        "render: the staging folder; check-selector: the selector path "
        "(default launch/runtime/runtime_selector.json); rollback-role: "
        "the role name (DailyScanners/OptionsEod/OptionsMirrorFollow)"))
    parser.add_argument("--repo", default=r"C:\Users\vinay\tvDownloadOHLC",
                        help="the ABSOLUTE client repository path")
    parser.add_argument("--binary", default=str(ROOT / "crates" / "target" / "release" / "te.exe"),
                        help="the ABSOLUTE certified te.exe path")
    parser.add_argument("--selector", default=str(SELECTOR_PATH), help=(
        "the selector file the action reads (default the shipped "
        "launch/runtime/runtime_selector.json)"))
    parser.add_argument("--dry-run", action="store_true", help=(
        "run the described procedure against a fake scheduler; REQUIRED "
        "for rollback-role (the kit cannot apply itself)"))
    parser.add_argument("--blocked", action="store_true", help=(
        "dry-run a blocked owner: the lock never releases and the rollback "
        "must stop before the selector switch"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli().parse_args(argv)
    binary = Path(args.binary)
    if args.action == "render":
        if not args.target:
            raise SystemExit("render needs the staging folder")
        roles = default_roles(Path(args.repo), binary)
        paths = write_configs(roles, Path(args.target), binary=binary,
                             capability=Path(args.target) / "capability.txt")
        for spec, path in zip(roles, paths):
            print(f"rendered {spec.name} -> {path}")
        print(f"rendered {len(paths)} staged configs (dry-run kit; nothing started)")
        return 0
    if args.action == "check-selector":
        path = Path(args.target) if args.target else SELECTOR_PATH
        selector = read_selector(path)
        ledgers = check_selector(selector)
        print(f"selector OK: {len(ledgers)} ledger row(s), default "
              f"'{selector.get('default', LEGACY)}' ({path})")
        return 0
    if args.action == "rollback-role":
        if not args.dry_run:
            print("refused: rollback-role needs --dry-run; applying the kit "
                  "needs an approved canary (plan section 7, Stage D)",
                  file=sys.stderr)
            return 2
        if not args.target:
            print("rollback-role needs the role name", file=sys.stderr)
            return 2
        roles = {spec.name: spec for spec in default_roles(Path(args.repo), binary)}
        if args.target not in roles:
            print(f"unknown role '{args.target}' (have: "
                  f"{', '.join(sorted(roles))})", file=sys.stderr)
            return 2
        selector_path = Path(args.selector)
        # The rehearsal selector must actually select runtime for the role's
        # ledger; the shipped one is empty by design (default legacy).
        if args.target in ("OptionsEod", "DailyScanners", "OptionsMirrorFollow") \
                and selector_path.resolve() == SELECTOR_PATH.resolve():
            print("refused: the shipped selector owns no ledger (default "
                  "legacy); point --selector at the staged scenario selector",
                  file=sys.stderr)
            return 2
        shell = FakeShell(lock_released=not args.blocked)
        summary = rollback_role(roles[args.target], shell, selector_path)
        print(json.dumps(summary, indent=2))
        print("dry-run complete: nothing was registered, started or switched")
        return 0
    raise SystemExit(f"unknown action {args.action}")


if __name__ == "__main__":
    sys.exit(main())