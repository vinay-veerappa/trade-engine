//! NT8 Desktop as a route (BROKER_PLATFORM phase 6a, design §6.2 and §10.3).
//!
//! NT8 is a platform, not a broker: there is no NT8 `BrokerAdapter`. One pure function
//! maps an NT8 account name to the broker key the engine and riskguard already agree on,
//! using the phase 3 alias rule (the name is the account id; the numeric Tradovate id is
//! engine-only, D-13):
//!
//! - a Tradovate-connected prop account (every prefix in the riskguard firm map:
//!   `TAKEPROFITPRO*`, `TAKEPROFIT*`, `PAAPEX*`, `APEX*`, `FTDFYG*`, `TDFYG*`, `TDYG*`,
//!   `LFE*`) maps to `tradovate:<name>`;
//! - an NT8-local account (`Sim101`, any `Sim*`, any `Playback*`) maps to
//!   `local:nt8:<name>`, a riskguard-only key that the engine's one key rule refuses
//!   (`accounts::key("local", _)` is `bad_key`), so it never enters a broker set;
//! - anything else is refused with `unknown_nt8_account` (I5), never guessed.
//!
//! Both outcomes carry the record's route `{platform: "nt8", local_name: <name>}`.
//! The bridge's account-list JSON is parsed from text (fixture only; no live read) and
//! capped at `accounts::MAX_RECORDS`. Nothing here reads a clock or keeps state (I7, B7).
//!
//! The firm table is the riskguard firm map's prefix list (read-only reference:
//! nt8-riskguard `docs/CONFIG_DEFAULTS.md` §R3b). Only the prefix is a rule; the firm
//! name is documentation and never reaches a record (riskguard owns `firm`).

use std::collections::BTreeSet;

use super::accounts::{self, Route};
use super::ledger::json::{self, Json};
use super::ledger::model::{err, LErr, R};

/// One NT8 account-list read holds at most the account port's hard cap (B7).
pub const MAX_NT8_ACCOUNTS: usize = accounts::MAX_RECORDS;

/// The riskguard firm map's NT8 account-name prefixes, longest first. Every one is a
/// Tradovate-connected prop account (design §10.4, D-10).
pub const FIRM_PREFIXES: [(&str, &str); 8] = [
    ("TAKEPROFITPRO", "Take Profit Trader"),
    ("TAKEPROFIT", "Take Profit Trader"),
    ("PAAPEX", "Apex"),
    ("APEX", "Apex"),
    ("FTDFYG", "Tradeify"),
    ("TDFYG", "Tradeify"),
    ("TDYG", "Tradeify"),
    ("LFE", "Lucid"),
];

/// NT8's built-in simulator account.
pub const LOCAL_EXACT: [&str; 1] = ["Sim101"];
/// NT8's local account name prefixes: the simulator family and playback.
pub const LOCAL_PREFIXES: [&str; 2] = ["Sim", "Playback"];

/// An NT8 account's mapped key, and whether it is a riskguard-only local key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Nt8Mapping {
    pub key: String,
    pub local: bool,
    pub route: Route,
}

impl Nt8Mapping {
    fn new(key: String, local: bool, name: &str) -> Nt8Mapping {
        Nt8Mapping {
            key,
            local,
            route: Route { platform: "nt8".into(), local_name: name.to_string() },
        }
    }
}

/// Map one NT8 account name to its key or a `unknown_nt8_account` refusal.
///
/// Pure and stateless: no clock, no I/O, no cache (B7). The name's case is kept; a name
/// with whitespace or `:` is never rewritten (the broker key rule refuses it, and a local
/// key would be ambiguous), so it is refused instead.
pub fn map_account(name: &str) -> R<Nt8Mapping> {
    if name.is_empty() {
        return err("unknown_nt8_account", "an empty NT8 account name names no account");
    }
    if let Some((_, firm)) = FIRM_PREFIXES.iter().find(|(prefix, _)| name.starts_with(prefix)) {
        let key = accounts::key("tradovate", name)?;
        let _ = firm; // documentation only: `firm` is riskguard's overlay, never ours.
        return Ok(Nt8Mapping::new(key, false, name));
    }
    let local = LOCAL_EXACT.contains(&name) || LOCAL_PREFIXES.iter().any(|p| name.starts_with(p));
    if local {
        if name.chars().any(char::is_whitespace) || name.contains(':') {
            return err(
                "bad_shape",
                "an NT8 local account name must have no whitespace and no ':' (the local key is local:nt8:<name>)",
            );
        }
        return Ok(Nt8Mapping::new(format!("local:nt8:{name}"), true, name));
    }
    err(
        "unknown_nt8_account",
        format!("{name:?} matches no firm prefix and no NT8-local pattern; the account is refused, not guessed"),
    )
}

