//! The IBKR accounts adapter's one rule (BROKER_PLATFORM phase 6b, plan phase 6).
//!
//! Two source shapes, both parsed from text; there is no socket and no HTTP client here
//! (I7, D3). The Python I/O shell is a stub that is handed the text:
//!
//! - **TWS or IB Gateway:** the `managedAccounts` callback plus `reqAccountUpdates`'s
//!   `updateAccountValue` messages, one tab-separated message per line:
//!   `managedAccounts\t<id>,<id>` and
//!   `updateAccountValue\t<key>\t<value>\t<currency>\t<account>`;
//! - **Client Portal:** the bare JSON array `GET /portfolio/accounts` returns, with
//!   `accountId` required and `accountType` optional.
//!
//! Rules:
//! - the key is `ibkr:<accountId>` through the phase 2 key rule; the mask is the one
//!   mask rule (`display` is `*` plus the last three characters);
//! - `DU...` is a paper account (`env: paper`); every other account is `live`;
//! - the account type is `margin`, `cash` or `ira` only when the source says exactly
//!   `MARGIN`, `CASH` or `IRA`; any other spelling leaves it null, never guessed;
//! - an unknown text shape is refused (`unknown_ibkr_shape`), an account value for an
//!   account the source did not list is refused (`unknown_ibkr_account`), and a list
//!   over the account port's hard cap is refused (`cap_exceeded`, B7);
//! - time arrives as an argument (`now`), never read here (I7).
//!
//! Never Flex: only the two live source shapes above are accepted.

use std::collections::{BTreeMap, BTreeSet};

use super::accounts::{display, key, Record, Route, Snapshot, MAX_RECORDS};
use super::ledger::json::{self, Json};
use super::ledger::model::{err, DateTime, LErr, R};

/// One text read is capped at 8 MiB (about 40,000 account values); over it the read is
/// refused rather than buffered without bound (B7).
pub const MAX_TEXT_BYTES: usize = 8 * 1024 * 1024;
/// Account values per read are capped too: 8 per account at the hard account cap.
pub const MAX_LINES: usize = MAX_RECORDS * 8;
pub const PAPER_PREFIX: &str = "DU";
pub const SOURCE_TWS: &str = "ibkr:tws";
pub const SOURCE_PORTAL: &str = "ibkr:client_portal";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Shape {
    Tws,
    Portal,
}

fn shape(text: &str) -> R<Shape> {
    let trimmed = text.trim_start();
    if trimmed.starts_with('[') {
        return Ok(Shape::Portal);
    }
    let first = text.lines().next().unwrap_or("").trim_end_matches('\r');
    let head = first.split('\t').next().unwrap_or("");
    if head == "managedAccounts" || head == "updateAccountValue" {
        return Ok(Shape::Tws);
    }
    err(
        "unknown_ibkr_shape",
        "the text is neither TWS managedAccounts/updateAccountValue messages nor a Client Portal accounts JSON array",
    )
}

/// `MARGIN` -> `margin`, `CASH` -> `cash`, `IRA` -> `ira`; anything else is null.
fn account_type(raw: Option<&str>) -> Option<String> {
    match raw.map(|s| s.to_ascii_uppercase()).as_deref() {
        Some("MARGIN") => Some("margin".to_string()),
        Some("CASH") => Some("cash".to_string()),
        Some("IRA") => Some("ira".to_string()),
        _ => None,
    }
}

fn record(id: &str, ty: Option<&str>, source: &str, now: &DateTime) -> R<Record> {
    let key = key("ibkr", id)?;
    Ok(Record {
        key,
        display: display(id),
        env: if id.starts_with(PAPER_PREFIX) { "paper" } else { "live" }.to_string(),
        status: "active".to_string(),
        class_: None,
        firm: None,
        account_type: account_type(ty),
        routes: vec![Route { platform: "ibkr".into(), local_name: id.to_string() }],
        source: source.to_string(),
        confirmed_at: now.clone(),
    })
}

