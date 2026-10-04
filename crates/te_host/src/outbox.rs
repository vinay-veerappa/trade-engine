//! Outbox SQL and FIFO delivery on the event owner's existing connection.
use crate::store::{sql, Store};
use sql::{types::Value, OptionalExtension};

pub struct OutboxRow {
    pub id: Value,
    pub event_seq: Value,
    pub destination: Value,
    pub payload: Value,
    pub status: Value,
    pub attempts: Value,
    pub created_at: Value,
    pub last_error: Value,
    pub delivered_at: Value,
}

impl OutboxRow {
    fn from_row(row: &sql::Row<'_>) -> sql::Result<Self> {
        Ok(Self {
            id: row.get("id")?,
            event_seq: row.get("event_seq")?,
            destination: row.get("destination")?,
            payload: row.get("payload_json")?,
            status: row.get("status")?,
            attempts: row.get("attempts")?,
            created_at: row.get("created_at")?,
            last_error: row.get("last_error")?,
            delivered_at: row.get("delivered_at")?,
        })
    }
}

pub trait TransactionHost {
    type Error: From<sql::Error>;
    fn commit(&self) -> Result<(), Self::Error>;
    fn rollback(&self) -> Result<(), Self::Error>;
    fn is_exception(&self, error: &Self::Error) -> bool;
}

impl Store {
    pub fn outbox_transaction<H, T>(
        &self,
        host: &H,
        action: impl FnOnce() -> Result<T, H::Error>,
    ) -> Result<T, H::Error>
    where
        H: TransactionHost,
    {
        let mut begun = false;
        let result = (|| {
            self.execute("BEGIN IMMEDIATE", &[])?;
            begun = true;
            let value = action()?;
            host.commit()?;
            begun = false;
            Ok(value)
        })();
        if let Err(error) = result {
            if begun && host.is_exception(&error) {
                host.rollback()?;
            }
            return Err(error);
        }
        result
    }

    pub fn event_timestamp(&self, seq: &Value) -> sql::Result<Option<Value>> {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(sql::Error::InvalidQuery)?
            .query_row("SELECT ts_utc FROM events WHERE seq = ?", [seq], |r| {
                r.get(0)
            })
            .optional()
    }

    pub fn insert_outbox(
        &self,
        seq: Value,
        destination: Value,
        payload: Value,
        created_at: Value,
    ) -> sql::Result<i64> {
        let conn = self.connection.lock().expect("connection mutex poisoned");
        let conn = conn.as_ref().ok_or(sql::Error::InvalidQuery)?;
        conn.execute(
            "INSERT INTO outbox (event_seq, destination, payload_json, status, attempts, created_at) \
             VALUES (?, ?, ?, 'PENDING', 0, ?)",
            sql::params![seq, destination, payload, created_at],
        )?;
        Ok(conn.last_insert_rowid())
    }

    pub fn pending_outbox(
        &self,
        destination: Option<Value>,
        include_failed: bool,
    ) -> sql::Result<Vec<OutboxRow>> {
        let mut query = String::from("SELECT * FROM outbox WHERE ");
        let mut params = Vec::new();
        if let Some(destination) = destination {
            query.push_str("destination = ? AND ");
            params.push(destination);
        }
        if include_failed {
            query.push_str("status != 'DELIVERED'");
        } else {
            query.push_str("status = 'PENDING'");
        }
        query.push_str(" ORDER BY id ASC");
        let conn = self.connection.lock().expect("connection mutex poisoned");
        let mut statement = conn
            .as_ref()
            .ok_or(sql::Error::InvalidQuery)?
            .prepare(&query)?;
        let rows = statement.query_map(sql::params_from_iter(params), OutboxRow::from_row)?;
        rows.collect()
    }

    pub fn deliver_outbox(&self, id: Value, timestamp: Value) -> sql::Result<()> {
        self.execute(
            "UPDATE outbox SET status = 'DELIVERED', delivered_at = ? WHERE id = ?",
            &[timestamp, id],
        )?;
        Ok(())
    }

