//! Reg-T margin (docs/RUST_PORT.md P1c): the equity requirement, the account
//! roll-up and, in `option`, LEAN's option strategy matching and formulas.
//!
//! Money is `rust_decimal` with checked arithmetic (D6): an overflow refuses
//! with a `MarginError`, it never wraps and never panics. Every refusal carries
//! the exact message the Python engine used, and a `kind` telling the Python
//! shim which of its exception types to raise.

pub mod option;
pub mod wire;

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::collections::{BTreeMap, HashMap};
use std::fmt;

use option::{margin_book, Contract, Style, StrategyMargin, StratDef};

/// A refusal. `Option` is the Python `OptionMarginError` (a `ValueError`),
/// `Value` a plain `ValueError`, `Unresolvable` an `UnresolvableInstrumentError`
/// raised by the option-roots table, which the shim resolves and Rust relays.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MarginError {
    Value(String),
    Option(String),
    Unresolvable(String),
}

impl MarginError {
    pub fn kind(&self) -> &'static str {
        match self {
            MarginError::Value(_) => "value",
            MarginError::Option(_) => "option",
            MarginError::Unresolvable(_) => "unresolvable",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            MarginError::Value(m) | MarginError::Option(m) | MarginError::Unresolvable(m) => m,
        }
    }
}

impl fmt::Display for MarginError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message())
    }
}

impl std::error::Error for MarginError {}

pub type Result<T> = std::result::Result<T, MarginError>;

pub(crate) fn overflow() -> MarginError {
    MarginError::Value("decimal arithmetic overflow (D6)".to_string())
}

pub(crate) fn add(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_add(b).ok_or_else(overflow)
}
pub(crate) fn sub(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_sub(b).ok_or_else(overflow)
}
pub(crate) fn mul(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_mul(b).ok_or_else(overflow)
}

pub(crate) const fn dec(units: u32, scale: u32) -> Decimal {
    Decimal::from_parts(units, 0, 0, false, scale)
}

pub const ZERO: Decimal = Decimal::ZERO;
pub const INITIAL_FRACTION: Decimal = dec(50, 2);
pub const MAINTENANCE_FRACTION: Decimal = dec(25, 2);

/// `MarginOverride.__post_init__`: `0 < maintenance <= initial <= 1`.
pub fn validate_override(initial: Decimal, maintenance: Decimal) -> Result<()> {
    if ZERO < maintenance && maintenance <= initial && initial <= Decimal::ONE {
        Ok(())
    } else {
        Err(MarginError::Value(format!(
            "Override fractions must satisfy 0 < maintenance <= initial <= 1, \
             got initial={initial}, maintenance={maintenance}"
        )))
    }
}

