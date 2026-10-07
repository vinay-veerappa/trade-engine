"""T2 compiling hand mutants using the established byte-restoring campaign."""
from __future__ import annotations

import subprocess
import mutate_p4c_t8 as campaign
from mutate_p4c_t6 import ENV, PY, ROOT, RUNNER

HOST = r"crates\te_host\src\http.rs"
BINDING = r"crates\te_py\src\http.rs"
MUTANTS = (
    ("invalid-host-accepted", HOST, 'if !matches!(host.as_str(), "127.0.0.1" | "localhost") {', "if false {"),
    ("cors-origin-not-echoed", HOST, 'reply\n            .headers\n            .push(("Access-Control-Allow-Origin".into(), origin));', "let _ = origin;"),
    ("query-last-duplicate-wins", BINDING, "Some(first.get_item(0)?.extract()?)", "Some(first.get_item(-1)?.extract()?)"),
    ("header-precedence-lost", HOST, "after = value;\n    }\n    Ok(Ok(after))", "let _ = value;\n    }\n    Ok(Ok(after))"),
    ("query-validation-skipped", HOST, "if let Some(value) = query {", "if let Some(value) = query.filter(|_| false) {"),
    ("snapshot-seq-incremented", HOST, '("seq", Json::Int(seq)),\n        ("accounts", Json::Obj(accounts))', '("seq", Json::Int(seq + 1)),\n        ("accounts", Json::Obj(accounts))'),
    ("optional-price-not-null", HOST, "value.as_ref().map_or(Json::Null, decimal)", 'value.as_ref().map_or(string("None"), decimal)'),
    ("retry-changed", HOST, 'write_all(b"retry: 1000\\n\\n")', 'write_all(b"retry: 2000\\n\\n")'),
    ("live-dedup-disabled", HOST, "if event.seq.is_some_and(|seq| seq <= max_sent) { continue; }", "if false { continue; }"),
    ("subscription-not-registered", HOST, '.insert(id, sender);', '; drop(sender);'),
    ("backlog-inclusive-cursor", HOST, '"SELECT * FROM events WHERE seq > ? ORDER BY seq ASC"', '"SELECT * FROM events WHERE seq >= ? ORDER BY seq ASC"'),
    ("backlog-reversed", HOST, '"SELECT * FROM events WHERE seq > ? ORDER BY seq ASC"', '"SELECT * FROM events WHERE seq > ? ORDER BY seq DESC"'),
    ("disconnect-not-unsubscribed", HOST, '.remove(&self.id);', '; let _ = self.id;'),
    ("legacy-version-not-rejected", HOST, "if major >= 2 {", "if major >= 20 {"),
    ("ping-frame-changed", HOST, 'write_all(b": ping\\n\\n")', 'write_all(b": pong\\n\\n")'),
)
REPORTER = RUNNER.replace("T6_", "T8_")


def tests():
    return subprocess.run([str(PY), "-B", "-c", REPORTER, "tests/test_p4c_http.py",
        "-x", "-q", "--tb=short", "-p", "no:cacheprovider"], cwd=ROOT, env=ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


def main():
    campaign.MUTANTS = MUTANTS
    campaign.tests = tests
    return campaign.main()


if __name__ == "__main__":
    raise SystemExit(main())
