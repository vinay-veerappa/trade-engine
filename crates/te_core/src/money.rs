//! Money (docs/RUST_PORT.md D6 and P7): a `rust_decimal::Decimal` that is always held in
//! its one canonical form, with checked arithmetic that refuses what it cannot hold.
//!
//! **The spelling (S1).** The exact value in plain notation: no exponent, no `+`, no
//! trailing zeros after the point, no point on an integer, `0` for every zero. The value
//! must be `m / 10^s` with `m < 2^96` and `s <= 28` (`rust_decimal`'s limits) once its
//! trailing zeros are gone. A value outside that is REFUSED (`DecErr::Overflow`, I5): it is
//! never rounded, truncated or saturated. `NaN` and `Infinity` do not exist here.
//!
//! **The arithmetic.** Every operation computes the exact result, rounds it to 28
//! significant digits half-even (the default `decimal` context, so a quotient is
//! value-equal with the one the Python clients and the frozen oracles compute), and only
//! then applies the bound. `rust_decimal`'s own `checked_add`/`checked_mul` are not the
//! result: they round a wide result to a smaller scale without saying so. The proptests
//! at the end hold this to `ledger::pydec::PyDec` (the test-only oracle) over every
//! operation the engine uses.

use std::cmp::Ordering;
use std::fmt;

use num_bigint::BigUint;
use num_traits::{ToPrimitive, Zero};
use rust_decimal::Decimal;

use crate::options::pyrules::{digit, strip};

/// Significant digits an arithmetic result keeps (Python's default context).
pub const PREC: usize = 28;
/// `rust_decimal`'s largest scale.
pub const MAX_SCALE: u32 = 28;
/// A mantissa must be below `2^96`.
pub const MANTISSA_LIMIT: u128 = 1u128 << 96;
/// The exponent limits a quantize target may carry (the `decimal` default context's).
pub const ETINY: i64 = -999_999 - 27;
pub const EMAX: i64 = 999_999;
/// CPython's `MAX_EMAX`: a literal with a larger exponent is a conversion syntax error.
const MAX_LITERAL_EXP: i128 = 999_999_999_999_999_999;

/// A refusal. The first three are the `decimal` signals the old arithmetic raised;
/// `Overflow` is also what a value outside the bound raises.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecErr {
    InvalidOperation,
    DivisionByZero,
    Overflow,
    Unsupported(&'static str),
}

pub type DecResult<T> = Result<T, DecErr>;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Round {
    HalfEven,
    Floor,
}

const POW10: [u128; 39] = {
    let mut t = [1u128; 39];
    let mut i = 1;
    while i < 39 {
        t[i] = t[i - 1] * 10;
        i += 1;
    }
    t
};

fn pow10_big(n: usize) -> BigUint {
    BigUint::from(10u32).pow(n as u32)
}

fn digits_u128(n: u128) -> usize {
    if n == 0 {
        1
    } else {
        (n.ilog10() + 1) as usize
    }
}

fn digits_big(c: &BigUint) -> usize {
    if c.is_zero() {
        1
    } else {
        c.to_str_radix(10).len()
    }
}

/// `c / 10^drop` rounded as asked; `neg` is the sign of the value (a floor of a negative
/// rounds the magnitude up).
fn shift_round_u128(c: u128, drop: usize, neg: bool, mode: Round) -> u128 {
    if drop > 38 {
        return match mode {
            Round::HalfEven => 0,
            Round::Floor => u128::from(neg && c != 0),
        };
    }
    let div = POW10[drop];
    let q = c / div;
    let r = c % div;
    let up = match mode {
        Round::HalfEven => match (r.checked_mul(2)).map(|t| t.cmp(&div)) {
            Some(Ordering::Greater) | None => true,
            Some(Ordering::Equal) => q % 2 == 1,
            Some(Ordering::Less) => false,
        },
        Round::Floor => neg && r != 0,
    };
    q + u128::from(up)
}

fn shift_round_big(c: &BigUint, drop: usize, neg: bool, mode: Round) -> BigUint {
    let div = pow10_big(drop);
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

/// An exact value: `(-1)^neg * coef * 10^exp`.
#[derive(Clone)]
struct Exact {
    neg: bool,
    coef: BigUint,
    exp: i64,
}

/// A decimal in its canonical form (S1). Equality, order and hash are by value.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Money(Decimal);

impl fmt::Debug for Money {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Money({})", self.canon())
    }
}

