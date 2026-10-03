//! Python's `decimal.Decimal` under the default context, as the ledger fold used it.
//!
//! D6 says money is `rust_decimal`. For the ledger it cannot be: the canonical state is
//! compared as strings, so the result of every operation must keep Python's exponent and
//! digit count, and `rust_decimal` differs from it on both (96-bit mantissa where Python
//! keeps 28 digits and rounds half-even; a scale where Python keeps an exponent, which
//! can be positive; `-0`; `NaN` and `Infinity` as values). `PyDec` reproduces the
//! default context: precision 28, `ROUND_HALF_EVEN`, `Emax` 999999, `Emin` -999999, with
//! `Overflow`, `InvalidOperation` and `DivisionByZero` trapped and every other signal
//! quiet. Each operation here is a port of the CPython `_pydecimal` algorithm the C
//! `decimal` module is specified to agree with, and `tests/test_ledger_fold_parity.py`
//! holds the two together.
//!
//! What is deliberately not reproduced (a result in the subnormal range, arithmetic on
//! `Infinity` beyond ordering and sign) raises `DecErr::Unsupported`, never a guess.

use std::cmp::Ordering;

use num_bigint::BigUint;
use num_traits::{ToPrimitive, Zero};

use crate::options::pyrules::{digit, strip};

pub const PREC: usize = 28;
pub const EMAX: i64 = 999_999;
/// `Emin - prec + 1`.
pub const ETINY: i64 = -999_999 - 27;
/// `Emax - prec + 1`.
pub const ETOP: i64 = EMAX - 27;
/// CPython's `MAX_EMAX`: a literal with a larger exponent is a conversion syntax error.
const MAX_LITERAL_EXP: i128 = 999_999_999_999_999_999;

/// A `decimal` signal the default context traps (or one this port does not model).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecErr {
    InvalidOperation,
    DivisionByZero,
    Overflow,
    Unsupported(&'static str),
}

pub type DecResult<T> = Result<T, DecErr>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DKind {
    Finite,
    Inf,
    QNan,
    SNan,
}

/// A Python `Decimal`: sign, coefficient and exponent, or a non-finite value.
#[derive(Debug, Clone)]
pub struct PyDec {
    kind: DKind,
    neg: bool,
    /// The digits; for a NaN, its payload.
    coef: BigUint,
    exp: i64,
}

fn pow10(n: usize) -> BigUint {
    BigUint::from(10u32).pow(n as u32)
}