    pub fn fail_outbox(&self, id: Value, error: Value) -> sql::Result<()> {
        self.execute(
            "UPDATE outbox SET status = 'FAILED', attempts = attempts + 1, last_error = ? WHERE id = ?",
            &[error, id],
        )?;
        Ok(())
    }

    pub fn set_meta(&self, key: Value, value: Value) -> sql::Result<()> {
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            &[key, value],
        )?;
        Ok(())
    }

    pub fn get_meta(&self, key: Value) -> sql::Result<Option<Value>> {
        self.connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .ok_or(sql::Error::InvalidQuery)?
            .query_row("SELECT value FROM meta WHERE key = ?", [key], |r| r.get(0))
            .optional()
    }
}

pub trait DrainHost {
    type Item;
    type Error;
    fn pending(&self) -> Result<Vec<Self::Item>, Self::Error>;
    fn publish(&self, item: &Self::Item) -> Result<bool, Self::Error>;
    fn delivered(&self, item: &Self::Item) -> Result<(), Self::Error>;
    fn failed(&self, item: &Self::Item, error: &str) -> Result<Self::Item, Self::Error>;
    fn exception_message(&self, error: &Self::Error) -> Result<Option<String>, Self::Error>;
    fn destination(&self) -> Result<String, Self::Error>;
}

pub struct Drained<T> {
    pub count: usize,
    pub failed: Option<T>,
    pub error: Option<String>,
    pub remaining: usize,
}

