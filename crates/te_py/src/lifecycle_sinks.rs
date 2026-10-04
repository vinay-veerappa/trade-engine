//! Python carrier conversion for the journal. The stdlib remains the ISO/JSON codec:
//! it preserves Python's accepted syntax, object conversion and exception spelling.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use te_core::runtime::journal as rules;

fn get<'py>(obj: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    obj.call_method1("get",(key,))
}
fn default<'py>(obj: &Bound<'py, PyAny>, key: &str, value: Bound<'py,PyAny>) -> PyResult<Bound<'py,PyAny>> {
    obj.call_method1("get",(key,value))
}
fn string(value: &Bound<'_,PyAny>) -> PyResult<String> { Ok(value.str()?.to_str()?.into()) }
fn lower(value: &Bound<'_,PyAny>) -> PyResult<String> { string(&value.call_method0("lower")?) }
fn symbol(value: &Bound<'_,PyAny>) -> PyResult<Py<PyAny>> {
    Ok(value.call_method0("strip")?.call_method0("upper")?.unbind())
}
fn number(value: &Bound<'_,PyAny>) -> PyResult<f64> {
    value.py().import("builtins")?.getattr("float")?.call1((value,))?.extract()
}
fn parse<'py>(value: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyAny>> {
    let py = value.py();
    let is_string=value.is_instance_of::<PyString>();
    if !rules::parseable(is_string,is_string && value.is_truthy()?) { return Ok(py.None().into_bound(py)); }
    let clean = value.call_method1("replace",("Z","+00:00"))?;
    let parsed = match py.import("datetime")?.getattr("datetime")?.call_method1("fromisoformat",(clean,)) {
        Ok(v) => v,
        Err(e) if e.is_instance_of::<PyValueError>(py) => return Ok(py.None().into_bound(py)),
        Err(e) => return Err(e),
    };
    if parsed.getattr("tzinfo")?.is_none() { Ok(py.None().into_bound(py)) } else { Ok(parsed) }
}
fn stamp<'py>(value: &Bound<'py,PyAny>, seq: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyAny>> {
    let py=value.py();
    let utc=py.import("datetime")?.getattr("timezone")?.getattr("utc")?;
    let dt=value.call_method1("astimezone",(utc,))?;
    let residue=seq.call_method1("__mod__",(1000,))?.extract::<i64>()?;
    let micro=rules::microsecond(dt.getattr("microsecond")?.extract()?,residue);
    let kwargs=PyDict::new(py); kwargs.set_item("microsecond",micro)?;
    let dt=dt.call_method("replace",(),Some(&kwargs))?;
    let kwargs=PyDict::new(py); kwargs.set_item("timespec","microseconds")?;
    dt.call_method("isoformat",(),Some(&kwargs))
}
fn tags<'py>(trade: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyList>> {
    let py=trade.py();
    let mut value=get(trade,"tags")?;
    if rules::tags_source(value.is_instance_of::<PyList>(),false) != 0 {
        let raw=get(trade,"tagsJson")?;
        if rules::tags_source(false,raw.is_instance_of::<PyString>()) != 1 { return Ok(PyList::empty(py)); }
        value=match py.import("json")?.call_method1("loads",(raw,)) {
            Ok(v)=>v,
            Err(e) if e.is_instance_of::<PyValueError>(py)=>return Ok(PyList::empty(py)),
            Err(e)=>return Err(e),
        };
    }
    if !value.is_instance_of::<PyList>() { return Ok(PyList::empty(py)); }
    let out=PyList::empty(py);
    for item in value.try_iter()? { out.append(item?.str()?)?; }
    Ok(out)
}
fn execution<'py>(d: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyDict>> {
    let py=d.py();
    let mut present=Vec::new();
    for field in rules::REQUIRED {
        present.push(d.contains(*field)? && !d.get_item(*field)?.is_none());
        if let Some(field)=rules::missing(&present) {
            return Err(PyValueError::new_err(format!("Missing required field '{field}' in execution payload (I5)")));
        }
    }
    let out=PyDict::new(py);
    let dt=d.get_item("executed_at")?;
    let dt=if dt.is_instance_of::<PyString>() {
        py.import("datetime")?.getattr("datetime")?.call_method1("fromisoformat",(dt,))?
    } else { dt };
    let side_class=py.import("trade_engine.domain.instruments")?.getattr("Side")?;
    let side=d.get_item("side")?;
    let side=if side.is_instance(&side_class)? {side}
        else if side.is_instance_of::<PyString>() {side_class.call1((side.call_method0("upper")?,))?}
        else {return Err(PyValueError::new_err(format!("Invalid side: {}",string(&side)?)));};
    out.set_item("executed_at",dt)?; out.set_item("side",side)?;
    let decimal=py.import("decimal")?.getattr("Decimal")?;
    out.set_item("symbol",d.get_item("symbol")?.str()?)?;
    for key in rules::DECIMALS { out.set_item(*key,decimal.call1((d.get_item(*key)?.str()?,))?)?; }
    for key in &rules::STRINGS[1..] { out.set_item(*key,d.get_item(*key)?.str()?)?; }
    out.set_item("multiplier",py.import("builtins")?.getattr("int")?.call1((d.get_item("multiplier")?,))?)?;
    for key in rules::OPTIONAL {
        let v=get(d,key)?;
        if v.is_none() {out.set_item(*key,v)?;}
        else if rules::ANNOTATIONS.iter().any(|(k,_)| k == key) {out.set_item(*key,decimal.call1((v.str()?,))?)?;}
        else {out.set_item(*key,v.str()?)?;}
    }
    Ok(out)
}
fn payload<'py>(account: &Bound<'py,PyAny>, seq: &Bound<'py,PyAny>, e: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyDict>> {
    let py=e.py();
    let row=PyDict::new(py);
    row.set_item("symbol",symbol(&e.getattr("symbol")?)?)?;
    row.set_item("side",e.getattr("side")?.getattr("value")?.call_method0("lower")?)?;
    for key in ["quantity","price","fee"] { row.set_item(key,number(&e.getattr(key)?)?)?; }
    row.set_item("executedAt",stamp(&e.getattr("executed_at")?,seq)?)?;
    row.set_item("assetClass",e.getattr("asset_class")?)?;
    let out=PyDict::new(py); out.set_item("accountId",account)?;
    out.set_item("executions",PyList::new(py,[row])?)?;
    let notes=e.getattr("notes")?;
    if !notes.is_none() {out.set_item("notes",notes)?;}
    Ok(out)
}
fn candidates<'py>(trades: &Bound<'py,PyAny>, sym: &Bound<'py,PyAny>, annotation: bool) -> PyResult<Bound<'py,PyAny>> {
    let py=trades.py();
    let reversed=py.import("builtins")?.getattr("reversed")?.call1((trades,))?;
    let out=PyList::empty(py); let mut fallback=None;
    for trade in reversed.try_iter()? {
        let trade=trade?;
        if !rules::symbol_matches(get(&trade,"symbol")?.eq(sym)?) {continue;}
        if !annotation {out.append(trade)?;continue;}
        if rules::open_trade(get(&trade,"status")?.eq("open")?) {return Ok(trade);}
        if fallback.is_none() {fallback=Some(trade);}
    }
    if annotation {Ok(fallback.unwrap_or_else(||py.None().into_bound(py)))} else {Ok(out.into_any())}
}
fn patch<'py>(e: &Bound<'py,PyAny>, trade: &Bound<'py,PyAny>) -> PyResult<Bound<'py,PyDict>> {
    let py=e.py(); let out=PyDict::new(py);
    for (attr,key) in rules::ANNOTATIONS {
        let v=e.getattr(*attr)?; if !v.is_none() {out.set_item(*key,number(&v)?)?;}
    }
    let tag=e.getattr("strategy_tag")?;
    if tag.is_truthy()? {
        let existing=tags(trade)?;
        if rules::add_tag(true,existing.contains(&tag)?) {existing.append(tag)?;out.set_item("tags",existing)?;}
    }
    Ok(out)
}
fn matches(e: &Bound<'_,PyAny>, fill: &Bound<'_,PyAny>, body: &Bound<'_,PyAny>, sent: &Bound<'_,PyAny>) -> PyResult<bool> {
    let py=e.py();
    if !parse(&get(fill,"executedAt")?)?.eq(sent)? {return Ok(false);}
    let q=number(&default(fill,"quantity",0i64.into_pyobject(py)?.into_any())?)?-number(&e.getattr("quantity")?)?;
    if !rules::quantity_match(q) {return Ok(false);}
    let price=number(&default(fill,"price",0i64.into_pyobject(py)?.into_any())?)?-number(&e.getattr("price")?)?;
    if !rules::price_match(price) {return Ok(false);}
    let side=lower(&default(fill,"side","".into_pyobject(py)?.into_any())?.str()?.into_any())? == lower(&e.getattr("side")?.getattr("value")?)?;
    if !rules::base_match(q,price,side) {return Ok(false);}
    if !e.getattr("fee")?.eq(0)? {
        let delta=number(&default(fill,"fee",0i64.into_pyobject(py)?.into_any())?)?-number(&e.getattr("fee")?)?;
        if !rules::optional_match(true,delta) {return Ok(false);}
    }
    if !get(fill,"assetClass")?.eq(e.getattr("asset_class")?)? {return Ok(false);}
    let detail=default(body,"trade",PyDict::new(py).into_any())?;
    if rules::derivative(e.getattr("multiplier")?.gt(1)?) {
        let v=get(&detail,"contractMultiplier")?;
        if v.is_none() || !rules::optional_match(true,number(&v)?-number(&e.getattr("multiplier")?)?) {return Ok(false);}
    }
    for (attr,key) in rules::ANNOTATIONS {
        let wanted=e.getattr(*attr)?;
        if !wanted.is_none() {
            let v=get(&detail,key)?;
            if v.is_none() || !rules::optional_match(true,number(&v)?-number(&wanted)?) {return Ok(false);}
        }
    }
    let tag=e.getattr("strategy_tag")?;
    if tag.is_truthy()? && !tags(&detail)?.contains(tag)? {return Ok(false);}
    Ok(true)
}
#[pyfunction]
fn runtime_journal(py: Python<'_>, op: &str, args: &Bound<'_,PyTuple>) -> PyResult<Py<PyAny>> {
    let a=|i|args.get_item(i);
    let out: Bound<'_,PyAny> = match op {
        "stamp"=>stamp(&a(0)?,&a(1)?)?,
        "parse"=>parse(&a(0)?)?,
        "tags"=>tags(&a(0)?)?.into_any(),
        "execution"=>execution(&a(0)?)?.into_any(),
        "payload"=>payload(&a(0)?,&a(1)?,&a(2)?)?.into_any(),
        "symbol"=>symbol(&a(0)?)?.into_bound(py),
        "before"=>a(0)?.lt(a(1)?)?.into_pyobject(py)?.to_owned().into_any(),
        "after"=>a(0)?.gt(a(1)?)?.into_pyobject(py)?.to_owned().into_any(),
        "candidates"=>candidates(&a(0)?,&a(1)?,false)?,
        "annotation_trade"=>candidates(&a(0)?,&a(1)?,true)?,
        "patch"=>patch(&a(0)?,&a(1)?)?.into_any(),
        "match"=>matches(&a(0)?,&a(1)?,&a(2)?,&a(3)?)?.into_pyobject(py)?.to_owned().into_any(),
        "accepted"=> {
            // HTTP status normally is int; rich comparisons also preserve lax fake clients.
            let status=a(0)?;
            rules::accepted_comparisons(status.lt(200)?,status.ge(300)?).into_pyobject(py)?.to_owned().into_any()
        },
        "account"=>rules::account_matches(a(0)?.eq(a(1)?)?).into_pyobject(py)?.to_owned().into_any(),
        "derivative"=>rules::derivative(a(0)?.gt(1)?).into_pyobject(py)?.to_owned().into_any(),
        "annotations"=>rules::annotation_present(!a(0)?.is_none(),!a(1)?.is_none(),a(2)?.is_truthy()?).into_pyobject(py)?.to_owned().into_any(),
        "stored"=> {
            // Python's int() is deliberately unbounded and supplies its exact refusals.
            let int=py.import("builtins")?.getattr("int")?;
            let body=a(0)?;
            let mut values=Vec::new();
            for k in ["inserted","duplicates","skipped"] {
                values.push(int.call1((default(&body,k,0i64.into_pyobject(py)?.into_any())?,))?);
            }
            let total=values[0].call_method1("__add__",(&values[1],))?;
            rules::stored_comparisons(total.ge(1)?,values[2].gt(0)?).into_pyobject(py)?.to_owned().into_any()
        },
        "multiplier"=> {
            let existing=a(0)?; let sym=a(1)?; let multiplier=a(2)?;
            let out=PyDict::new(py);
            let action=rules::multiplier_action(!existing.is_none(),
                !existing.is_none() && existing.call_method1("get",(&sym,))?.eq(&multiplier)?);
            out.set_item("action",action)?;
            if action == "patch" {
                let merged=existing.call_method0("copy")?;
                merged.set_item(sym,multiplier)?;
                out.set_item("multipliers",merged)?;
            }
            out.into_any()
        },
        _=>return Err(PyValueError::new_err(format!("Unknown journal decision {op}"))),
    };
    Ok(out.unbind())
}
pub fn register(m: &Bound<'_,PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(runtime_journal,m)?)?;
    Ok(())
}