fn ndigits(c: &BigUint) -> usize {
    if c.is_zero() {
        1
    } else {
        c.to_str_radix(10).len()
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Round {
    HalfEven,
    Floor,
}

/// Divide by `10^drop` rounding as asked; `neg` is the sign of the value (floor of a
/// negative rounds the magnitude up).
fn shift_round(c: &BigUint, drop: usize, neg: bool, mode: Round) -> BigUint {
    let div = pow10(drop);
    let q = c / &div;
    let r = c - &q * &div;
    let up = match mode {
        Round::HalfEven => {
            let twice = &r * 2u32;
            match twice.cmp(&div) {
                Ordering::Greater => true,
                Ordering::Equal => (&q % 2u32) == BigUint::from(1u32),
                Ordering::Less => false,
            }
        }
        Round::Floor => neg && !r.is_zero(),
    };
    if up {
        q + 1u32
    } else {
        q
    }
}

impl PyDec {
    pub fn zero() -> PyDec {
        PyDec { kind: DKind::Finite, neg: false, coef: BigUint::zero(), exp: 0 }
    }

    pub fn from_i128(n: i128) -> PyDec {
        PyDec { kind: DKind::Finite, neg: n < 0, coef: BigUint::from(n.unsigned_abs()), exp: 0 }
    }

    pub fn from_parts(neg: bool, coef: BigUint, exp: i64) -> PyDec {
        PyDec { kind: DKind::Finite, neg, coef, exp }
    }

    pub fn kind(&self) -> DKind {
        self.kind
    }
    pub fn is_finite(&self) -> bool {
        self.kind == DKind::Finite
    }
    pub fn is_nan(&self) -> bool {
        matches!(self.kind, DKind::QNan | DKind::SNan)
    }
    pub fn is_zero(&self) -> bool {
        self.kind == DKind::Finite && self.coef.is_zero()
    }
    pub fn is_negative(&self) -> bool {
        self.neg
    }
    pub fn exponent(&self) -> i64 {
        self.exp
    }
    pub fn coefficient(&self) -> &BigUint {
        &self.coef
    }

    /// `Decimal(text)`: whitespace stripped and underscores dropped, as CPython does.
    pub fn parse(text: &str) -> Option<PyDec> {
        let stripped = strip(text);
        let cleaned: String = stripped.chars().filter(|&c| c != '_').collect();
        let mut chars: Vec<char> = cleaned.chars().collect();
        let mut neg = false;
        if let Some(&c) = chars.first() {
            if c == '-' || c == '+' {
                neg = c == '-';
                chars.remove(0);
            }
        }
        let rest: String = chars.iter().collect();
        let lower = rest.to_lowercase();
        if lower == "inf" || lower == "infinity" {
            return Some(PyDec { kind: DKind::Inf, neg, coef: BigUint::zero(), exp: 0 });
        }
        let nan_body = lower.strip_prefix('s');
        for (body, kind) in [(Some(lower.as_str()), DKind::QNan), (nan_body, DKind::SNan)] {
            let Some(body) = body else { continue };
            if let Some(diag) = body.strip_prefix("nan") {
                let mut payload = BigUint::zero();
                for ch in diag.chars() {
                    let d = digit(ch)?;
                    payload = payload * 10u32 + d;
                }
                return Some(PyDec { kind, neg, coef: payload, exp: 0 });
            }
        }
        // digits [. digits] [E [sign] digits], at least one digit before or after the dot
        let mut i = 0;
        let mut int_digits: Vec<u32> = Vec::new();
        while i < chars.len() {
            match digit(chars[i]) {
                Some(d) => int_digits.push(d),
                None => break,
            }
            i += 1;
        }
        let mut frac_digits: Vec<u32> = Vec::new();
        let mut has_dot = false;
        if i < chars.len() && chars[i] == '.' {
            has_dot = true;
            i += 1;
            while i < chars.len() {
                match digit(chars[i]) {
                    Some(d) => frac_digits.push(d),
                    None => break,
                }
                i += 1;
            }
        }
        if int_digits.is_empty() && frac_digits.is_empty() {
            return None;
        }
        let _ = has_dot;
        let mut exp: i128 = 0;
        if i < chars.len() && (chars[i] == 'e' || chars[i] == 'E') {
            i += 1;
            let mut eneg = false;
            if i < chars.len() && (chars[i] == '-' || chars[i] == '+') {
                eneg = chars[i] == '-';
                i += 1;
            }
            let mut any = false;
            let mut mag: i128 = 0;
            while i < chars.len() {
                match digit(chars[i]) {
                    Some(d) => {
                        any = true;
                        mag = (mag * 10 + d as i128).min(i128::MAX / 20);
                    }
                    None => break,
                }
                i += 1;
            }
            if !any {
                return None;
            }
            exp = if eneg { -mag } else { mag };
        }
        if i != chars.len() {
            return None;
        }
        let all: Vec<u32> = int_digits.iter().chain(frac_digits.iter()).copied().collect();
        let mut coef = BigUint::zero();
        for d in &all {
            coef = coef * 10u32 + *d;
        }
        let total = exp - frac_digits.len() as i128;
        if total.abs() > MAX_LITERAL_EXP || exp.abs() > MAX_LITERAL_EXP {
            return None;
        }
        Some(PyDec { kind: DKind::Finite, neg, coef, exp: total as i64 })
    }

    /// `str(d)`.
    pub fn to_py_string(&self) -> String {
        let sign = if self.neg { "-" } else { "" };
        match self.kind {
            DKind::Inf => format!("{sign}Infinity"),
            DKind::QNan | DKind::SNan => {
                let tag = if self.kind == DKind::SNan { "sNaN" } else { "NaN" };
                let diag = if self.coef.is_zero() { String::new() } else { self.coef.to_str_radix(10) };
                format!("{sign}{tag}{diag}")
            }
            DKind::Finite => {
                let coef = self.coef.to_str_radix(10);
                let n = coef.len() as i64;
                let leftdigits = self.exp + n;
                let dotplace = if self.exp <= 0 && leftdigits > -6 { leftdigits } else { 1 };
                let (intpart, fracpart) = if dotplace <= 0 {
                    ("0".to_string(), format!(".{}{}", "0".repeat((-dotplace) as usize), coef))
                } else if dotplace >= n {
                    (format!("{}{}", coef, "0".repeat((dotplace - n) as usize)), String::new())
                } else {
                    (coef[..dotplace as usize].to_string(), format!(".{}", &coef[dotplace as usize..]))
                };
                let exp_str =
                    if leftdigits == dotplace { String::new() } else { format!("E{:+}", leftdigits - dotplace) };
                format!("{sign}{intpart}{fracpart}{exp_str}")
            }
        }
    }

    /// The context's `_fix`: round to 28 digits half-even and check the exponent range.
    pub fn fix(&self) -> DecResult<PyDec> {
        match self.kind {
            DKind::Inf | DKind::QNan | DKind::SNan => return Ok(self.clone()),
            DKind::Finite => {}
        }
        if self.coef.is_zero() {
            let exp = self.exp.clamp(ETINY, EMAX);
            return Ok(PyDec { kind: DKind::Finite, neg: self.neg, coef: BigUint::zero(), exp });
        }
        let n = ndigits(&self.coef) as i64;
        let mut exp_min = n + self.exp - PREC as i64;
        if exp_min > ETOP {
            return Err(DecErr::Overflow);
        }
        if exp_min < ETINY {
            return Err(DecErr::Unsupported("subnormal result"));
        }
        if self.exp < exp_min {
            let drop = (exp_min - self.exp) as usize;
            let mut coef = shift_round(&self.coef, drop, self.neg, Round::HalfEven);
            if ndigits(&coef) > PREC {
                coef /= 10u32;
                exp_min += 1;
            }
            if exp_min > ETOP {
                return Err(DecErr::Overflow);
            }
            return Ok(PyDec { kind: DKind::Finite, neg: self.neg, coef, exp: exp_min });
        }
        Ok(self.clone())
    }

    fn nonfinite_arith(&self, other: &PyDec) -> DecResult<()> {
        if self.kind == DKind::SNan || other.kind == DKind::SNan {
            return Err(DecErr::InvalidOperation);
        }
        Err(DecErr::Unsupported("arithmetic on a non-finite Decimal"))
    }

    pub fn neg(&self) -> DecResult<PyDec> {
        // `-x` rounds to the context, a zero keeps its sign flipped unless the result is
        // a zero from a non-zero (never here), so -0 -> 0 under half-even.
        if !self.is_finite() {
            if self.is_nan() {
                return Err(DecErr::Unsupported("negating a NaN"));
            }
            return Ok(PyDec { kind: DKind::Inf, neg: !self.neg, coef: BigUint::zero(), exp: 0 });
        }
        let mut d = self.clone();
        if d.coef.is_zero() {
            d.neg = false;
        } else {
            d.neg = !d.neg;
        }
        d.fix()
    }

    /// `abs(x)`: rounds to the context.
    pub fn abs(&self) -> DecResult<PyDec> {
        if self.is_nan() {
            return Err(DecErr::Unsupported("abs of a NaN"));
        }
        let mut d = self.clone();
        d.neg = false;
        d.fix()
    }

    fn aligned(a: &PyDec, b: &PyDec) -> (BigUint, BigUint, i64) {
        let exp = a.exp.min(b.exp);
        let ac = &a.coef * pow10((a.exp - exp) as usize);
        let bc = &b.coef * pow10((b.exp - exp) as usize);
        (ac, bc, exp)
    }

    pub fn add(&self, other: &PyDec) -> DecResult<PyDec> {
        if !self.is_finite() || !other.is_finite() {
            if self.kind == DKind::Inf && other.kind == DKind::Inf && self.neg != other.neg {
                return Err(DecErr::InvalidOperation);
            }
            if self.kind == DKind::Inf && other.is_finite() {
                return Ok(self.clone());
            }
            if other.kind == DKind::Inf && self.is_finite() {
                return Ok(other.clone());
            }
            if self.kind == DKind::Inf && other.kind == DKind::Inf {
                return Ok(self.clone());
            }
            return self.nonfinite_arith(other).map(|_| PyDec::zero());
        }
        let exp = self.exp.min(other.exp);
        if self.coef.is_zero() && other.coef.is_zero() {
            let neg = if self.neg != other.neg { false } else { self.neg };
            return PyDec { kind: DKind::Finite, neg, coef: BigUint::zero(), exp }.fix();
        }
        // a huge exponent gap would make the aligned integers enormous; the result is
        // still exact then rounded, so guard against absurd sizes only
        if (self.exp - other.exp).unsigned_abs() > 4_000_000 {
            return Err(DecErr::Unsupported("exponent gap in add"));
        }
        let (a, b, exp) = PyDec::aligned(self, other);
        let (neg, coef) = if self.neg == other.neg {
            (self.neg, a + b)
        } else {
            match a.cmp(&b) {
                Ordering::Equal => (false, BigUint::zero()),
                Ordering::Greater => (self.neg, a - b),
                Ordering::Less => (other.neg, b - a),
            }
        };
        PyDec { kind: DKind::Finite, neg, coef, exp }.fix()
    }

    pub fn sub(&self, other: &PyDec) -> DecResult<PyDec> {
        // `a - b` is `a + copy_negate(b)`: the negation is exact, never rounded.
        let mut n = other.clone();
        n.neg = !n.neg;
        self.add(&n)
    }

    pub fn mul(&self, other: &PyDec) -> DecResult<PyDec> {
        if !self.is_finite() || !other.is_finite() {
            if self.kind == DKind::Inf || other.kind == DKind::Inf {
                if (self.is_zero()) || other.is_zero() {
                    return Err(DecErr::InvalidOperation);
                }
                if self.is_finite() || other.is_finite() || (self.kind == DKind::Inf && other.kind == DKind::Inf) {
                    return Ok(PyDec {
                        kind: DKind::Inf,
                        neg: self.neg != other.neg,
                        coef: BigUint::zero(),
                        exp: 0,
                    });
                }
            }
            return self.nonfinite_arith(other).map(|_| PyDec::zero());
        }
        PyDec {
            kind: DKind::Finite,
            neg: self.neg != other.neg,
            coef: &self.coef * &other.coef,
            exp: self.exp + other.exp,
        }
        .fix()
    }

    pub fn div(&self, other: &PyDec) -> DecResult<PyDec> {
        if !self.is_finite() || !other.is_finite() {
            return self.nonfinite_arith(other).map(|_| PyDec::zero());
        }
        let neg = self.neg != other.neg;
        if other.coef.is_zero() {
            return Err(if self.coef.is_zero() { DecErr::InvalidOperation } else { DecErr::DivisionByZero });
        }
        if self.coef.is_zero() {
            let exp = (self.exp - other.exp).clamp(ETINY, EMAX);
            return Ok(PyDec { kind: DKind::Finite, neg, coef: BigUint::zero(), exp });
        }
        let shift = ndigits(&other.coef) as i64 - ndigits(&self.coef) as i64 + PREC as i64 + 1;
        let mut exp = self.exp - other.exp - shift;
        let (mut coef, rem) = if shift >= 0 {
            let num = &self.coef * pow10(shift as usize);
            let q = &num / &other.coef;
            let r = &num - &q * &other.coef;
            (q, r)
        } else {
            let den = &other.coef * pow10((-shift) as usize);
            let q = &self.coef / &den;
            let r = &self.coef - &q * &den;
            (q, r)
        };
        let ten = BigUint::from(10u32);
        let five = BigUint::from(5u32);
        if !rem.is_zero() {
            if (&coef % &five).is_zero() {
                coef += 1u32;
            }
        } else {
            let ideal = self.exp - other.exp;
            while exp < ideal && (&coef % &ten).is_zero() {
                coef /= 10u32;
                exp += 1;
            }
        }
        PyDec { kind: DKind::Finite, neg, coef, exp }.fix()
    }

    pub fn mul_i128(&self, n: i128) -> DecResult<PyDec> {
        self.mul(&PyDec::from_i128(n))
    }

    pub fn add_i128(&self, n: i128) -> DecResult<PyDec> {
        self.add(&PyDec::from_i128(n))
    }

    /// Ordering comparison (`<`, `<=`, `>`, `>=`): a NaN raises `InvalidOperation`.
    pub fn cmp_ord(&self, other: &PyDec) -> DecResult<Ordering> {
        if self.is_nan() || other.is_nan() {
            return Err(DecErr::InvalidOperation);
        }
        Ok(self.cmp_num(other))
    }

    /// Numeric order of two non-NaN values.
    fn cmp_num(&self, other: &PyDec) -> Ordering {
        let sa = if self.is_zero() { 0 } else if self.neg { -1 } else { 1 };
        let sb = if other.is_zero() { 0 } else if other.neg { -1 } else { 1 };
        if sa != sb {
            return sa.cmp(&sb);
        }
        if sa == 0 {
            return Ordering::Equal;
        }
        let mag = match (self.kind, other.kind) {
            (DKind::Inf, DKind::Inf) => Ordering::Equal,
            (DKind::Inf, _) => Ordering::Greater,
            (_, DKind::Inf) => Ordering::Less,
            _ => {
                let adj_a = self.exp + ndigits(&self.coef) as i64;
                let adj_b = other.exp + ndigits(&other.coef) as i64;
                if adj_a != adj_b {
                    adj_a.cmp(&adj_b)
                } else {
                    let (a, b, _) = PyDec::aligned(self, other);
                    a.cmp(&b)
                }
            }
        };
        if sa < 0 {
            mag.reverse()
        } else {
            mag
        }
    }

    /// `==`: numeric, a quiet NaN equals nothing, a signaling one raises.
    pub fn eq_num(&self, other: &PyDec) -> DecResult<bool> {
        if self.kind == DKind::SNan || other.kind == DKind::SNan {
            return Err(DecErr::InvalidOperation);
        }
        if self.is_nan() || other.is_nan() {
            return Ok(false);
        }
        Ok(self.cmp_num(other) == Ordering::Equal)
    }

    pub fn lt(&self, other: &PyDec) -> DecResult<bool> {
        Ok(self.cmp_ord(other)? == Ordering::Less)
    }
    pub fn le(&self, other: &PyDec) -> DecResult<bool> {
        Ok(self.cmp_ord(other)? != Ordering::Greater)
    }
    pub fn gt(&self, other: &PyDec) -> DecResult<bool> {
        Ok(self.cmp_ord(other)? == Ordering::Greater)
    }
    pub fn ge(&self, other: &PyDec) -> DecResult<bool> {
        Ok(self.cmp_ord(other)? != Ordering::Less)
    }

    /// `x > 0`, `x <= 0` and the like against a Python int.
    pub fn cmp_int(&self, n: i128) -> DecResult<Ordering> {
        self.cmp_ord(&PyDec::from_i128(n))
    }

    /// A key two numerically equal Decimals share: what `hash` and `==` agree on.
    pub fn num_key(&self) -> String {
        match self.kind {
            DKind::Inf => format!("{}inf", if self.neg { "-" } else { "" }),
            DKind::QNan | DKind::SNan => format!("nan:{}", self.to_py_string()),
            DKind::Finite => {
                if self.coef.is_zero() {
                    return "0".to_string();
                }
                let mut s = self.coef.to_str_radix(10);
                let mut e = self.exp;
                while s.ends_with('0') {
                    s.pop();
                    e += 1;
                }
                format!("{}{}e{}", if self.neg { "-" } else { "" }, s, e)
            }
        }
    }

    /// `d.to_integral_value(rounding=...)`: an integral value is returned as it is
    /// (a positive exponent stays), a fraction is rounded to exponent 0.
    pub fn to_integral(&self, mode: Round) -> DecResult<PyDec> {
        match self.kind {
            DKind::Inf => return Ok(self.clone()),
            DKind::QNan | DKind::SNan => return Err(DecErr::Unsupported("to_integral_value of a NaN")),
            DKind::Finite => {}
        }
        if self.exp >= 0 {
            return Ok(self.clone());
        }
        let drop = (-self.exp) as usize;
        if drop > 4_000_000 {
            return Err(DecErr::Unsupported("to_integral_value of a tiny exponent"));
        }
        let coef = shift_round(&self.coef, drop, self.neg, mode);
        Ok(PyDec { kind: DKind::Finite, neg: self.neg, coef, exp: 0 })
    }

    /// `d == d.to_integral_value()`.
    pub fn is_integral(&self) -> DecResult<bool> {
        let i = self.to_integral(Round::HalfEven)?;
        self.eq_num(&i)
    }

    /// `int(d)`: truncate toward zero.
    pub fn trunc_i128(&self) -> DecResult<i128> {
        if !self.is_finite() {
            return Err(DecErr::Unsupported("int() of a non-finite Decimal"));
        }
        let mag: BigUint = if self.exp >= 0 {
            if self.exp > 40 {
                return Err(DecErr::Unsupported("int() beyond i128"));
            }
            &self.coef * pow10(self.exp as usize)
        } else {
            let d = (-self.exp) as usize;
            &self.coef / pow10(d.min(5_000_000))
        };
        let m = mag.to_i128().ok_or(DecErr::Unsupported("int() beyond i128"))?;
        Ok(if self.neg { -m } else { m })
    }

    /// The value as an `i128` when it is an integer of that size (an int field).
    pub fn to_i128_exact(&self) -> Option<i128> {
        if !self.is_integral().ok()? || !self.is_finite() {
            return None;
        }
        self.trunc_i128().ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> PyDec {
        PyDec::parse(s).unwrap_or_else(|| panic!("parse {s}"))
    }
    fn s(x: DecResult<PyDec>) -> String {
        x.unwrap().to_py_string()
    }

    #[test]
    fn str_matches_python() {
        assert_eq!(d("1.50").to_py_string(), "1.50");
        assert_eq!(d("1E+2").to_py_string(), "1E+2");
        assert_eq!(d("0.000001").to_py_string(), "0.000001");
        assert_eq!(d("0.0000001").to_py_string(), "1E-7");
        assert_eq!(d("-0").to_py_string(), "-0");
        assert_eq!(d("0E+3").to_py_string(), "0E+3");
        assert_eq!(d("123E-9").to_py_string(), "1.23E-7");
        assert_eq!(d("-NaN12").to_py_string(), "-NaN12");
        assert_eq!(d("sNaN7").to_py_string(), "sNaN7");
        assert_eq!(d("-iNfinity").to_py_string(), "-Infinity");
        assert_eq!(d("1_0.5_0").to_py_string(), "10.50");
        assert_eq!(d("\u{661}\u{662}").to_py_string(), "12");
        assert_eq!(d(" 12 ").to_py_string(), "12");
        assert!(PyDec::parse("1e+").is_none() && PyDec::parse(".").is_none() && PyDec::parse("1 2").is_none());
        assert!(PyDec::parse("1e99999999999999999999").is_none());
    }

    #[test]
    fn arithmetic_keeps_python_exponents() {
        assert_eq!(s(d("1.10").add(&d("2.205"))), "3.305");
        assert_eq!(s(d("100.00").sub(&d("100"))), "0.00");
        assert_eq!(s(d("1.5").mul(&d("2.50"))), "3.750");
        assert_eq!(s(d("-1.5").add(&d("1.5"))), "0.0");
        assert_eq!(s(d("0").sub(&d("0"))), "0");
        assert_eq!(s(d("-0").add(&d("-0"))), "-0");
        assert_eq!(s(d("1").div(&d("3"))), "0.3333333333333333333333333333");
        assert_eq!(s(d("2").div(&d("3"))), "0.6666666666666666666666666667");
        assert_eq!(s(d("10").div(&d("4"))), "2.5");
        assert_eq!(s(d("100").div(&d("4"))), "25");
        assert_eq!(s(d("1.00").div(&d("4"))), "0.25");
        assert_eq!(s(d("1E+2").div(&d("4"))), "25");
        assert_eq!(s(d("0.00").div(&d("4"))), "0.00");
        assert_eq!(s(d("12345678901234567890123456789").add(&d("1"))), "1.234567890123456789012345679E+28");
        assert_eq!(s(d("12345678901234567890123456785").add(&d("0"))), "1.234567890123456789012345678E+28");
        assert_eq!(s(d("9999999999999999999999999999.5").add(&d("0"))), "1.000000000000000000000000000E+28");
        assert_eq!(s(d("-5").abs()), "5");
        assert_eq!(s(d("12345678901234567890123456789").abs()), "1.234567890123456789012345679E+28");
        assert_eq!(s(d("0.00").neg()), "0.00");
        assert_eq!(s(d("1.5").neg()), "-1.5");
    }

    #[test]
    fn compare_and_integral() {
        assert!(d("1.0").eq_num(&d("1.00")).unwrap());
        assert!(!d("NaN").eq_num(&d("NaN")).unwrap());
        assert_eq!(d("NaN").lt(&d("1")), Err(DecErr::InvalidOperation));
        assert!(d("-1").lt(&d("0")).unwrap());
        assert!(d("Infinity").gt(&d("1E+999999")).unwrap());
        assert_eq!(d("1E+2").num_key(), d("100").num_key());
        assert_eq!(s(d("2.5").to_integral(Round::HalfEven)), "2");
        assert_eq!(s(d("3.5").to_integral(Round::HalfEven)), "4");
        assert_eq!(s(d("2.5").to_integral(Round::Floor)), "2");
        assert_eq!(s(d("-2.5").to_integral(Round::Floor)), "-3");
        assert_eq!(s(d("1E+2").to_integral(Round::Floor)), "1E+2");
        assert_eq!(s(d("0.00").to_integral(Round::Floor)), "0");
        assert_eq!(d("-7.9").trunc_i128().unwrap(), -7);
    }
}