/// Parse the bridge's account-list JSON text (`{"accounts":[{...,"account":"<name>"}]}`)
/// and map every account. A missing, malformed or over-cap list is refused; an unknown
/// account refuses the whole list (I5) rather than silently reporting a shorter fleet.
pub fn map_accounts(text: &str) -> R<Vec<Nt8Mapping>> {
    let node = json::parse(text).map_err(|e| LErr {
        kind: "bad_shape",
        msg: format!("the NT8 account list is not JSON: {}", e.message),
    })?;
    let items = match node.get("accounts") {
        Some(Json::Arr(items)) => items,
        Some(other) => {
            return err(
                "bad_shape",
                format!("the NT8 account list's accounts must be a list, got {}", other.type_name()),
            )
        }
        None => return err("bad_shape", "the NT8 account list has no accounts field"),
    };
    if items.len() > MAX_NT8_ACCOUNTS {
        return err(
            "cap_exceeded",
            format!(
                "{} NT8 accounts is over the {MAX_NT8_ACCOUNTS}-account cap; the list is refused (B7)",
                items.len()
            ),
        );
    }
    if let Some(count) = node.get("count") {
        match count {
            Json::Int(n) if *n >= 0 && *n as usize == items.len() => {}
            Json::Int(n) => {
                return err(
                    "bad_shape",
                    format!("the NT8 list says count {n} but carries {} accounts", items.len()),
                )
            }
            other => {
                return err(
                    "bad_shape",
                    format!("the NT8 list's count must be an integer, got {}", other.type_name()),
                )
            }
        }
    }
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        if !matches!(item, Json::Obj(_)) {
            return err(
                "bad_shape",
                format!("an NT8 account entry must be an object, got {}", item.type_name()),
            );
        }
        let name = match item.get("account") {
            Some(Json::Str(s)) if !s.is_empty() => s.as_str(),
            Some(Json::Str(_)) => return err("bad_shape", "an NT8 account entry's account is empty"),
            Some(other) => {
                return err(
                    "bad_shape",
                    format!("an NT8 account entry's account must be a string, got {}", other.type_name()),
                )
            }
            None => return err("bad_shape", "an NT8 account entry has no account field"),
        };
        if !seen.insert(name) {
            return err("bad_shape", format!("duplicate NT8 account {name:?} in one list"));
        }
        out.push(map_account(name)?);
    }
    Ok(out)
}

/// One mapping as JSON for the Python boundary: `{key, local, route:{platform, local_name}}`.
pub fn mapping_json(m: &Nt8Mapping) -> Json {
    Json::Obj(vec![
        ("key".into(), Json::Str(m.key.clone())),
        ("local".into(), Json::Bool(m.local)),
        (
            "route".into(),
            Json::Obj(vec![
                ("platform".into(), Json::Str(m.route.platform.clone())),
                ("local_name".into(), Json::Str(m.route.local_name.clone())),
            ]),
        ),
    ])
}

pub fn mappings_json(mappings: &[Nt8Mapping]) -> String {
    json::dumps(&Json::Arr(mappings.iter().map(mapping_json).collect()))
}