impl fmt::Display for Money {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.canon())
    }
}

/// Round an exact `(coef, exp)` to 28 significant digits half-even (the context's `_fix`
/// on a finite value), then apply the bound.
fn finish_big(neg: bool, coef: BigUint, exp: i64) -> DecResult<Money> {
    if coef.is_zero() {
        return Ok(Money::ZERO);
    }
    let n = digits_big(&coef);
    if n <= PREC {
        return finish_u128(neg, coef.to_u128().expect("28 digits fit"), exp);
    }
    let drop = n - PREC;
    let mut rounded = shift_round_big(&coef, drop, neg, Round::HalfEven);
    let mut exp = exp + drop as i64;
    if digits_big(&rounded) > PREC {
        rounded /= 10u32;
        exp += 1;
    }
    finish_u128(neg, rounded.to_u128().expect("28 digits fit"), exp)
}

fn finish_u128(neg: bool, coef: u128, exp: i64) -> DecResult<Money> {
    if coef == 0 {
        return Ok(Money::ZERO);
    }
    let n = digits_u128(coef);
    let (coef, exp) = if n > PREC {
        let drop = n - PREC;
        let mut r = shift_round_u128(coef, drop, neg, Round::HalfEven);
        let mut e = exp + drop as i64;
        if digits_u128(r) > PREC {
            r /= 10;
            e += 1;
        }
        (r, e)
    } else {
        (coef, exp)
    };
    Money::build(neg, coef, exp)
}

impl Money {
    pub const ZERO: Money = Money(Decimal::ZERO);

    /// `neg`, `coef` and `exp` as an exact value, or a refusal outside the bound. No
    /// rounding: the coefficient is the value's.
    fn build(neg: bool, mut coef: u128, mut exp: i64) -> DecResult<Money> {
        if coef == 0 {
            return Ok(Money::ZERO);
        }
        while coef % 10 == 0 {
            coef /= 10;
            exp += 1;
        }
        if exp > 0 {
            if exp > 38 {
                return Err(DecErr::Overflow);
            }
            coef = coef.checked_mul(POW10[exp as usize]).ok_or(DecErr::Overflow)?;
            exp = 0;
        }
        let scale = -exp;
        if scale > MAX_SCALE as i64 || coef >= MANTISSA_LIMIT {
            return Err(DecErr::Overflow);
        }
        let signed = if neg { -(coef as i128) } else { coef as i128 };
        Ok(Money(Decimal::from_i128_with_scale(signed, scale as u32)))
    }

    fn build_big(neg: bool, coef: &BigUint, exp: i64) -> DecResult<Money> {
        // exact, no rounding: the value must fit as it is
        if coef.is_zero() {
            return Ok(Money::ZERO);
        }
        let ten = BigUint::from(10u32);
        let mut c = coef.clone();
        let mut e = exp;
        while (&c % &ten).is_zero() {
            c /= 10u32;
            e += 1;
        }
        let c = c.to_u128().ok_or(DecErr::Overflow)?;
        Money::build(neg, c, e)
    }

    pub fn zero() -> Money {
        Money::ZERO
    }

    /// A small integer; never refuses.
    pub fn int(n: i64) -> Money {
        Money(Decimal::from(n))
    }

    /// An integer, or a refusal beyond the bound.
    pub fn from_i128(n: i128) -> DecResult<Money> {
        Money::build(n < 0, n.unsigned_abs(), 0)
    }

    /// The canonical form of a `rust_decimal` value (always in bound).
    pub fn from_decimal(d: Decimal) -> Money {
        if d.is_zero() {
            Money::ZERO
        } else {
            Money(d.normalize())
        }
    }

    pub fn to_decimal(&self) -> Decimal {
        self.0
    }

    // --- parts -----------------------------------------------------------------------

    fn parts(&self) -> (bool, u128, i64) {
        (self.0.is_sign_negative() && !self.0.is_zero(), self.0.mantissa().unsigned_abs(), -(self.0.scale() as i64))
    }

