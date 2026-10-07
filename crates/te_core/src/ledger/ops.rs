//! Decimal operations in the form the fold uses them (every one can refuse, as a
//! `decimal` signal does in Python), and the insertion-ordered map the folded state is
//! built from.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap};

use super::model::{derr, R};
use crate::money::Money;

pub fn zero() -> Money {
    Money::zero()
}

/// An integer as a `Money`, refusing past the bound.
pub fn dint(n: i128) -> R<Money> {
    Money::from_i128(n).map_err(derr)
}

pub fn add(a: &Money, b: &Money) -> R<Money> {
    a.add(b).map_err(derr)
}

pub fn sub(a: &Money, b: &Money) -> R<Money> {
    a.sub(b).map_err(derr)
}

pub fn mul(a: &Money, b: &Money) -> R<Money> {
    a.mul(b).map_err(derr)
}

pub fn mul_i(a: &Money, n: i128) -> R<Money> {
    a.mul_i128(n).map_err(derr)
}

pub fn div(a: &Money, b: &Money) -> R<Money> {
    a.div(b).map_err(derr)
}

pub fn div_i(a: &Money, n: i128) -> R<Money> {
    a.div(&dint(n)?).map_err(derr)
}

pub fn neg(a: &Money) -> R<Money> {
    a.neg().map_err(derr)
}

pub fn abs(a: &Money) -> R<Money> {
    a.abs().map_err(derr)
}

pub fn cmp(a: &Money, b: &Money) -> R<Ordering> {
    a.cmp_ord(b).map_err(derr)
}

pub fn lt(a: &Money, b: &Money) -> R<bool> {
    a.lt(b).map_err(derr)
}

pub fn le(a: &Money, b: &Money) -> R<bool> {
    a.le(b).map_err(derr)
}

pub fn gt(a: &Money, b: &Money) -> R<bool> {
    a.gt(b).map_err(derr)
}

pub fn ge(a: &Money, b: &Money) -> R<bool> {
    a.ge(b).map_err(derr)
}

pub fn eq(a: &Money, b: &Money) -> R<bool> {
    a.eq_num(b).map_err(derr)
}

pub fn ne(a: &Money, b: &Money) -> R<bool> {
    eq(a, b).map(|e| !e)
}

/// `str(d)`.
pub fn s(d: &Money) -> String {
    d.canon()
}

/// A dict that keeps insertion order, as Python's does: setting an existing key replaces
/// the value and keeps both the position and the FIRST key object (so two keys that are
/// equal but not identical, `Decimal("200")` and `Decimal("200.00")`, keep the first
/// spelling); popping removes the entry and a later insert goes to the end.
///
/// Keys are compared by `hk`, a string two keys share exactly when Python's `==` and
/// `hash` call them one key.
///
/// The map keeps a journal of what changed since the last [`OMap::take_journal`] (P2b):
/// the positions written (an insert, or a `get_mut` that may have written), or that the
/// map was `rebuilt` (a removal shifts every later position). The incremental export to
/// Python reads it, so a read after an append ships the touched entries, not the map.
#[derive(Debug, Clone)]
pub struct OMap<K, V> {
    items: Vec<(String, K, V)>,
    index: HashMap<String, usize>,
    dirty: BTreeSet<usize>,
    rebuilt: bool,
}

impl<K, V> Default for OMap<K, V> {
    fn default() -> Self {
        OMap { items: Vec::new(), index: HashMap::new(), dirty: BTreeSet::new(), rebuilt: false }
    }
}

impl<K, V> OMap<K, V> {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn get(&self, hk: &str) -> Option<&V> {
        self.index.get(hk).map(|&i| &self.items[i].2)
    }

    pub fn get_mut(&mut self, hk: &str) -> Option<&mut V> {
        match self.index.get(hk) {
            Some(&i) => {
                self.dirty.insert(i);
                Some(&mut self.items[i].2)
            }
            None => None,
        }
    }

    pub fn contains(&self, hk: &str) -> bool {
        self.index.contains_key(hk)
    }

    pub fn insert(&mut self, hk: String, key: K, value: V) {
        match self.index.get(&hk) {
            Some(&i) => {
                self.dirty.insert(i);
                self.items[i].2 = value;
            }
            None => {
                self.dirty.insert(self.items.len());
                self.index.insert(hk.clone(), self.items.len());
                self.items.push((hk, key, value));
            }
        }
    }

    pub fn remove(&mut self, hk: &str) -> Option<V> {
        let i = self.index.remove(hk)?;
        let (_, _, v) = self.items.remove(i);
        for (_, slot) in self.index.iter_mut() {
            if *slot > i {
                *slot -= 1;
            }
        }
        self.rebuilt = true;
        self.dirty.clear();
        Some(v)
    }

    /// What changed since the last call, and reset: `(rebuilt, written positions)`. A
    /// rebuilt map must be shipped whole; otherwise the positions, ascending, are every
    /// entry that may differ (a new key's position is past every old one).
    pub fn take_journal(&mut self) -> (bool, Vec<usize>) {
        let rebuilt = std::mem::take(&mut self.rebuilt);
        let dirty = std::mem::take(&mut self.dirty);
        (rebuilt, if rebuilt { Vec::new() } else { dirty.into_iter().collect() })
    }

    /// The entry at a position (a journal position).
    pub fn entry_at(&self, i: usize) -> (&K, &V) {
        let (_, k, v) = &self.items[i];
        (k, v)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&K, &V)> {
        self.items.iter().map(|(_, k, v)| (k, v))
    }

    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.items.iter().map(|(_, _, v)| v)
    }
}

impl<V> OMap<String, V> {
    pub fn put(&mut self, key: &str, value: V) {
        self.insert(key.to_string(), key.to_string(), value);
    }
}
