# TEST-HANG-1: find the test that hangs and grows memory in the full suite

## Symptom
`python -m pytest -q` (repo root, `.venv\Scripts\python.exe`, `PYTHONPATH=src`) stops progressing at about 23% and the python process grows to many GB (14 GB seen). Targeted runs (`-k "bp4 or bp5 or registry or schwab"`) pass in about 156 s.

## Rules
- Run at most ONE pytest process at a time. Before starting one, confirm no other pytest from you is running.
- Never kill processes by image name. To stop your own run, find its PID, confirm its CommandLine contains `pytest`, and stop only that PID.
- Watch memory: if your pytest process passes 3 GB working set, stop it (by PID) and record the last test name printed.
- Do not start any real service, feed, hub or broker connection. Do not read token or secret files.

## Do
1. Find the culprit: `python -m pytest -q -p no:cacheprovider --timeout=120 -o faulthandler_timeout=150 -v` (install nothing; if `pytest-timeout` is missing, use `-o faulthandler_timeout=120` alone and bisect by directory/file with `--collect-only -q` order). Record the test id where progress stops and the faulthandler traceback.
2. Read the test and the code it drives. Name the cause (unbounded loop, a thread that never sees its stop event, a real socket/sleep, an unbounded list, a fixture that leaks between tests).
3. Fix at the narrowest layer: if the test is wrong, fix the test; if production code has an unbounded loop or buffer, fix that with a bound and add a test that proves the bound. Keep behavior otherwise unchanged.
4. Re-run that file, then the full suite once, to completion. Report pass/fail counts and wall time.

Commit (only the files you changed, staged by name): `fix(tests): TEST-HANG-1 <one-line cause>`. Do not push.