    fn exact(&self) -> Exact {
        let (neg, m, exp) = self.parts();
        Exact { neg, coef: BigUint::from(m), exp }
    }

    /// A `Money` is always a finite number (NaN and Infinity are refused at parse, I5); kept so
    /// the finite-checks ported from the Python code read the same.
    pub fn is_finite(&self) -> bool {
        true
    }

    pub fn is_nan(&self) -> bool {
        false
    }

    pub fn is_zero(&self) -> bool {
        self.0.is_zero()
    }

    pub fn is_negative(&self) -> bool {
        self.0.is_sign_negative() && !self.0.is_zero()
    }

    /// The canonical coefficient (the digits of the mantissa).
    pub fn coefficient(&self) -> BigUint {
        BigUint::from(self.0.mantissa().unsigned_abs())
    }

    /// The canonical exponent: `-scale`, never positive.
    pub fn exponent(&self) -> i64 {
        -(self.0.scale() as i64)
    }

    pub fn scale(&self) -> u32 {
        self.0.scale()
    }

    // --- spelling --------------------------------------------------------------------

    /// The canonical text (S1).
    pub fn canon(&self) -> String {
        let (neg, m, _) = self.parts();
        let scale = self.0.scale() as usize;
        let digits = m.to_string();
        let mut out = String::with_capacity(digits.len() + 3);
        if neg {
            out.push('-');
        }
        if scale == 0 {
            out.push_str(&digits);
        } else if digits.len() > scale {
            out.push_str(&digits[..digits.len() - scale]);
            out.push('.');
            out.push_str(&digits[digits.len() - scale..]);
        } else {
            out.push_str("0.");
            for _ in 0..scale - digits.len() {
                out.push('0');
            }
            out.push_str(&digits);
        }
        out
    }

    /// Plain digits, never an exponent: the canonical text.
    pub fn format_f(&self) -> String {
        self.canon()
    }

    /// A key two equal values share (the canonical text).
    pub fn num_key(&self) -> String {
        self.canon()
    }

    // --- parsing ---------------------------------------------------------------------

    /// `Decimal(text)` read as a finite literal (whitespace stripped, underscores
    /// dropped, any Unicode decimal digit, an exponent), or `None` when it is not one or
    /// does not fit the bound. `NaN` and `Infinity` are not literals here.
    pub fn parse(text: &str) -> Option<Money> {
        Money::try_parse(text).ok()
    }

