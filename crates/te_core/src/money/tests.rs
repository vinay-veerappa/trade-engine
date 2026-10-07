//! The spelling table (docs/RUST_PORT.md "P7 verification and boundary") and the
//! value-equality proptests against `PyDec`, the test-only oracle of Python's `decimal`.

use super::*;
use crate::ledger::pydec::{self, PyDec};
use proptest::prelude::*;

fn m(s: &str) -> Money {
    Money::try_parse(s).unwrap_or_else(|e| panic!("parse {s}: {e:?}"))
}

#[test]
fn the_design_table() {
    for (input, spelled) in [
        ("1.10", "1.1"),
        ("1E+2", "100"),
        ("-0", "0"),
        ("0.000", "0"),
        ("0E+3", "0"),
        ("7.77E+3", "7770"),
        ("1E-7", "0.0000001"),
        ("-0.50", "-0.5"),
        ("100", "100"),
        ("1_0.5_0", "10.5"),
        (" 12 ", "12"),
        ("\u{661}\u{662}", "12"),
        ("0.1428571428571428571428571429", "0.1428571428571428571428571429"),
        ("79228162514264337593543950335", "79228162514264337593543950335"),
        ("0.0000000000000000000000000001", "0.0000000000000000000000000001"),
    ] {
        assert_eq!(m(input).canon(), spelled, "{input}");
        assert_eq!(m(spelled).canon(), spelled, "idempotent {spelled}");
    }
    // the 28-digit quotient keeps its digits
    assert_eq!(m("100").div(&m("3")).unwrap().canon(), "33.33333333333333333333333333");
    assert_eq!(m("1").div(&m("3")).unwrap().canon(), "0.3333333333333333333333333333");
    assert_eq!(m("2").div(&m("3")).unwrap().canon(), "0.6666666666666666666666666667");
}

#[test]
fn a_value_outside_the_bound_is_refused_never_rounded() {
    for s in [
        "79228162514264337593543950336", // 2^96
        "1E+30",
        "1E-29",
        "0.00000000000000000000000000001",
        "123456789012345678901234567890123",
        "NaN",
        "Infinity",
        "-inf",
        "sNaN",
        "1e",
        ".",
        "",
    ] {
        assert!(Money::try_parse(s).is_err(), "{s} must be refused");
        assert!(Money::parse(s).is_none());
    }
    assert_eq!(Money::try_parse("1E+30"), Err(DecErr::Overflow));
    assert_eq!(Money::try_parse("NaN"), Err(DecErr::InvalidOperation));
    // arithmetic that lands outside refuses too
    assert_eq!(m("1000000000000000").mul(&m("1000000000000000")), Err(DecErr::Overflow));
    assert_eq!(m("0.0000000001").mul(&m("0.0000000001")).unwrap().canon(), "0.00000000000000000001");
    assert_eq!(m("0.000000000000001").mul(&m("0.000000000000001")), Err(DecErr::Overflow));
    assert_eq!(m("1").div(&m("0")), Err(DecErr::DivisionByZero));
    assert_eq!(m("0").div(&m("0")), Err(DecErr::InvalidOperation));
    // 2^96 - 1 plus one is 29 digits: Python rounds it to 28 digits and so does Money
    let big = m("79228162514264337593543950335");
    assert_eq!(big.add(&m("1")), Err(DecErr::Overflow));
}

#[test]
fn negative_zero_never_exists() {
    let z = m("-0").neg().unwrap();
    assert_eq!(z.canon(), "0");
    assert_eq!(m("0.5").sub(&m("0.5")).unwrap().canon(), "0");
    assert_eq!(m("-0.5").add(&m("0.5")).unwrap().canon(), "0");
    assert_eq!(m("-0.0001").quantize(-2).unwrap().canon(), "0");
    assert_eq!(m("0").mul(&m("-3")).unwrap().canon(), "0");
    assert!(!m("-0").is_negative());
}

#[test]
fn rounding_modes() {
    assert_eq!(m("2.5").quantize(0).unwrap().canon(), "2");
    assert_eq!(m("3.5").quantize(0).unwrap().canon(), "4");
    assert_eq!(m("-2.5").quantize_round(0, Round::Floor).unwrap().canon(), "-3");
    assert_eq!(m("2.5").quantize_round(0, Round::Floor).unwrap().canon(), "2");
    assert_eq!(m("1234").quantize(2).unwrap().canon(), "1200");
    assert_eq!(m("1.005").quantize(-2).unwrap().canon(), "1");
    assert_eq!(m("1.015").quantize(-2).unwrap().canon(), "1.02");
    assert_eq!(m("123456789012345678901234567.9").quantize(0), Ok(m("123456789012345678901234568")));
    assert_eq!(m("12345678901234567890123456789").quantize(-1), Err(DecErr::InvalidOperation));
}

fn oracle_value(p: &PyDec) -> DecResult<Money> {
    Money::try_parse(&p.format_f())
}