fn check_cap(n: usize) -> R<()> {
    if n > MAX_RECORDS {
        return err(
            "cap_exceeded",
            format!("{n} IBKR accounts is over the {MAX_RECORDS}-account cap; the read is refused (B7)"),
        );
    }
    Ok(())
}

fn parse_tws(text: &str, now: &DateTime) -> R<Snapshot> {
    let mut ids: Option<Vec<String>> = None;
    let mut types: BTreeMap<String, String> = BTreeMap::new();
    let mut lines = 0usize;
    for raw in text.lines() {
        let line = raw.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }
        lines += 1;
        if lines > MAX_LINES {
            return err(
                "cap_exceeded",
                format!("the IBKR text has more than {MAX_LINES} messages; the read is refused (B7)"),
            );
        }
        let fields: Vec<&str> = line.split('\t').collect();
        match fields[0] {
            "managedAccounts" => {
                if fields.len() != 2 {
                    return err("bad_shape", "managedAccounts is one tab-separated field: managedAccounts\\t<id>,<id>");
                }
                if ids.is_some() {
                    return err("bad_shape", "managedAccounts appears twice in one read");
                }
                let mut list = Vec::new();
                let mut seen: BTreeSet<&str> = BTreeSet::new();
                for id in fields[1].split(',') {
                    if id.is_empty() {
                        return err("bad_shape", "managedAccounts carries an empty account id");
                    }
                    if !seen.insert(id) {
                        return err("bad_shape", format!("managedAccounts names {id:?} twice"));
                    }
                    list.push(id.to_string());
                }
                check_cap(list.len())?;
                ids = Some(list);
            }
            "updateAccountValue" => {
                if fields.len() != 5 {
                    return err(
                        "bad_shape",
                        "updateAccountValue is four tab-separated fields: updateAccountValue\\t<key>\\t<value>\\t<currency>\\t<account>",
                    );
                }
                let (name, value, account) = (fields[1], fields[2], fields[4]);
                if account.is_empty() {
                    return err("bad_shape", "updateAccountValue carries an empty account");
                }
                if name == "AccountType" {
                    match types.get(account) {
                        Some(old) if old != value => {
                            return err(
                                "bad_shape",
                                format!("account {account:?} reports two AccountType values"),
                            )
                        }
                        _ => {
                            types.insert(account.to_string(), value.to_string());
                        }
                    }
                }
            }
            other => {
                return err(
                    "unknown_ibkr_shape",
                    format!("{other:?} is not a managedAccounts or updateAccountValue message"),
                )
            }
        }
    }
    let ids = match ids {
        Some(ids) => ids,
        None => return err("bad_shape", "the TWS read carries no managedAccounts message"),
    };
    let known: BTreeSet<&str> = ids.iter().map(String::as_str).collect();
    for account in types.keys() {
        if !known.contains(account.as_str()) {
            return err(
                "unknown_ibkr_account",
                format!("account values name {account:?}, which managedAccounts did not list"),
            );
        }
    }
    let mut records = Vec::with_capacity(ids.len());
    for id in &ids {
        records.push(record(id, types.get(id).map(String::as_str), SOURCE_TWS, now)?);
    }
    Ok(Snapshot { records, confirmed_at: Some(now.clone()) })
}