/// `AccountMargin.margin_used` / `margin_available`: the binding overnight
/// requirement is the maintenance figure; availability is equity less it.
pub fn margin_summary(equity: Decimal, maintenance: Decimal) -> Result<(Decimal, Decimal)> {
    Ok((maintenance, sub(equity, maintenance)?))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Requirement {
    pub market_value: Decimal,
    pub initial: Decimal,
    pub maintenance: Decimal,
}

/// One position's Reg-T requirement; the signed quantity picks long/short value.
pub fn margin_requirement(
    quantity: Decimal,
    mark: Decimal,
    fractions: Option<(Decimal, Decimal)>,
) -> Result<Requirement> {
    if quantity == ZERO {
        return Err(MarginError::Value("margin_requirement needs a non-zero quantity".to_string()));
    }
    if mark <= ZERO {
        return Err(MarginError::Value(format!("Mark must be positive, got {mark} (I5)")));
    }
    let market_value = mul(quantity, mark)?;
    let magnitude = market_value.abs();
    let (initial, maintenance) = fractions.unwrap_or((INITIAL_FRACTION, MAINTENANCE_FRACTION));
    Ok(Requirement {
        market_value,
        initial: mul(initial, magnitude)?,
        maintenance: mul(maintenance, magnitude)?,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Equity,
    Option,
    Combo,
}

#[derive(Debug, Clone)]
pub struct Position {
    pub symbol: String,
    pub kind: Kind,
    pub quantity: Decimal,
    pub mark: Option<Decimal>,
    pub multiplier: i64,
    pub flat: bool,
    /// Index into the contract table, for an option.
    pub contract: Option<usize>,
    pub avg_cost: Option<Decimal>,
}

#[derive(Debug, Clone)]
pub struct EquityLine {
    pub symbol: String,
    pub quantity: Decimal,
    pub mark: Decimal,
    pub requirement: Requirement,
}

#[derive(Debug, Clone)]
pub struct AccountMargin {
    pub equity: Decimal,
    pub cash: Decimal,
    pub market_value_long: Decimal,
    pub market_value_short: Decimal,
    pub gross_exposure: Decimal,
    pub net_exposure: Decimal,
    pub margin_initial: Decimal,
    pub margin_maintenance: Decimal,
    pub positions: Vec<EquityLine>,
    pub strategies: Vec<StrategyMargin>,
}

pub struct AccountInput<'a> {
    pub cash: Decimal,
    pub positions: &'a [Position],
    pub contracts: &'a [Contract],
    /// Marks of the Equity instruments in the account state, by symbol.
    pub equity_marks: &'a HashMap<String, Decimal>,
    pub underlying_prices: &'a HashMap<String, Decimal>,
    pub overrides: &'a HashMap<String, (Decimal, Decimal)>,
    pub definitions: &'a [StratDef],
    pub search_limit: usize,
}

fn whole(quantity: Decimal, what: &str) -> Result<i64> {
    if quantity != quantity.trunc() {
        return Err(MarginError::Option(format!("{what} holds a fractional {quantity} contracts (I5)")));
    }
    quantity
        .to_i64()
        .ok_or_else(|| MarginError::Option(format!("{what} holds {quantity} contracts, out of range (I5)")))
}

/// Reg-T figures for one account. Every open position must carry a mark at the
/// session close: a missing mark refuses (I5), a guessed one would understate
/// the requirement. Options also need their underlying's price.
pub fn account_margin(input: &AccountInput<'_>) -> Result<AccountMargin> {
    let mut market_value_long = ZERO;
    let mut market_value_short = ZERO;
    let mut margin_initial = ZERO;
    let mut margin_maintenance = ZERO;
    let mut positions: Vec<EquityLine> = Vec::new();
    let mut strategies: Vec<StrategyMargin> = Vec::new();
    let mut shares: BTreeMap<String, Decimal> = BTreeMap::new();
    let mut share_marks: HashMap<String, Decimal> = HashMap::new();
    // underlying -> (contract, whole contracts), in the order positions came in
    let mut books: BTreeMap<String, Vec<(usize, i64)>> = BTreeMap::new();
    let mut avg_costs: HashMap<usize, Decimal> = HashMap::new();
    let mut contract_marks: HashMap<usize, Decimal> = HashMap::new();

    let mut ordered: Vec<&Position> = input.positions.iter().collect();
    ordered.sort_by(|a, b| a.symbol.cmp(&b.symbol)); // stable, like sorted(key=symbol)

    for position in ordered {
        if position.flat {
            continue;
        }
        let mark = match position.mark {
            Some(m) if m > ZERO => m,
            _ => {
                return Err(MarginError::Value(format!(
                    "No session-close mark for open position {} (I5)",
                    position.symbol
                )))
            }
        };
        if position.kind == Kind::Combo {
            return Err(MarginError::Option(format!(
                "Combo position {} must be held per leg (I6)",
                position.symbol
            )));
        }
        let value = mul(mul(position.quantity, mark)?, Decimal::from(position.multiplier))?;
        if value > ZERO {
            market_value_long = add(market_value_long, value)?;
        } else {
            market_value_short = add(market_value_short, value)?;
        }
        if position.kind == Kind::Option {
            let index = position.contract.ok_or_else(|| MarginError::Value("option position without a contract".to_string()))?;
            let contract = &input.contracts[index];
            let underlying = match &contract.style {
                Style::Resolved { underlying, .. } => underlying.clone(),
                Style::Unresolved(message) => return Err(MarginError::Unresolvable(message.clone())),
            };
            let held = whole(position.quantity, &contract.occ)?;
            let book = books.entry(underlying).or_default();
            match book.iter_mut().find(|(c, _)| *c == index) {
                Some(slot) => slot.1 = held,
                None => book.push((index, held)),
            }
            contract_marks.insert(index, mark);
            if let Some(cost) = position.avg_cost {
                avg_costs.insert(index, cost);
            }
        } else {
            shares.insert(position.symbol.clone(), position.quantity);
            share_marks.insert(position.symbol.clone(), mark);
        }
    }

    for (underlying, book) in &books {
        let price = match input.underlying_prices.get(underlying).or_else(|| input.equity_marks.get(underlying)) {
            Some(p) if *p > ZERO => *p,
            _ => {
                return Err(MarginError::Option(format!("No underlying price for {underlying} options (I5)")))
            }
        };
        let (initial, maintenance) =
            input.overrides.get(underlying).copied().unwrap_or((INITIAL_FRACTION, MAINTENANCE_FRACTION));
        // per-contract marks and entry prices, indexed like the contract table
        let mut marks: Vec<Option<Decimal>> = vec![None; input.contracts.len()];
        let mut entries: Vec<Option<Decimal>> = vec![None; input.contracts.len()];
        for (index, _) in book {
            marks[*index] = contract_marks.get(index).copied();
            entries[*index] = avg_costs.get(index).copied();
        }
        let (matched, left) = margin_book(
            underlying,
            input.contracts,
            book,
            shares.get(underlying).copied().unwrap_or(ZERO),
            price,
            &marks,
            initial,
            maintenance,
            Some(&entries),
            input.definitions,
            input.search_limit,
        )?;
        for figures in matched {
            margin_initial = add(margin_initial, figures.initial)?;
            margin_maintenance = add(margin_maintenance, figures.maintenance)?;
            strategies.push(figures);
        }
        if let Some(slot) = shares.get_mut(underlying) {
            *slot = left;
        }
    }

    for (symbol, quantity) in &shares {
        if *quantity == ZERO {
            continue;
        }
        let mark = share_marks[symbol];
        let requirement = margin_requirement(*quantity, mark, input.overrides.get(symbol).copied())?;
        margin_initial = add(margin_initial, requirement.initial)?;
        margin_maintenance = add(margin_maintenance, requirement.maintenance)?;
        positions.push(EquityLine { symbol: symbol.clone(), quantity: *quantity, mark, requirement });
    }

    let equity = add(add(input.cash, market_value_long)?, market_value_short)?;
    Ok(AccountMargin {
        equity,
        cash: input.cash,
        market_value_long,
        market_value_short,
        gross_exposure: add(market_value_long, market_value_short.abs())?,
        net_exposure: add(market_value_long, market_value_short)?,
        margin_initial,
        margin_maintenance,
        positions,
        strategies,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn requirement_long_and_short_use_magnitude() {
        let long = margin_requirement(d("100"), d("48"), None).unwrap();
        assert_eq!((long.market_value, long.initial, long.maintenance), (d("4800"), d("2400"), d("1200")));
        let short = margin_requirement(d("-200"), d("50"), None).unwrap();
        assert_eq!((short.market_value, short.initial, short.maintenance), (d("-10000"), d("5000"), d("2500")));
    }

    #[test]
    fn requirement_refusals_match_python_messages() {
        assert_eq!(
            margin_requirement(d("0"), d("1"), None).unwrap_err().message(),
            "margin_requirement needs a non-zero quantity"
        );
        assert_eq!(
            margin_requirement(d("1"), d("-2.5"), None).unwrap_err().message(),
            "Mark must be positive, got -2.5 (I5)"
        );
    }

    #[test]
    fn requirement_overflow_refuses_instead_of_panicking() {
        let big = Decimal::MAX;
        let e = margin_requirement(big, d("2"), None).unwrap_err();
        assert!(e.message().contains("overflow"), "{e}");
    }

    #[test]
    fn override_bounds() {
        assert!(validate_override(d("0.75"), d("0.5")).is_ok());
        assert!(validate_override(d("1"), d("1")).is_ok());
        assert!(validate_override(d("0.25"), d("0.5")).is_err());
        assert!(validate_override(d("0.5"), d("0")).is_err());
        assert!(validate_override(d("1.01"), d("0.5")).is_err());
    }

    #[test]
    fn summary_is_maintenance_and_equity_less_it() {
        assert_eq!(margin_summary(d("1400"), d("5150")).unwrap(), (d("5150"), d("-3750")));
    }

    #[test]
    fn whole_contracts() {
        assert_eq!(whole(d("-3"), "x").unwrap(), -3);
        assert_eq!(whole(d("2.00"), "x").unwrap(), 2);
        assert!(whole(d("1.5"), "x").unwrap_err().message().contains("fractional 1.5"));
    }

    mod properties {
        use super::*;
        use proptest::prelude::*;

        proptest! {
            #[test]
            fn requirement_is_never_negative_and_maintenance_never_exceeds_initial(
                q in -100_000i64..100_000, mark_cents in 1i64..10_000_000,
            ) {
                prop_assume!(q != 0);
                let r = margin_requirement(Decimal::from(q), Decimal::new(mark_cents, 2), None).unwrap();
                prop_assert!(r.initial >= ZERO && r.maintenance >= ZERO);
                prop_assert!(r.maintenance <= r.initial);
                prop_assert_eq!(r.market_value.is_sign_negative(), q < 0);
            }

            #[test]
            fn requirement_is_monotone_in_quantity_magnitude_and_symmetric_in_side(
                q in 1i64..100_000, extra in 1i64..1000, mark_cents in 1i64..1_000_000,
            ) {
                let m = Decimal::new(mark_cents, 2);
                let small = margin_requirement(Decimal::from(q), m, None).unwrap();
                let big = margin_requirement(Decimal::from(q + extra), m, None).unwrap();
                let short = margin_requirement(Decimal::from(-q), m, None).unwrap();
                prop_assert!(big.initial >= small.initial && big.maintenance >= small.maintenance);
                prop_assert_eq!((short.initial, short.maintenance), (small.initial, small.maintenance));
            }

            #[test]
            fn a_valid_override_is_the_only_override_accepted(i in 0i64..=150, m in 0i64..=150) {
                let (init, maint) = (Decimal::new(i, 2), Decimal::new(m, 2));
                let ok = validate_override(init, maint).is_ok();
                prop_assert_eq!(ok, m > 0 && m <= i && i <= 100);
            }

            #[test]
            fn overflow_refuses_never_panics(q in any::<i64>(), scale in 0u32..20) {
                let _ = margin_requirement(Decimal::MAX, Decimal::new(q.max(1), scale), None);
            }
        }
    }
}
