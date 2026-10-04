//! Journal acceptance and read-back rules; floats only at the existing HTTP boundary.
pub fn accepted(status: i64) -> bool { (200..300).contains(&status) }
pub fn stored(inserted: i64, duplicates: i64, skipped: i64) -> bool {
    inserted as i128 + duplicates as i128 >= 1 && skipped <= 0
}
pub fn base_match(quantity: f64, price: f64, side: bool) -> bool {
    quantity_match(quantity) && price_match(price) && side
}
pub fn quantity_match(delta: f64) -> bool { delta.abs() < 1e-6 }
pub fn price_match(delta: f64) -> bool { delta.abs() < 1e-4 }
pub fn accepted_comparisons(low: bool, high: bool) -> bool { !low && !high }
pub fn stored_comparisons(any_stored: bool, skipped: bool) -> bool { any_stored && !skipped }
pub fn optional_match(present: bool, delta: f64) -> bool {
    present && !(delta.abs() > 1e-4)
}
pub fn microsecond(micro: i64, residue: i64) -> i64 {
    (micro / 1000) * 1000 + residue
}
pub const REQUIRED: &[&str] = &["symbol","side","quantity","price","fee","executed_at",
                              "account_id","asset_class","multiplier"];
pub const STRINGS: &[&str] = &["symbol","account_id","asset_class"];
pub const DECIMALS: &[&str] = &["quantity","price","fee"];
pub const OPTIONAL: &[&str] = &["stop_loss","profit_target","strategy_tag","notes"];
pub const ANNOTATIONS: &[(&str,&str)] = &[("stop_loss","stopLoss"),("profit_target","profitTarget")];
pub fn missing(present: &[bool]) -> Option<&'static str> {
    REQUIRED.iter().zip(present).find(|(_,v)|!**v).map(|(k,_)|*k)
}
pub fn parseable(is_string: bool, nonempty: bool) -> bool { is_string && nonempty }
pub fn tags_source(is_list: bool, is_json_string: bool) -> u8 {
    if is_list {0} else if is_json_string {1} else {2}
}
pub fn multiplier_action(readable: bool, equal: bool) -> &'static str {
    if !readable {"refuse"} else if equal {"done"} else {"patch"}
}
pub fn annotation_present(stop: bool, target: bool, tag: bool) -> bool { stop || target || tag }
pub fn account_matches(equal: bool) -> bool { equal }
pub fn derivative(multiplier_gt_one: bool) -> bool { multiplier_gt_one }
pub fn symbol_matches(equal: bool) -> bool { equal }
pub fn open_trade(equal: bool) -> bool { equal }
pub fn add_tag(nonempty: bool, contains: bool) -> bool { nonempty && !contains }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn boundary() {
        assert!(accepted(200)); assert!(!accepted(300));
        assert!(stored(0,1,0)); assert!(!stored(1,0,1));
        assert!(base_match(0.0,0.0,true)); assert!(!base_match(1e-6,0.0,true));
        assert_eq!(microsecond(123456,999),123999);
    }
}