fn parse_portal(text: &str, now: &DateTime) -> R<Snapshot> {
    let node = json::parse(text).map_err(|e| LErr {
        kind: "bad_shape",
        msg: format!("the Client Portal accounts text is not JSON: {}", e.message),
    })?;
    let items = match &node {
        Json::Arr(items) => items,
        other => {
            return err(
                "unknown_ibkr_shape",
                format!("Client Portal accounts must be a JSON array, got {}", other.type_name()),
            )
        }
    };
    check_cap(items.len())?;
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut records = Vec::with_capacity(items.len());
    for item in items {
        if !matches!(item, Json::Obj(_)) {
            return err(
                "bad_shape",
                format!("a Client Portal account must be an object, got {}", item.type_name()),
            );
        }
        let id = match item.get("accountId") {
            Some(Json::Str(s)) if !s.is_empty() => s.as_str(),
            Some(Json::Str(_)) => return err("bad_shape", "a Client Portal accountId is empty"),
            Some(other) => {
                return err(
                    "bad_shape",
                    format!("a Client Portal accountId must be a string, got {}", other.type_name()),
                )
            }
            None => return err("bad_shape", "a Client Portal account has no accountId"),
        };
        let ty = match item.get("accountType") {
            None | Some(Json::Null) => None,
            Some(Json::Str(s)) => Some(s.as_str()),
            Some(other) => {
                return err(
                    "bad_shape",
                    format!("a Client Portal accountType must be a string or null, got {}", other.type_name()),
                )
            }
        };
        if !seen.insert(id) {
            return err("bad_shape", format!("duplicate Client Portal accountId {id:?}"));
        }
        records.push(record(id, ty, SOURCE_PORTAL, now)?);
    }
    Ok(Snapshot { records, confirmed_at: Some(now.clone()) })
}

