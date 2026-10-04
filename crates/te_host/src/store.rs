//! The single connection and event transaction owner. Callback effects never borrow
//! the connection, so the compatibility SQL proxy and commit seam use this connection.
use crate::lock::{LockError, SingleInstanceGuard};
pub use rusqlite as sql;
use rusqlite::{types::Value, Connection, OpenFlags};
use std::{
    path::Path,
    sync::{Arc, Mutex},
};

pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc         TEXT    NOT NULL,
    account        TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    command_id     TEXT,
    payload_json   TEXT    NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_command_id
    ON events(command_id) WHERE command_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_account_seq ON events(account, seq);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq    INTEGER NOT NULL,
    destination  TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT    NOT NULL,
    delivered_at TEXT,
    FOREIGN KEY(event_seq) REFERENCES events(seq),
    UNIQUE(event_seq, destination)
);
CREATE INDEX IF NOT EXISTS ix_outbox_dest_status_id
    ON outbox(destination, status, id);
"#;

pub type SharedConnection = Arc<Mutex<Option<Connection>>>;

pub fn single_statement(conn: &Connection, query: &str) -> rusqlite::Result<bool> {
    let query = std::ffi::CString::new(query).map_err(|_| rusqlite::Error::InvalidQuery)?;
    let mut statement = std::ptr::null_mut();
    let mut tail = std::ptr::null();
    // The borrowed connection remains alive, the NUL-terminated SQL remains
    // alive, and the temporary parser statement is finalized on every path.
    let (code, rest) = unsafe {
        let code = rusqlite::ffi::sqlite3_prepare_v2(
            conn.handle(),
            query.as_ptr(),
            -1,
            &mut statement,
            &mut tail,
        );
        let rest = if tail.is_null() {
            &[][..]
        } else {
            std::ffi::CStr::from_ptr(tail).to_bytes()
        };
        rusqlite::ffi::sqlite3_finalize(statement);
        (code, rest)
    };
    if code != rusqlite::ffi::SQLITE_OK {
        return Err(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(code),
            None,
        ));
    }

    let mut rest = rest;
    loop {
        rest = rest.trim_ascii_start();
        if rest.starts_with(b"--") {
            rest = &rest[rest.iter().position(|b| *b == b'\n').unwrap_or(rest.len())..];
        } else if rest.starts_with(b"/*") {
            match rest.windows(2).position(|p| p == b"*/") {
                Some(end) => rest = &rest[end + 2..],
                None => return Ok(true),
            }
        } else {
            return Ok(rest.is_empty());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        path::PathBuf,
        sync::atomic::{AtomicUsize, Ordering},
    };

    static SERIAL: AtomicUsize = AtomicUsize::new(0);
    struct Synthetic(PathBuf);
    impl Synthetic {
        fn new() -> Self {
            let root = Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap();
            let path = root.join(".ci-local").join(format!(
                "t4-host-{}-{}",
                std::process::id(),
                SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn open(&self) -> Store {
            Store::open(
                &self.0.join("synthetic.db"),
                &self.0.join("synthetic.db.lock"),
                &std::process::id().to_string(),
            )
            .unwrap_or_else(|_| panic!("synthetic open failed"))
        }
    }
    impl Drop for Synthetic {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).expect("remove only own synthetic fixture");
        }
    }

    struct Host<'a> {
        store: &'a Store,
        fail: bool,
        dropped: Vec<String>,
        replayed: usize,
    }
    impl WriteHost for Host<'_> {
        type Error = rusqlite::Error;
        fn len(&self) -> usize {
            2
        }
        fn validate_sequences(&self) -> Result<(), Self::Error> {
            Ok(())
        }
        fn command(&self, index: usize) -> Result<Option<String>, Self::Error> {
            Ok(Some(format!("c{index}")))
        }
        fn account(&self, index: usize) -> Result<String, Self::Error> {
            Ok(format!("a{index}"))
        }
        fn load(&mut self, _: &Store, _: &str) -> Result<(), Self::Error> {
            Ok(())
        }
        fn encode(&self, index: usize) -> Result<StoredEvent, Self::Error> {
            Ok(StoredEvent {
                seq: 0,
                ts: "2026-09-24T14:30:00+00:00".into(),
                account: format!("a{index}"),
                kind: "CashFlow".into(),
                command: Some(format!("c{index}")),
                payload: "{}".into(),
                schema: 1,
            })
        }
        fn replay(&mut self, _: StoredRow) -> Result<(), Self::Error> {
            self.replayed += 1;
            Ok(())
        }
        fn apply(&mut self, index: usize, _: &StoredEvent) -> Result<(), Self::Error> {
            if self.fail && index == 1 {
                Err(rusqlite::Error::InvalidQuery)
            } else {
                Ok(())
            }
        }
        fn outbox(&self, _: usize, seq: i64) -> Result<(), Self::Error> {
            self.store.execute("INSERT INTO outbox(event_seq,destination,payload_json,status,created_at) VALUES(?,'sink','{}','PENDING','now')",
                                   &[Value::Integer(seq)]).map(|_|())
        }
        fn commit(&self) -> Result<(), Self::Error> {
            self.store.execute("COMMIT", &[]).map(|_| ())
        }
        fn rollback(&self) -> Result<(), Self::Error> {
            self.store.execute("ROLLBACK", &[]).map(|_| ())
        }
        fn drop_account(&mut self, account: &str) {
            self.dropped.push(account.into());
        }
        fn is_exception(&self, _: &Self::Error) -> bool {
            true
        }
    }

    #[test]
    fn event_outbox_batch_rollback_and_replay_share_connection() {
        let fixture = Synthetic::new();
        let store = fixture.open();
        let mut host = Host {
            store: &store,
            fail: true,
            dropped: vec![],
            replayed: 0,
        };
        assert!(store.write(&mut host).is_err());
        assert_eq!(store.count().unwrap(), 0);
        assert_eq!(host.dropped, ["a0", "a1"]);
        assert!(store
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .is_autocommit());
        host.fail = false;
        store.write(&mut host).unwrap();
        assert_eq!(store.count().unwrap(), 2);
        let count: i64 = store
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 2);
        store.write(&mut host).unwrap();
        assert_eq!(host.replayed, 2);
        assert_eq!(store.count().unwrap(), 2);
        store.close();
    }

    #[test]
    fn guarded_open_precedes_sql_and_readers_are_sqlite_readonly() {
        let fixture = Synthetic::new();
        let missing = fixture.0.join("unopened.db");
        let sidecar = fixture.0.join("held.lock");
        let guard = SingleInstanceGuard::acquire(&sidecar, "synthetic").unwrap();
        assert!(matches!(
            Store::open(&missing, &sidecar, "second"),
            Err(OpenError::Lock(_))
        ));
        assert!(!missing.exists());
        drop(guard);
        let store = fixture.open();
        let uri = format!(
            "file:{}?mode=ro",
            fixture
                .0
                .join("synthetic.db")
                .display()
                .to_string()
                .replace('\\', "/")
        );
        let reader = Store::reader(&uri).unwrap();
        assert!(reader
            .execute("INSERT INTO meta VALUES('x','y')", &[])
            .is_err());
        store.execute("BEGIN IMMEDIATE", &[]).unwrap();
        store
            .execute("INSERT INTO meta VALUES('x','y')", &[])
            .unwrap();
        let count: i64 = reader
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM meta", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
        store.execute("COMMIT", &[]).unwrap();
        let count: i64 = reader
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM meta", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        reader.close();
        store.close();
    }

    #[test]
    fn raw_history_types_are_not_silently_narrowed() {
        let fixture = Synthetic::new();
        let store = fixture.open();
        store.execute("INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES('x','a','k','{}','bad')",&[]).unwrap();
        assert_eq!(
            store.events(None, None, None, None).unwrap()[0].schema,
            Value::Text("bad".into())
        );
        store.close();
    }
}
pub enum OpenError {
    Lock(LockError),
    Sql(rusqlite::Error),
}