/// One mapping as JSON text (the pyo3 boundary).
pub fn mapping_text(m: &Nt8Mapping) -> String {
    json::dumps(&mapping_json(m))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expect_key(name: &str, key: &str, local: bool) {
        let mapped = map_account(name).unwrap();
        assert_eq!(mapped.key, key, "name {name:?}");
        assert_eq!(mapped.local, local, "name {name:?}");
        assert_eq!(mapped.route.platform, "nt8");
        assert_eq!(mapped.route.local_name, name);
    }

    fn expect_refused(name: &str, kind: &str) {
        let e = map_account(name).unwrap_err();
        assert_eq!(e.kind, kind, "name {name:?}");
    }

    /// The brief's table: at least 10 rows, every firm prefix in the riskguard firm map,
    /// Sim101, Playback, a SimFan name and a refusal.
    #[test]
    fn the_name_table_maps_each_firm_and_local_family() {
        let rows: &[(&str, &str, bool)] = &[
            ("APEX-ACCT-TEST-194", "tradovate:APEX-ACCT-TEST-194", false),
            ("PAAPEX-ACCT-TEST-195", "tradovate:PAAPEX-ACCT-TEST-195", false),
            ("TAKEPROFITPRO-ACCT-TEST-196", "tradovate:TAKEPROFITPRO-ACCT-TEST-196", false),
            ("TAKEPROFIT-ACCT-TEST-197", "tradovate:TAKEPROFIT-ACCT-TEST-197", false),
            ("TDYG-ACCT-TEST-198", "tradovate:TDYG-ACCT-TEST-198", false),
            ("TDFYG-ACCT-TEST-199", "tradovate:TDFYG-ACCT-TEST-199", false),
            ("FTDFYG-ACCT-TEST-200", "tradovate:FTDFYG-ACCT-TEST-200", false),
            ("LFE-ACCT-TEST-201", "tradovate:LFE-ACCT-TEST-201", false),
            ("Sim101", "local:nt8:Sim101", true),
            ("Playback101", "local:nt8:Playback101", true),
            ("SimFan01", "local:nt8:SimFan01", true),
            ("Sim_All_Day_ORB", "local:nt8:Sim_All_Day_ORB", true),
            ("Sim-ORB", "local:nt8:Sim-ORB", true),
        ];
        assert!(rows.len() >= 10);
        for (name, key, local) in rows {
            expect_key(name, key, *local);
        }
        expect_refused("MYSTERY-194", "unknown_nt8_account");
        expect_refused("", "unknown_nt8_account");
        expect_refused("ACME-TRADING", "unknown_nt8_account");
    }

    #[test]
    fn a_local_key_is_refused_by_the_engine_key_rule() {
        let local = map_account("Sim101").unwrap();
        assert!(local.key.starts_with("local:nt8:"));
        assert_eq!(accounts::key("local", "nt8:Sim101").unwrap_err().kind, "bad_key");
    }

    #[test]
    fn the_bridge_list_text_maps_every_account() {
        let text = concat!(
            r#"{"success":true,"count":3,"accounts":["#,
            r#"{"account":"APEX-ACCT-TEST-194","provider":"Provider31","connection":null},"#,
            r#"{"account":"Sim101","provider":"Simulator","connection":"TPT"},"#,
            r#"{"account":"Playback101","provider":"Playback","connection":"Playback"}"#,
            "]}"
        );
        let mapped = map_accounts(text).unwrap();
        assert_eq!(mapped.len(), 3);
        assert_eq!(mapped[0].key, "tradovate:APEX-ACCT-TEST-194");
        assert_eq!(mapped[1].key, "local:nt8:Sim101");
        assert_eq!(mapped[2].key, "local:nt8:Playback101");
        assert!(mappings_json(&mapped).contains("\"local_name\":\"Sim101\""));
    }

    #[test]
    fn a_list_with_an_unknown_account_or_a_bad_shape_is_refused() {
        let unknown = r#"{"accounts":[{"account":"MYSTERY-194"}]}"#;
        assert_eq!(map_accounts(unknown).unwrap_err().kind, "unknown_nt8_account");
        assert_eq!(map_accounts("not json").unwrap_err().kind, "bad_shape");
        assert_eq!(map_accounts(r#"{"accounts":{}}"#).unwrap_err().kind, "bad_shape");
        assert_eq!(map_accounts(r#"{"accounts":[{"provider":"x"}]}"#).unwrap_err().kind, "bad_shape");
        assert_eq!(
            map_accounts(r#"{"count":2,"accounts":[{"account":"Sim101"}]}"#).unwrap_err().kind,
            "bad_shape"
        );
        assert_eq!(
            map_accounts(r#"{"accounts":[{"account":"Sim101"},{"account":"Sim101"}]}"#)
                .unwrap_err()
                .kind,
            "bad_shape"
        );
    }

    #[test]
    fn a_list_over_the_cap_is_refused() {
        let rows: Vec<String> = (0..=MAX_NT8_ACCOUNTS)
            .map(|i| format!(r#"{{"account":"SimFan{i:04}"}}"#))
            .collect();
        let text = format!(r#"{{"accounts":[{}]}}"#, rows.join(","));
        assert_eq!(map_accounts(&text).unwrap_err().kind, "cap_exceeded");
    }

}
