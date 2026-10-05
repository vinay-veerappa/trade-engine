//! EOD/pass and snapshot routing flow. Observations and effects are supplied by the
//! host; the core neither reads a clock nor owns Python objects or a ledger.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Close,
    Pass,
}

#[derive(Clone, Copy, Debug)]
pub enum Boundary {
    Close,
    Settled,
    Through,
}

#[derive(Clone, Copy, Debug)]
pub enum Finish {
    Reconcile,
    Sync,
    Dividends,
    Marks,
    Manage,
    Entries,
    Marker,
}

pub struct Account<V> {
    pub id: String,
    pub instruments: V,
    pub tally: V,
    pub options: bool,
}

pub struct Observation<V> {
    pub at: V,
    pub item: V,
    pub accounts: Vec<String>,
    pub snapshot: bool,
    pub regular: bool,
}

pub trait EodHost {
    type Value: Clone;
    type Error;

    fn validate(&mut self, mode: Mode) -> Result<(), Self::Error>;
    fn accounts(&self) -> Result<Vec<String>, Self::Error>;
    fn is_options(&self, account: &str) -> Result<bool, Self::Error>;
    fn completed(&self, account: &str, mode: Mode) -> Result<bool, Self::Error>;
    fn prepare(&mut self, account: &str) -> Result<Self::Value, Self::Error>;
    fn tally(&self, account: &str) -> Result<Self::Value, Self::Error>;
    fn snapshots(&self, options: &[String], mode: Mode) -> Result<Vec<Self::Value>, Self::Error>;
    fn pass_end(&self, account: &str) -> Result<Option<Self::Value>, Self::Error>;
    fn validate_resume(&self, account: &str, end: &Self::Value) -> Result<(), Self::Error>;
    fn timeline(
        &self,
        accounts: &[Account<Self::Value>],
        snapshots: &[Self::Value],
        options: &[String],
    ) -> Result<Vec<Observation<Self::Value>>, Self::Error>;
    fn replay_accounts(
        &self,
        accounts: &[Account<Self::Value>],
        snapshots: &[Self::Value],
        options: &[String],
        ends: &[(String, Self::Value)],
    ) -> Result<(), Self::Error>;
    fn advance(&self, at: &Self::Value) -> Result<(), Self::Error>;
    fn covered(&self, snapshot: &Self::Value, end: &Self::Value) -> Result<bool, Self::Error>;
    fn remember(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> Result<(), Self::Error>;
    fn snapshot(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> Result<(), Self::Error>;
    fn bar(&self, account: &Account<Self::Value>, row: &Observation<Self::Value>) -> Result<(), Self::Error>;
    fn boundary(&self, boundary: Boundary) -> Result<(), Self::Error>;
    fn settle(&self, accounts: &[String]) -> Result<(), Self::Error>;
    fn finish(&self, account: &Account<Self::Value>, effect: Finish) -> Result<i64, Self::Error>;
    fn finish_account(&self, account: &Account<Self::Value>) -> Result<Self::Value, Self::Error>;
    fn result(
        &self,
        account: &Account<Self::Value>,
        mode: Mode,
        orders: i64,
        exits: i64,
    ) -> Result<Self::Value, Self::Error>;
    fn empty_result(&self, account: &str) -> Result<Self::Value, Self::Error>;
    fn drain(&self) -> Result<(), Self::Error>;
}

pub fn run<H: EodHost>(host: &mut H, mode: Mode) -> Result<Vec<H::Value>, H::Error> {
    host.validate(mode)?;
    let ids = host.accounts()?;
    let mut accounts = Vec::new();
    // In a close run all marker reads precede any venue connection. Pass selection
    // additionally observes venue capability before the marker reads.
    let mut pending = Vec::new();
    for id in &ids {
        if mode == Mode::Pass && !host.is_options(id)? {
            continue;
        }
        if !host.completed(id, mode)? {
            pending.push(id.clone());
        }
    }
    let mut ends = Vec::new();
    if mode == Mode::Pass {
        for id in &pending {
            if let Some(end) = host.pass_end(id)? {
                ends.push((id.clone(), end));
            }
        }
        for (id, end) in &ends {
            host.validate_resume(id, end)?;
        }
    }
    let mut prepared = Vec::new();
    for id in &pending {
        prepared.push(host.prepare(id)?);
    }
    for (id, instruments) in pending.iter().zip(prepared) {
        accounts.push(Account {
            id: id.clone(),
            instruments,
            tally: host.tally(id)?,
            options: mode == Mode::Pass,
        });
    }
    let mut options = Vec::new();
    for account in &mut accounts {
        if mode == Mode::Close {
            account.options = host.is_options(&account.id)?;
        }
        if account.options {
            options.push(account.id.clone());
        }
    }
    let snapshots = host.snapshots(&options, mode)?;
    if mode == Mode::Close {
        for id in &options {
            if let Some(end) = host.pass_end(id)? {
                ends.push((id.clone(), end));
            }
        }
    }
    host.replay_accounts(&accounts, &snapshots, &options, &ends)?;
    host.boundary(if mode == Mode::Pass { Boundary::Through } else { Boundary::Close })?;
    let mut results = Vec::new();
    if mode == Mode::Close {
        for id in &ids {
            match accounts.iter().find(|a| &a.id == id) {
                Some(account) if !account.options => {
                    results.push((id.clone(), host.finish_account(account)?));
                }
                None => results.push((id.clone(), host.empty_result(id)?)),
                _ => {}
            }
        }
        if !options.is_empty() {
            host.boundary(Boundary::Settled)?;
            host.settle(&options)?;
            for account in accounts.iter().filter(|a| a.options) {
                results.push((account.id.clone(), host.finish_account(account)?));
            }
        }
    } else {
        for id in &ids {
            let result = match accounts.iter().find(|a| &a.id == id) {
                Some(account) => finish(host, account, mode)?,
                None => host.empty_result(id)?,
            };
            results.push((id.clone(), result));
        }
    }
    host.drain()?;
    results.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(results.into_iter().map(|(_, value)| value).collect())
}

pub fn replay<H: EodHost>(
    host: &H,
    accounts: &[Account<H::Value>],
    snapshots: &[H::Value],
    options: &[String],
    ends: &[(String, H::Value)],
) -> Result<(), H::Error> {
    for row in host.timeline(accounts, snapshots, options)? {
        host.advance(&row.at)?;
        for id in &row.accounts {
            // Timeline account IDs originate from these same prepared accounts.
            let account = accounts.iter().find(|a| &a.id == id).expect("prepared timeline account");
            if row.snapshot {
                if let Some((_, end)) = ends.iter().find(|(a, _)| a == id) {
                    if host.covered(&row.item, end)? {
                        host.remember(account, &row.item)?;
                        continue;
                    }
                }
                host.snapshot(account, &row.item)?;
            } else {
                host.bar(account, &row)?;
            }
        }
    }
    Ok(())
}

pub fn finish<H: EodHost>(
    host: &H,
    account: &Account<H::Value>,
    mode: Mode,
) -> Result<H::Value, H::Error> {
    let mut orders = 0;
    let mut exits = 0;
    if mode == Mode::Close {
        host.finish(account, Finish::Reconcile)?;
        if account.options {
            host.finish(account, Finish::Sync)?;
            host.finish(account, Finish::Dividends)?;
        }
        host.finish(account, Finish::Marks)?;
        exits = host.finish(account, Finish::Manage)?;
        orders = host.finish(account, Finish::Entries)?;
    }
    host.finish(account, Finish::Marker)?;
    host.result(account, mode, orders, exits)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionKind {
    Entry,
    Structure,
    Holding,
}

pub trait RoutingHost {
    type Action;
    type Tally;
    type Error;
    fn manager(&self) -> Result<(), Self::Error>;
    fn match_snapshot(&self) -> Result<(), Self::Error>;
    fn remember(&self) -> Result<(), Self::Error>;
    fn has_strategy(&self) -> Result<bool, Self::Error>;
    fn actions(&self) -> Result<Vec<Self::Action>, Self::Error>;
    fn taken(&self, action: &Self::Action) -> Result<bool, Self::Error>;
    fn classify(&self, action: &Self::Action) -> Result<ActionKind, Self::Error>;
    fn enter(&self, action: &Self::Action) -> Result<i64, Self::Error>;
    fn close(&self, action: &Self::Action, holding: bool) -> Result<(), Self::Error>;
    fn apply_actions(&self, actions: Vec<Self::Action>) -> Result<(i64, i64), Self::Error>;
    fn tally(&self, orders: i64, exits: i64, snapshots: i64) -> Result<Self::Tally, Self::Error>;
    fn rounds_refusal(&self) -> Self::Error;
}

pub trait EntryHost {
    type Value: Clone;
    type Error;
    fn validate(&mut self, intent: &Self::Value) -> Result<(), Self::Error>;
    fn evaluate(&self, intent: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn record(&self, intent: &Self::Value, verdict: &Self::Value) -> Result<(), Self::Error>;
    fn approved(&self, intent: &Self::Value, verdict: &Self::Value) -> Result<(bool, bool), Self::Error>;
    fn resize(&self, intent: &Self::Value, verdict: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn open(&self, intent: &Self::Value) -> Result<(), Self::Error>;
}

pub fn enter_option<H: EntryHost>(host: &mut H, intent: H::Value) -> Result<i64, H::Error> {
    host.validate(&intent)?;
    let verdict = host.evaluate(&intent)?;
    host.record(&intent, &verdict)?;
    let (accepted, resize) = host.approved(&intent, &verdict)?;
    if !accepted { return Ok(0); }
    let intent = if resize { host.resize(&intent, &verdict)? } else { intent };
    host.open(&intent)?;
    Ok(1)
}

pub trait SignalHost {
    type Value;
    type Error;
    fn setup(&mut self) -> Result<bool, Self::Error>;
    fn next_signal(&mut self) -> Result<Option<Self::Value>, Self::Error>;
    fn record_signal(&self, signal: &Self::Value) -> Result<(), Self::Error>;
    fn start_intents(&mut self) -> Result<(), Self::Error>;
    fn next_intent(&mut self) -> Result<Option<Self::Value>, Self::Error>;
    fn enter(&self, intent: &Self::Value) -> Result<i64, Self::Error>;
    fn count(&self, submitted: i64) -> Result<(), Self::Error>;
}

pub fn entries<H: SignalHost>(host: &mut H) -> Result<i64, H::Error> {
    if !host.setup()? { return Ok(0); }
    while let Some(signal) = host.next_signal()? {
        host.record_signal(&signal)?;
    }
    host.start_intents()?;
    let mut submitted = 0;
    while let Some(intent) = host.next_intent()? {
        let count = host.enter(&intent)?;
        host.count(count)?;
        submitted += count;
    }
    Ok(submitted)
}

pub trait EquityEntryHost {
    type Value;
    type Error;
    fn validate(&self, intent: &Self::Value) -> Result<(), Self::Error>;
    fn evaluate(&self, intent: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn record(&self, intent: &Self::Value, verdict: &Self::Value) -> Result<(), Self::Error>;
    fn approved(&self, verdict: &Self::Value) -> Result<bool, Self::Error>;
    fn bracket(&self, intent: &Self::Value, verdict: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn submit(&self, bracket: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn terminal(&self, order: &Self::Value) -> Result<bool, Self::Error>;
    fn reconcile(&self, bracket: &Self::Value) -> Result<(), Self::Error>;
}

pub fn enter_equity<H: EquityEntryHost>(host: &H, intent: &H::Value) -> Result<i64, H::Error> {
    host.validate(intent)?;
    let verdict = host.evaluate(intent)?;
    host.record(intent, &verdict)?;
    if !host.approved(&verdict)? { return Ok(0); }
    let bracket = host.bracket(intent, &verdict)?;
    let submitted = host.submit(&bracket)?;
    if !host.terminal(&submitted)? {
        host.reconcile(&bracket)?;
    }
    Ok(1)
}

#[derive(Clone, Copy)]
pub enum ExitKind {
    Move,
    Close,
    Reduce,
}

pub trait ExitHost {
    type Value;
    type Error;
    fn actions(&mut self) -> Result<Vec<Self::Value>, Self::Error>;
    fn classify(&self, action: &Self::Value) -> Result<ExitKind, Self::Error>;
    fn move_stop(&self, action: &Self::Value) -> Result<(), Self::Error>;
    fn close(&self, action: &Self::Value, reduce: bool) -> Result<Self::Value, Self::Error>;
    fn terminal(&self, order: &Self::Value) -> Result<bool, Self::Error>;
    fn reconcile(&self, order: &Self::Value) -> Result<(), Self::Error>;
}

pub fn manage_positions<H: ExitHost>(host: &mut H) -> Result<i64, H::Error> {
    let mut applied = 0;
    for action in host.actions()? {
        match host.classify(&action)? {
            ExitKind::Move => host.move_stop(&action)?,
            kind => {
                let order = host.close(&action, matches!(kind, ExitKind::Reduce))?;
                if !host.terminal(&order)? {
                    host.reconcile(&order)?;
                }
            }
        }
        applied += 1;
    }
    Ok(applied)
}

pub fn apply<H: RoutingHost>(
    host: &H,
    actions: impl IntoIterator<Item = Result<H::Action, H::Error>>,
) -> Result<(i64, i64), H::Error> {
    let (mut orders, mut exits) = (0, 0);
    for action in actions {
        let action = action?;
        match host.classify(&action)? {
            ActionKind::Entry => orders += host.enter(&action)?,
            ActionKind::Structure => {
                host.close(&action, false)?;
                exits += 1;
            }
            ActionKind::Holding => {
                host.close(&action, true)?;
                exits += 1;
            }
        }
    }
    Ok((orders, exits))
}

pub fn manage_snapshot<H: RoutingHost>(host: &H, rounds: usize) -> Result<H::Tally, H::Error> {
    host.manager()?;
    host.match_snapshot()?;
    host.remember()?;
    let (mut orders, mut exits) = (0, 0);
    if !host.has_strategy()? {
        return host.tally(orders, exits, 1);
    }
    for _ in 0..rounds {
        let actions = host.actions()?;
        let mut all_taken = true;
        for action in &actions {
            if !host.taken(action)? {
                all_taken = false;
                break;
            }
        }
        if all_taken {
            return host.tally(orders, exits, 1);
        }
        let (submitted, closed) = host.apply_actions(actions)?;
        orders += submitted;
        exits += closed;
        host.match_snapshot()?;
    }
    Err(host.rounds_refusal())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct Entry {
        calls: RefCell<Vec<&'static str>>,
        accepted: bool,
        resize: bool,
    }
    impl EntryHost for Entry {
        type Value = i64;
        type Error = &'static str;
        fn validate(&mut self, _: &i64) -> Result<(), Self::Error> {
            self.calls.borrow_mut().push("validate");
            Ok(())
        }
        fn evaluate(&self, _: &i64) -> Result<i64, Self::Error> {
            self.calls.borrow_mut().push("evaluate");
            Ok(1)
        }
        fn record(&self, _: &i64, _: &i64) -> Result<(), Self::Error> {
            self.calls.borrow_mut().push("record");
            Ok(())
        }
        fn approved(&self, _: &i64, _: &i64) -> Result<(bool, bool), Self::Error> {
            self.calls.borrow_mut().push("approved");
            Ok((self.accepted, self.resize))
        }
        fn resize(&self, _: &i64, verdict: &i64) -> Result<i64, Self::Error> {
            self.calls.borrow_mut().push("resize");
            Ok(*verdict)
        }
        fn open(&self, intent: &i64) -> Result<(), Self::Error> {
            self.calls.borrow_mut().push("open");
            if *intent == 1 { Ok(()) } else { Err("wrong quantity") }
        }
    }
    #[test]
    fn refused_entry_still_records_verdict() {
        let mut host = Entry { calls: RefCell::new(Vec::new()), accepted: false, resize: false };
        assert_eq!(enter_option(&mut host, 2), Ok(0));
        assert_eq!(*host.calls.borrow(), ["validate", "evaluate", "record", "approved"]);
    }
    #[test]
    fn resize_precedes_open_and_host_error_is_not_swallowed() {
        let mut host = Entry { calls: RefCell::new(Vec::new()), accepted: true, resize: true };
        assert_eq!(enter_option(&mut host, 2), Ok(1));
        assert_eq!(*host.calls.borrow(), ["validate", "evaluate", "record", "approved", "resize", "open"]);
        host.resize = false;
        assert_eq!(enter_option(&mut host, 2), Err("wrong quantity"));
    }
}