    /// As [`Money::parse`], telling a syntax error (`InvalidOperation`, what `decimal`
    /// raises) from a value outside the bound (`Overflow`).
    pub fn try_parse(text: &str) -> DecResult<Money> {
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
        let mut i = 0;
        let mut all: Vec<u32> = Vec::new();
        let mut int_len = 0usize;
        while i < chars.len() {
            match digit(chars[i]) {
                Some(d) => all.push(d),
                None => break,
            }
            i += 1;
        }
        int_len = int_len.max(all.len());
        let mut frac_len = 0usize;
        if i < chars.len() && chars[i] == '.' {
            i += 1;
            while i < chars.len() {
                match digit(chars[i]) {
                    Some(d) => {
                        all.push(d);
                        frac_len += 1;
                    }
                    None => break,
                }
                i += 1;
            }
        }
        if int_len == 0 && frac_len == 0 {
            return Err(DecErr::InvalidOperation);
        }
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
                return Err(DecErr::InvalidOperation);
            }
            exp = if eneg { -mag } else { mag };
        }
        if i != chars.len() {
            return Err(DecErr::InvalidOperation);
        }
        let total = exp - frac_len as i128;
        if total.abs() > MAX_LITERAL_EXP || exp.abs() > MAX_LITERAL_EXP {
            return Err(DecErr::InvalidOperation);
        }
        // strip leading zeros, then trailing zeros: only the significant digits matter
        let first = all.iter().position(|&d| d != 0);
        let Some(first) = first else { return Ok(Money::ZERO) };
        let mut sig: &[u32] = &all[first..];
        let mut e = total;
        while let Some((&0, rest)) = sig.split_last() {
            sig = rest;
            e += 1;
        }
        if sig.len() > 29 || e > 38 || e < -(MAX_SCALE as i128) - 29 {
            return Err(DecErr::Overflow);
        }
        let mut coef: u128 = 0;
        for &d in sig {
            coef = coef * 10 + d as u128;
        }
        Money::build(neg, coef, e as i64)
    }

    // --- arithmetic ------------------------------------------------------------------

    /// `-x`: rounded to the context, as `decimal` does (a 29-digit value loses its last).
    pub fn neg(&self) -> DecResult<Money> {
        let (neg, m, e) = self.parts();
        finish_u128(!neg && m != 0, m, e)
    }

    /// `abs(x)`: rounded to the context.
    pub fn abs(&self) -> DecResult<Money> {
        let (_, m, e) = self.parts();
        finish_u128(false, m, e)
    }

    pub fn add(&self, other: &Money) -> DecResult<Money> {
        Money::add_signed(self, other, false)
    }

    pub fn sub(&self, other: &Money) -> DecResult<Money> {
        Money::add_signed(self, other, true)
    }

    fn add_signed(a: &Money, b: &Money, negate_b: bool) -> DecResult<Money> {
        let (na, ma, ea) = a.parts();
        let (mut nb, mb, eb) = b.parts();
        nb ^= negate_b && mb != 0;
        let e = ea.min(eb);
        let fast = POW10
            .get((ea - e) as usize)
            .and_then(|fa| ma.checked_mul(*fa))
            .zip(POW10.get((eb - e) as usize).and_then(|fb| mb.checked_mul(*fb)));
        if let Some((x, y)) = fast {
            let (neg, mag) = if na == nb {
                match x.checked_add(y) {
                    Some(s) => (na, s),
                    None => return Money::add_big(a, b, negate_b),
                }
            } else if x >= y {
                (na, x - y)
            } else {
                (nb, y - x)
            };
            return finish_u128(neg && mag != 0, mag, e);
        }
        Money::add_big(a, b, negate_b)
    }

    fn add_big(a: &Money, b: &Money, negate_b: bool) -> DecResult<Money> {
        let x = a.exact();
        let mut y = b.exact();
        y.neg ^= negate_b && !y.coef.is_zero();
        let e = x.exp.min(y.exp);
        let xc = &x.coef * pow10_big((x.exp - e) as usize);
        let yc = &y.coef * pow10_big((y.exp - e) as usize);
        let (neg, coef) = if x.neg == y.neg {
            (x.neg, xc + yc)
        } else {
            match xc.cmp(&yc) {
                Ordering::Equal => (false, BigUint::zero()),
                Ordering::Greater => (x.neg, xc - yc),
                Ordering::Less => (y.neg, yc - xc),
            }
        };
        finish_big(neg, coef, e)
    }

    pub fn mul(&self, other: &Money) -> DecResult<Money> {
        let (na, ma, ea) = self.parts();
        let (nb, mb, eb) = other.parts();
        let neg = na != nb;
        match ma.checked_mul(mb) {
            Some(p) => finish_u128(neg, p, ea + eb),
            None => finish_big(neg, BigUint::from(ma) * BigUint::from(mb), ea + eb),
        }
    }

    /// Python's `Decimal.__truediv__` under the default context: a quotient correctly
    /// rounded to 28 significant digits half-even.
    pub fn div(&self, other: &Money) -> DecResult<Money> {
        let (na, ma, ea) = self.parts();
        let (nb, mb, eb) = other.parts();
        if mb == 0 {
            return Err(if ma == 0 { DecErr::InvalidOperation } else { DecErr::DivisionByZero });
        }
        if ma == 0 {
            return Ok(Money::ZERO);
        }
        let neg = na != nb;
        let shift = digits_u128(mb) as i64 - digits_u128(ma) as i64 + PREC as i64 + 1;
        let exp = ea - eb - shift;
        // the quotient, its remainder, and whether the remainder is non-zero
        let (coef, inexact): (BigUint, bool) = if shift >= 0 {
            match POW10.get(shift as usize).and_then(|p| ma.checked_mul(*p)) {
                Some(num) => (BigUint::from(num / mb), num % mb != 0),
                None => {
                    let num = BigUint::from(ma) * pow10_big(shift as usize);
                    let q = &num / mb;
                    let r = &num - &q * mb;
                    (q, !r.is_zero())
                }
            }
        } else {
            let den = BigUint::from(mb) * pow10_big((-shift) as usize);
            let q = BigUint::from(ma) / &den;
            let r = BigUint::from(ma) - &q * &den;
            (q, !r.is_zero())
        };
        let mut coef = coef;
        if inexact && (&coef % 5u32).is_zero() {
            coef += 1u32; // the sticky digit: a tie is never a tie when digits were cut
        }
        finish_big(neg, coef, exp)
    }

    pub fn mul_i128(&self, n: i128) -> DecResult<Money> {
        self.mul(&Money::from_i128(n)?)
    }

    pub fn add_i128(&self, n: i128) -> DecResult<Money> {
        self.add(&Money::from_i128(n)?)
    }

    // --- comparison (total: there is no NaN; the Result is kept for the call sites) ---

    pub fn cmp_ord(&self, other: &Money) -> DecResult<Ordering> {
        Ok(self.0.cmp(&other.0))
    }

    pub fn eq_num(&self, other: &Money) -> DecResult<bool> {
        Ok(self.0 == other.0)
    }

    pub fn lt(&self, other: &Money) -> DecResult<bool> {
        Ok(self.0 < other.0)
    }

    pub fn le(&self, other: &Money) -> DecResult<bool> {
        Ok(self.0 <= other.0)
    }

    pub fn gt(&self, other: &Money) -> DecResult<bool> {
        Ok(self.0 > other.0)
    }

    pub fn ge(&self, other: &Money) -> DecResult<bool> {
        Ok(self.0 >= other.0)
    }

    pub fn cmp_int(&self, n: i128) -> DecResult<Ordering> {
        self.cmp_ord(&Money::from_i128(n)?)
    }

    // --- integers and rounding -------------------------------------------------------

    /// `d.to_integral_value(rounding=...)`.
    pub fn to_integral(&self, mode: Round) -> DecResult<Money> {
        let (neg, m, exp) = self.parts();
        if exp >= 0 {
            return Ok(*self);
        }
        let coef = shift_round_u128(m, (-exp) as usize, neg, mode);
        Money::build(neg, coef, 0)
    }

    /// `d == d.to_integral_value()`.
    pub fn is_integral(&self) -> DecResult<bool> {
        Ok(self.0.scale() == 0)
    }

    /// `int(d)`: truncate toward zero.
    pub fn trunc_i128(&self) -> DecResult<i128> {
        let (neg, m, exp) = self.parts();
        let mag = if exp >= 0 { m } else { m / POW10[((-exp) as usize).min(38)] };
        let mag = i128::try_from(mag).map_err(|_| DecErr::Unsupported("int() beyond i128"))?;
        Ok(if neg { -mag } else { mag })
    }

    /// The value as an `i128` when it is an integer.
    pub fn to_i128_exact(&self) -> Option<i128> {
        if self.0.scale() != 0 {
            return None;
        }
        self.trunc_i128().ok()
    }

    /// `d.quantize(Decimal(f"1E{exp}"))` under the default context (half-even): a result
    /// over 28 digits raises `InvalidOperation`, as `decimal` does.
    pub fn quantize(&self, exp: i64) -> DecResult<Money> {
        self.quantize_round(exp, Round::HalfEven)
    }

    pub fn quantize_round(&self, exp: i64, mode: Round) -> DecResult<Money> {
        if !(ETINY..=EMAX).contains(&exp) {
            return Err(DecErr::InvalidOperation);
        }
        let (neg, m, e) = self.parts();
        if m == 0 {
            return Ok(Money::ZERO);
        }
        // the coefficient at exponent `exp`
        let coef: BigUint = if e >= exp {
            let up = (e - exp) as usize;
            if up > 4_000_000 {
                return Err(DecErr::InvalidOperation);
            }
            BigUint::from(m) * pow10_big(up)
        } else {
            let drop = (exp - e) as usize;
            BigUint::from(shift_round_u128(m, drop, neg, mode))
        };
        if digits_big(&coef) > PREC {
            return Err(DecErr::InvalidOperation);
        }
        Money::build_big(neg, &coef, exp)
    }
}

#[cfg(test)]
mod tests;