/// Parse one read of either source shape. The shape is detected from the text; an
/// unknown shape is refused with `unknown_ibkr_shape` (never an empty fleet).
pub fn snapshot_from_text(text: &str, now: &DateTime) -> R<Snapshot> {
    if text.len() > MAX_TEXT_BYTES {
        return err(
            "cap_exceeded",
            format!("{} bytes is over the {MAX_TEXT_BYTES}-byte IBKR text cap (B7)", text.len()),
        );
    }
    match shape(text)? {
        Shape::Tws => parse_tws(text, now),
        Shape::Portal => parse_portal(text, now),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::accounts;
    use crate::ledger::model::parse_datetime;

    fn at(text: &str) -> DateTime {
        let (dt, aware) = parse_datetime(text).expect("iso");
        assert!(aware);
        dt
    }

    fn tws_text() -> String {
        concat!(
            "managedAccounts\tACCT-TEST-194,DUACCT-TEST-195,ACCT-TEST-196\n",
            "updateAccountValue\tNetLiquidation\t250000.00\tUSD\tACCT-TEST-194\n",
            "updateAccountValue\tAccountType\tMARGIN\tUSD\tACCT-TEST-194\n",
            "updateAccountValue\tAccountType\tCASH\tUSD\tDUACCT-TEST-195\n"
        )
        .to_string()
    }

    fn portal_text() -> String {
        concat!(
            r#"[{"accountId":"ACCT-TEST-194","accountType":"MARGIN","currency":"USD"},"#,
            r#"{"accountId":"DUACCT-TEST-195","accountType":"IRA","currency":"USD"},"#,
            r#"{"accountId":"ACCT-TEST-196","currency":"USD"}]"#
        )
        .to_string()
    }

    #[test]
    fn the_tws_shape_maps_key_env_type_and_route() {
        let snap = snapshot_from_text(&tws_text(), &at("2026-11-11T18:00:00Z")).unwrap();
        assert_eq!(snap.records.len(), 3);
        assert_eq!(snap.records[0].key, "ibkr:ACCT-TEST-194");
        assert_eq!(snap.records[0].display, "*194");
        assert_eq!(snap.records[0].env, "live");
        assert_eq!(snap.records[0].account_type.as_deref(), Some("margin"));
        assert_eq!(snap.records[0].source, SOURCE_TWS);
        assert_eq!(snap.records[0].routes[0].platform, "ibkr");
        assert_eq!(snap.records[0].routes[0].local_name, "ACCT-TEST-194");
        assert_eq!(snap.records[1].env, "paper");
        assert_eq!(snap.records[1].account_type.as_deref(), Some("cash"));
        assert_eq!(snap.records[2].env, "live");
        assert_eq!(snap.records[2].account_type, None);
    }

    #[test]
    fn the_portal_shape_maps_key_env_type_and_route() {
        let snap = snapshot_from_text(&portal_text(), &at("2026-11-11T18:00:00Z")).unwrap();
        assert_eq!(snap.records.len(), 3);
        assert_eq!(snap.records[0].key, "ibkr:ACCT-TEST-194");
        assert_eq!(snap.records[0].source, SOURCE_PORTAL);
        assert_eq!(snap.records[0].account_type.as_deref(), Some("margin"));
        assert_eq!(snap.records[1].env, "paper");
        assert_eq!(snap.records[1].account_type.as_deref(), Some("ira"));
        assert_eq!(snap.records[2].account_type, None);
    }

    #[test]
    fn both_shapes_round_trip_through_the_snapshot_contract() {
        for text in [tws_text(), portal_text()] {
            let snap = snapshot_from_text(&text, &at("2026-11-11T18:00:00Z")).unwrap();
            let wire = accounts::snapshot_to_json(&snap);
            let back = accounts::snapshot_from_json(&wire).unwrap();
            assert_eq!(back.records.len(), 3);
            assert!(wire.contains("\"confirmed_at\":\"2026-11-11T18:00:00Z\""));
        }
    }

    #[test]
    fn an_unknown_shape_is_refused_with_a_kind() {
        for text in ["", "garbage", "{\"accounts\":[]}", "FlexQuery:123"] {
            assert_eq!(
                snapshot_from_text(text, &at("2026-11-11T18:00:00Z")).unwrap_err().kind,
                "unknown_ibkr_shape",
                "text {text:?}"
            );
        }
    }

    #[test]
    fn an_unknown_account_in_tws_values_or_portal_fields_is_refused() {
        let text = concat!(
            "managedAccounts\tACCT-TEST-194\n",
            "updateAccountValue\tAccountType\tMARGIN\tUSD\tACCT-TEST-999\n"
        );
        assert_eq!(
            snapshot_from_text(text, &at("2026-11-11T18:00:00Z")).unwrap_err().kind,
            "unknown_ibkr_account"
        );
        assert_eq!(
            snapshot_from_text(
                r#"[{"accountId":"ACCT-TEST-194"},{"accountId":"ACCT-TEST-194"}]"#,
                &at("2026-11-11T18:00:00Z")
            )
            .unwrap_err()
            .kind,
            "bad_shape"
        );
        assert_eq!(
            snapshot_from_text(r#"[{"accountType":"MARGIN"}]"#, &at("2026-11-11T18:00:00Z"))
                .unwrap_err()
                .kind,
            "bad_shape"
        );
    }

    #[test]
    fn a_tws_read_without_managed_accounts_is_refused() {
        let text = "updateAccountValue\tAccountType\tMARGIN\tUSD\tACCT-TEST-194\n";
        assert_eq!(
            snapshot_from_text(text, &at("2026-11-11T18:00:00Z")).unwrap_err().kind,
            "bad_shape"
        );
    }

    #[test]
    fn the_cap_is_the_account_port_cap_and_it_refuses() {
        assert_eq!(MAX_RECORDS, 2_000);
        let ids: Vec<String> = (0..=MAX_RECORDS).map(|i| format!("ACCT-TEST-{i:04}")).collect();
        let tws = format!("managedAccounts\t{}\n", ids.join(","));
        assert_eq!(
            snapshot_from_text(&tws, &at("2026-11-11T18:00:00Z")).unwrap_err().kind,
            "cap_exceeded"
        );
        let rows: Vec<String> = ids.iter().map(|id| format!(r#"{{"accountId":"{id}"}}"#)).collect();
        let portal = format!("[{}]", rows.join(","));
        assert_eq!(
            snapshot_from_text(&portal, &at("2026-11-11T18:00:00Z")).unwrap_err().kind,
            "cap_exceeded"
        );
    }
}
