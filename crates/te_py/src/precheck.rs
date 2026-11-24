//! W5 P1: the RiskGuard pre-check's decision rules (`te_core::oms::precheck`) as
//! `src/trade_engine/oms/precheck.py` reaches them. Money crosses as its canonical text; a
//! refusal crosses as `ValueError((kind, message))`. The Python shim keeps the I/O.

use std::collections::HashMap;

use pyo3::prelude::*;
use te_core::ledger::model::R as LR;
use te_core::money::Money;
use te_core::oms::precheck as pc;

use crate::sim::{dec, instrument, refuse, side as parse_side};

#[pyfunction]
fn precheck_split_quantity(q: &str, side: &str, position: &str) -> PyResult<(String, String)> {
    let run = || -> LR<(Money, Money)> { pc::split_quantity(&dec(q)?, parse_side(side)?, &dec(position)?) };
    let (reducing, opening) = run().map_err(refuse)?;
    Ok((reducing.to_string(), opening.to_string()))
}

type Indexed = Vec<(usize, String)>;

#[pyfunction]
fn precheck_split_allocations(
    side: &str,
    allocs: Vec<(String, String)>,
    positions: HashMap<String, String>,
) -> PyResult<(Indexed, Indexed, Vec<(String, String)>)> {
    let run = || -> LR<pc::AllocSplit> {
        let allocs = allocs.iter().map(|(account, q)| Ok((account.clone(), dec(q)?))).collect::<LR<Vec<_>>>()?;
        let positions = positions
            .iter()
            .map(|(account, p)| Ok((account.clone(), dec(p)?)))
            .collect::<LR<HashMap<_, _>>>()?;
        pc::split_allocations(parse_side(side)?, &allocs, &positions)
    };
    let split = run().map_err(refuse)?;
    let indexed = |rows: &[(usize, Money)]| rows.iter().map(|(i, m)| (*i, m.to_string())).collect();
    let openings = split.openings.iter().map(|(account, m)| (account.clone(), m.to_string())).collect();
    Ok((indexed(&split.reducing), indexed(&split.opening), openings))
}

/// The RiskGuard root for an instrument's text (`codec.text(codec.canon(instrument))`);
/// `None` when it has none or the text does not parse. Never raises.
#[pyfunction]
fn precheck_root(instrument_text: &str) -> Option<String> {
    instrument(instrument_text).ok().and_then(|found| pc::root(&found))
}

#[pyfunction]
fn precheck_valid_response(body: &str) -> bool {
    pc::valid_response(body)
}

#[pyfunction]
fn precheck_loopback_base(url: &str) -> PyResult<String> {
    pc::loopback_base(url).map_err(refuse)
}

#[pyfunction]
#[pyo3(signature = (order_qty, new_qty, side, position, price_changed))]
fn precheck_replace_opening(
    order_qty: &str,
    new_qty: Option<String>,
    side: &str,
    position: &str,
    price_changed: bool,
) -> PyResult<Option<String>> {
    let run = || -> LR<Option<Money>> {
        let new_qty = new_qty.as_deref().map(dec).transpose()?;
        pc::replace_opening(&dec(order_qty)?, new_qty, parse_side(side)?, &dec(position)?, price_changed)
    };
    Ok(run().map_err(refuse)?.map(|opening| opening.to_string()))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(precheck_split_quantity, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_split_allocations, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_root, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_valid_response, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_loopback_base, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_replace_opening, m)?)?;
    Ok(())
}
