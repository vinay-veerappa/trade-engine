//! Decimal operations in the form the fold uses them (every one can refuse, as a
//! `decimal` signal does in Python), and the insertion-ordered map the folded state is
//! built from.

use std::cmp::Ordering;
use std::collections::HashMap;

use super::model::{derr, R};
use super::pydec::PyDec;

pub fn zero() -> PyDec {
    PyDec::zero()
}

pub fn add(a: &PyDec, b: &PyDec) -> R<PyDec> {
    a.add(b).map_err(derr)
}

pub fn sub(a: &PyDec, b: &PyDec) -> R<PyDec> {
    a.sub(b).map_err(derr)
}

pub fn mul(a: &PyDec, b: &PyDec) -> R<PyDec> {
    a.mul(b).map_err(derr)
}

pub fn mul_i(a: &PyDec, n: i128) -> R<PyDec> {
    a.mul_i128(n).map_err(derr)
}

pub fn div(a: &PyDec, b: &PyDec) -> R<PyDec> {
    a.div(b).map_err(derr)
}

pub fn div_i(a: &PyDec, n: i128) -> R<PyDec> {
    a.div(&PyDec::from_i128(n)).map_err(derr)
}

pub fn neg(a: &PyDec) -> R<PyDec> {
    a.neg().map_err(derr)
}

pub fn abs(a: &PyDec) -> R<PyDec> {
    a.abs().map_err(derr)
}

pub fn cmp(a: &PyDec, b: &PyDec) -> R<Ordering> {
    a.cmp_ord(b).map_err(derr)
}

pub fn lt(a: &PyDec, b: &PyDec) -> R<bool> {
    a.lt(b).map_err(derr)
}

pub fn le(a: &PyDec, b: &PyDec) -> R<bool> {
    a.le(b).map_err(derr)
}

pub fn gt(a: &PyDec, b: &PyDec) -> R<bool> {
    a.gt(b).map_err(derr)
}

pub fn ge(a: &PyDec, b: &PyDec) -> R<bool> {
    a.ge(b).map_err(derr)
}

pub fn eq(a: &PyDec, b: &PyDec) -> R<bool> {
    a.eq_num(b).map_err(derr)
}

pub fn ne(a: &PyDec, b: &PyDec) -> R<bool> {
    eq(a, b).map(|e| !e)
}

/// `str(d)`.
pub fn s(d: &PyDec) -> String {
    d.to_py_string()
}

/// A dict that keeps insertion order, as Python's does: setting an existing key replaces
/// the value and keeps both the position and the FIRST key object (so two keys that are
/// equal but not identical, `Decimal("200")` and `Decimal("200.00")`, keep the first
/// spelling); popping removes the entry and a later insert goes to the end.
///
/// Keys are compared by `hk`, a string two keys share exactly when Python's `==` and
/// `hash` call them one key.
#[derive(Debug, Clone)]
pub struct OMap<K, V> {
    items: Vec<(String, K, V)>,
    index: HashMap<String, usize>,
}

impl<K, V> Default for OMap<K, V> {
    fn default() -> Self {
        OMap { items: Vec::new(), index: HashMap::new() }
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
            Some(&i) => Some(&mut self.items[i].2),
            None => None,
        }
    }

    pub fn contains(&self, hk: &str) -> bool {
        self.index.contains_key(hk)
    }

    pub fn insert(&mut self, hk: String, key: K, value: V) {
        match self.index.get(&hk) {
            Some(&i) => self.items[i].2 = value,
            None => {
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
        Some(v)
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