pub struct Store {
    pub connection: SharedConnection,
    pub guard: Arc<Mutex<Option<SingleInstanceGuard>>>,
}

#[derive(Clone, Debug)]
pub struct StoredEvent {
    pub seq: i64,
    pub ts: String,
    pub account: String,
    pub kind: String,
    pub command: Option<String>,
    pub payload: String,
    pub schema: i64,
}

pub struct StoredRow {
    pub seq: Value,
    pub ts: Value,
    pub account: Value,
    pub kind: Value,
    pub command: Value,
    pub payload: Value,
    pub schema: Value,
}

impl StoredRow {
    pub fn from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            seq: row.get("seq")?,
            ts: row.get("ts_utc")?,
            account: row.get("account")?,
            kind: row.get("kind")?,
            command: row.get("command_id")?,
            payload: row.get("payload_json")?,
            schema: row.get("schema_version")?,
        })
    }
}

/// Conversion/fold/effect adapters supplied by the one binding. The transaction
/// algorithm is native; outbox business sequencing remains the T5 Python adapter.
pub trait WriteHost {
    type Error: From<rusqlite::Error>;
    fn len(&self) -> usize;
    fn validate_sequences(&self) -> Result<(), Self::Error>;
    fn command(&self, index: usize) -> Result<Option<String>, Self::Error>;
    fn account(&self, index: usize) -> Result<String, Self::Error>;
    fn load(&mut self, store: &Store, account: &str) -> Result<(), Self::Error>;
    fn encode(&self, index: usize) -> Result<StoredEvent, Self::Error>;
    fn replay(&mut self, event: StoredRow) -> Result<(), Self::Error>;
    fn apply(&mut self, index: usize, event: &StoredEvent) -> Result<(), Self::Error>;
    fn outbox(&self, index: usize, seq: i64) -> Result<(), Self::Error>;
    fn commit(&self) -> Result<(), Self::Error>;
    fn rollback(&self) -> Result<(), Self::Error>;
    fn drop_account(&mut self, account: &str);
    fn is_exception(&self, error: &Self::Error) -> bool;
}