fn text() -> impl Strategy<Value = String> {
    (any::<bool>(), 1usize..=29, 0u8..4, -32i32..=8).prop_flat_map(|(neg, digits, shape, exp)| {
        proptest::collection::vec(0u8..10, digits).prop_map(move |ds| {
            let body: String = ds.iter().map(|d| char::from(b'0' + d)).collect();
            let sign = if neg { "-" } else { "" };
            match shape {
                0 => format!("{sign}{body}"),
                1 => format!("{sign}{body}E{exp}"),
                2 => {
                    let cut = (body.len() / 2).max(1);
                    format!("{sign}{}.{}", &body[..cut], &body[cut..])
                }
                _ => format!("{sign}0.{body}"),
            }
        })
    })
}

fn both(a: &str, b: &str) -> Option<(PyDec, PyDec, Money, Money)> {
    let (ma, mb) = (Money::try_parse(a).ok()?, Money::try_parse(b).ok()?);
    Some((PyDec::parse(a)?, PyDec::parse(b)?, ma, mb))
}

/// Money's result is the oracle's value where the oracle's is in the bound, and a refusal
/// where it is not; the oracle's own refusals are Money's (same signal).
fn agrees(py: pydec::DecResult<PyDec>, got: DecResult<Money>) {
    match py {
        Ok(r) => {
            let expected = oracle_value(&r);
            assert_eq!(got, expected, "oracle {}", r.to_py_string());
        }
        Err(pydec::DecErr::Unsupported(_)) => {}
        Err(pydec::DecErr::InvalidOperation) => assert_eq!(got, Err(DecErr::InvalidOperation)),
        Err(pydec::DecErr::DivisionByZero) => assert_eq!(got, Err(DecErr::DivisionByZero)),
        Err(pydec::DecErr::Overflow) => assert_eq!(got, Err(DecErr::Overflow)),
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(4000))]

    #[test]
    fn spelling_is_idempotent_and_value_preserving(a in text()) {
        if let Ok(x) = Money::try_parse(&a) {
            let again = Money::try_parse(&x.canon()).unwrap();
            prop_assert_eq!(again, x);
            prop_assert_eq!(again.canon(), x.canon());
            let py = PyDec::parse(&a).unwrap();
            prop_assert_eq!(oracle_value(&py).unwrap(), x);
            prop_assert!(!x.canon().contains(['E', 'e', '+']));
            prop_assert!(!x.canon().ends_with(".0"));
            if x.canon().contains('.') { prop_assert!(!x.canon().ends_with('0')); }
        } else {
            // refused: the value is outside the bound, so PyDec's text, exactly read, does
            // not fit either
            let py = PyDec::parse(&a).unwrap();
            prop_assert!(Money::try_parse(&py.format_f()).is_err());
        }
    }

    #[test]
    fn add_sub_mul_div_equal_the_oracle(a in text(), b in text()) {
        if let Some((pa, pb, ma, mb)) = both(&a, &b) {
            agrees(pa.add(&pb), ma.add(&mb));
            agrees(pa.sub(&pb), ma.sub(&mb));
            agrees(pa.mul(&pb), ma.mul(&mb));
            agrees(pa.div(&pb), ma.div(&mb));
        }
    }

    #[test]
    fn neg_abs_compare_equal_the_oracle(a in text(), b in text()) {
        if let Some((pa, pb, ma, mb)) = both(&a, &b) {
            agrees(pa.neg(), ma.neg());
            agrees(pa.abs(), ma.abs());
            prop_assert_eq!(pa.cmp_ord(&pb).unwrap(), ma.cmp_ord(&mb).unwrap());
            prop_assert_eq!(pa.eq_num(&pb).unwrap(), ma.eq_num(&mb).unwrap());
            prop_assert_eq!(pa.lt(&pb).unwrap(), ma.lt(&mb).unwrap());
            prop_assert_eq!(pa.ge(&pb).unwrap(), ma.ge(&mb).unwrap());
            prop_assert_eq!(pa.is_zero(), ma.is_zero());
        }
    }

    #[test]
    fn quantize_and_integral_equal_the_oracle(a in text(), exp in -30i64..=3) {
        if let Some((pa, _, ma, _)) = both(&a, "1") {
            agrees(pa.quantize(exp), ma.quantize(exp));
            agrees(pa.quantize_round(exp, pydec::Round::Floor), ma.quantize_round(exp, Round::Floor));
            agrees(pa.to_integral(pydec::Round::HalfEven), ma.to_integral(Round::HalfEven));
            agrees(pa.to_integral(pydec::Round::Floor), ma.to_integral(Round::Floor));
            prop_assert_eq!(pa.is_integral().unwrap(), ma.is_integral().unwrap());
            prop_assert_eq!(pa.trunc_i128().ok(), ma.trunc_i128().ok());
        }
    }
}