pub fn drain<H: DrainHost>(host: &H) -> Result<Drained<H::Item>, H::Error> {
    let pending = host.pending()?;
    let mut result = Drained {
        count: 0,
        failed: None,
        error: None,
        remaining: 0,
    };
    for item in pending {
        // The oracle catches delivery, clock, mark and replacement errors together.
        let attempt = (|| {
            if host.publish(&item)? {
                host.delivered(&item)?;
                Ok(None)
            } else {
                let message = format!("Delivery unconfirmed by sink {}", host.destination()?);
                Ok(Some((host.failed(&item, &message)?, message)))
            }
        })();
        let failure = match attempt {
            Ok(value) => value,
            Err(error) => {
                let Some(detail) = host.exception_message(&error)? else {
                    return Err(error);
                };
                let message = format!("Sink {} raised: {detail}", host.destination()?);
                Some((host.failed(&item, &message)?, message))
            }
        };
        if let Some((item, error)) = failure {
            result.failed = Some(item);
            result.error = Some(error);
            break;
        }
        result.count += 1;
    }
    result.remaining = host.pending()?.len();
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    fn book() -> Store {
        let connection = sql::Connection::open_in_memory().unwrap();
        connection.execute_batch(crate::store::SCHEMA).unwrap();
        connection.execute_batch("PRAGMA foreign_keys=ON; INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES('ts','a','CashFlow','{}',1);").unwrap();
        Store {
            connection: std::sync::Arc::new(std::sync::Mutex::new(Some(connection))),
            guard: std::sync::Arc::new(std::sync::Mutex::new(None)),
        }
    }
    #[test]
    fn sql_defaults_filters_constraints_and_meta_updates() {
        let store = book();
        let a = store
            .insert_outbox(
                Value::Integer(1),
                Value::Text("a".into()),
                Value::Text("{}".into()),
                Value::Text("ts".into()),
            )
            .unwrap();
        let b = store
            .insert_outbox(
                Value::Integer(1),
                Value::Text("b".into()),
                Value::Text("{}".into()),
                Value::Text("ts".into()),
            )
            .unwrap();
        assert!(store
            .insert_outbox(
                Value::Integer(1),
                Value::Text("a".into()),
                Value::Text("{}".into()),
                Value::Text("ts".into())
            )
            .is_err());
        assert!(store
            .insert_outbox(
                Value::Integer(99),
                Value::Text("a".into()),
                Value::Text("{}".into()),
                Value::Text("ts".into())
            )
            .is_err());
        store
            .fail_outbox(Value::Integer(a), Value::Text("error".into()))
            .unwrap();
        store
            .fail_outbox(Value::Integer(a), Value::Text("again".into()))
            .unwrap();
        let pending = store.pending_outbox(None, true).unwrap();
        assert_eq!(
            pending.iter().map(|r| r.id.clone()).collect::<Vec<_>>(),
            [Value::Integer(a), Value::Integer(b)]
        );
        assert_eq!(pending[0].attempts, Value::Integer(2));
        assert_eq!(pending[0].last_error, Value::Text("again".into()));
        assert_eq!(store.pending_outbox(None, false).unwrap().len(), 1);
        store
            .deliver_outbox(Value::Integer(a), Value::Text("done".into()))
            .unwrap();
        assert_eq!(
            store
                .pending_outbox(Some(Value::Text("a".into())), true)
                .unwrap()
                .len(),
            0
        );
        assert_eq!(store.get_meta(Value::Text("missing".into())).unwrap(), None);
        store
            .set_meta(Value::Text("key".into()), Value::Text("old".into()))
            .unwrap();
        store
            .set_meta(Value::Text("key".into()), Value::Text("new".into()))
            .unwrap();
        assert_eq!(
            store.get_meta(Value::Text("key".into())).unwrap(),
            Some(Value::Text("new".into()))
        );
    }
    struct Transaction<'a> {
        store: &'a Store,
        fail: bool,
    }
    impl TransactionHost for Transaction<'_> {
        type Error = sql::Error;
        fn commit(&self) -> sql::Result<()> {
            if self.fail {
                Err(sql::Error::InvalidQuery)
            } else {
                self.store.execute("COMMIT", &[]).map(|_| ())
            }
        }
        fn rollback(&self) -> sql::Result<()> {
            self.store.execute("ROLLBACK", &[]).map(|_| ())
        }
        fn is_exception(&self, _: &sql::Error) -> bool {
            true
        }
    }
    #[test]
    fn meta_transaction_rollback_and_retry() {
        let store = book();
        let mut host = Transaction {
            store: &store,
            fail: true,
        };
        let action = || store.set_meta(Value::Text("key".into()), Value::Text("new".into()));
        assert!(store.outbox_transaction(&host, action).is_err());
        assert_eq!(store.get_meta(Value::Text("key".into())).unwrap(), None);
        host.fail = false;
        store.outbox_transaction(&host, action).unwrap();
        assert_eq!(
            store.get_meta(Value::Text("key".into())).unwrap(),
            Some(Value::Text("new".into()))
        );
    }
    struct Host {
        pending: RefCell<Vec<usize>>,
        seen: RefCell<Vec<usize>>,
        fail: bool,
    }
    impl DrainHost for Host {
        type Item = usize;
        type Error = String;
        fn pending(&self) -> Result<Vec<usize>, String> {
            Ok(self.pending.borrow().clone())
        }
        fn publish(&self, item: &usize) -> Result<bool, String> {
            self.seen.borrow_mut().push(*item);
            Ok(!(self.fail && *item == 2))
        }
        fn delivered(&self, item: &usize) -> Result<(), String> {
            self.pending.borrow_mut().retain(|x| x != item);
            Ok(())
        }
        fn failed(&self, item: &usize, _: &str) -> Result<usize, String> {
            Ok(*item)
        }
        fn exception_message(&self, error: &String) -> Result<Option<String>, String> {
            Ok(Some(error.clone()))
        }
        fn destination(&self) -> Result<String, String> {
            Ok("sink".into())
        }
    }
    #[test]
    fn fifo_failure_and_retry() {
        let mut host = Host {
            pending: RefCell::new(vec![1, 2, 3]),
            seen: RefCell::new(vec![]),
            fail: true,
        };
        let first = drain(&host).unwrap();
        assert_eq!(
            (first.count, first.failed, first.remaining),
            (1, Some(2), 2)
        );
        assert_eq!(host.seen.borrow().as_slice(), [1, 2]);
        host.fail = false;
        let second = drain(&host).unwrap();
        assert_eq!(
            (second.count, second.failed, second.remaining),
            (2, None, 0)
        );
        assert_eq!(host.seen.borrow().as_slice(), [1, 2, 2, 3]);
    }
}