impl Store {
    pub fn open(path: &Path, sidecar: &Path, pid: &str) -> Result<Self, OpenError> {
        let guard = SingleInstanceGuard::acquire(sidecar, pid).map_err(OpenError::Lock)?;
        // This must remain after guard acquisition, including schema initialization.
        let conn = Connection::open(path).map_err(OpenError::Sql)?;
        conn.busy_timeout(std::time::Duration::from_secs(5))
            .map_err(OpenError::Sql)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;",
        )
        .map_err(OpenError::Sql)?;
        conn.execute_batch(SCHEMA).map_err(OpenError::Sql)?;
        Ok(Self {
            connection: Arc::new(Mutex::new(Some(conn))),
            guard: Arc::new(Mutex::new(Some(guard))),
        })
    }

    pub fn reader(uri: &str) -> rusqlite::Result<Self> {
        let conn = Connection::open_with_flags(
            uri,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        Ok(Self {
            connection: Arc::new(Mutex::new(Some(conn))),
            guard: Arc::new(Mutex::new(None)),
        })
    }

    pub fn close(&self) {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .take();
        self.guard.lock().expect("guard mutex poisoned").take();
    }

    pub fn held(&self) -> bool {
        self.guard.lock().expect("guard mutex poisoned").is_some()
    }

    pub fn execute(&self, sql: &str, params: &[Value]) -> rusqlite::Result<usize> {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .execute(sql, rusqlite::params_from_iter(params))
    }

    pub fn next_seq(&self) -> rusqlite::Result<i64> {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .query_row("SELECT COALESCE(MAX(seq), 0) + 1 FROM events", [], |r| {
                r.get(0)
            })
    }

    pub fn count(&self) -> rusqlite::Result<i64> {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
    }

    pub fn by_command(&self, command: &str) -> rusqlite::Result<Option<StoredRow>> {
        self.by_command_value(&Value::Text(command.into()))
    }
    pub fn by_command_value(&self, command: &Value) -> rusqlite::Result<Option<StoredRow>> {
        use rusqlite::OptionalExtension;
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .query_row(
                "SELECT * FROM events WHERE command_id = ?",
                [command],
                StoredRow::from_row,
            )
            .optional()
    }

    pub fn has_command(&self, command: &str) -> rusqlite::Result<bool> {
        self.has_command_value(&Value::Text(command.into()))
    }
    pub fn has_command_value(&self, command: &Value) -> rusqlite::Result<bool> {
        use rusqlite::OptionalExtension;
        Ok(self
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .query_row(
                "SELECT 1 FROM events WHERE command_id = ? LIMIT 1",
                [command],
                |r| r.get::<_, i64>(0),
            )
            .optional()?
            .is_some())
    }

    pub fn events(
        &self,
        after: Option<i64>,
        account: Option<&str>,
        kind: Option<&str>,
        through: Option<i64>,
    ) -> rusqlite::Result<Vec<StoredRow>> {
        let mut filters = Vec::new();
        for (column, op, value) in [
            ("seq", ">", after.map(Value::Integer)),
            ("account", "=", account.map(|s| Value::Text(s.into()))),
            ("kind", "=", kind.map(|s| Value::Text(s.into()))),
            ("seq", "<=", through.map(Value::Integer)),
        ] {
            if let Some(value) = value {
                filters.push((column, op, value));
            }
        }
        self.query_events(filters)
    }

    pub fn query_events(
        &self,
        filters: Vec<(&str, &str, Value)>,
    ) -> rusqlite::Result<Vec<StoredRow>> {
        let mut sql = String::from("SELECT * FROM events");
        let clauses: Vec<_> = filters
            .iter()
            .map(|(column, op, _)| format!("{column} {op} ?"))
            .collect();
        if !clauses.is_empty() {
            sql.push_str(&format!(" WHERE {}", clauses.join(" AND ")));
        }
        sql.push_str(" ORDER BY seq ASC");
        let params: Vec<_> = filters.into_iter().map(|(_, _, value)| value).collect();
        let conn = self.connection.lock().expect("connection mutex poisoned");
        let mut stmt = conn
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .prepare(&sql)?;
        let rows = stmt.query_map(rusqlite::params_from_iter(params), StoredRow::from_row)?;
        rows.collect()
    }

    pub fn accounts(&self) -> rusqlite::Result<Vec<Value>> {
        let conn = self.connection.lock().expect("connection mutex poisoned");
        let mut stmt = conn
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?
            .prepare("SELECT account FROM events GROUP BY account ORDER BY MIN(seq)")?;
        let rows = stmt.query_map([], |r| r.get(0))?;
        rows.collect()
    }

    pub fn write<H: WriteHost>(&self, host: &mut H) -> Result<(), H::Error> {
        host.validate_sequences()?;
        let mut touched = std::collections::BTreeSet::new();
        let mut in_transaction = false;
        let result = (|| {
            self.execute("BEGIN IMMEDIATE", &[])?;
            in_transaction = true;
            for index in 0..host.len() {
                if let Some(command) = host.command(index)? {
                    if let Some(event) = self.by_command(&command)? {
                        host.replay(event)?;
                        continue;
                    }
                }
                let account = host.account(index)?;
                host.load(self, &account)?;
                touched.insert(account);
                let mut event = host.encode(index)?;
                self.execute(
                    "INSERT INTO events (ts_utc, account, kind, command_id, payload_json, schema_version) VALUES (?, ?, ?, ?, ?, ?)",
                    &[Value::Text(event.ts.clone()), Value::Text(event.account.clone()),
                      Value::Text(event.kind.clone()), event.command.clone().map_or(Value::Null, Value::Text),
                      Value::Text(event.payload.clone()), Value::Integer(event.schema)],
                )?;
                event.seq = self
                    .connection
                    .lock()
                    .expect("connection mutex poisoned")
                    .as_ref()
                    .expect("open")
                    .last_insert_rowid();
                host.apply(index, &event)?;
                host.outbox(index, event.seq)?;
            }
            host.commit()?;
            in_transaction = false;
            Ok(())
        })();
        if let Err(error) = result {
            for account in touched {
                host.drop_account(&account);
            }
            // Preserve the existing BaseException contract: invalidate always,
            // but only Exception triggers rollback through the crash seam.
            if in_transaction && host.is_exception(&error) {
                host.rollback()?;
            }
            return Err(error);
        }
        Ok(())
    }
}
