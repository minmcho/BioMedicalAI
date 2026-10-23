//! JSON de-identification: walk the document, apply category transforms
//! to values whose KEY is a configured identifier key, and pass every
//! remaining string value through the free-text rules. Structure (object
//! keys, array lengths, nesting, non-identifier scalar values) is
//! preserved.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::config::Config;
use crate::text::{transform_single_date, TextEngine};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyCategory {
    Name,
    Address,
    City,
    Zip,
    Ssn,
    Mrn,
    Account,
    Device,
    Email,
    Phone,
    Url,
    Ipv4,
    Date,
}

impl KeyCategory {
    pub fn count_name(&self) -> &'static str {
        match self {
            KeyCategory::Name => "json_key:name",
            KeyCategory::Address => "json_key:address",
            KeyCategory::City => "json_key:city",
            KeyCategory::Zip => "json_key:zip",
            KeyCategory::Ssn => "json_key:ssn",
            KeyCategory::Mrn => "json_key:mrn",
            KeyCategory::Account => "json_key:account",
            KeyCategory::Device => "json_key:device",
            KeyCategory::Email => "json_key:email",
            KeyCategory::Phone => "json_key:phone",
            KeyCategory::Url => "json_key:url",
            KeyCategory::Ipv4 => "json_key:ipv4",
            KeyCategory::Date => "json_key:date",
        }
    }

    fn replacement(&self, cfg: &Config) -> String {
        let r = &cfg.replacements;
        match self {
            KeyCategory::Name => r.name.clone(),
            KeyCategory::Address => r.address.clone(),
            KeyCategory::City => r.city.clone(),
            KeyCategory::Zip => r.identifier.clone(), // zip uses its transform; fallback only
            KeyCategory::Ssn => r.ssn.clone(),
            KeyCategory::Mrn => r.mrn.clone(),
            KeyCategory::Account => r.account.clone(),
            KeyCategory::Device => r.device.clone(),
            KeyCategory::Email => r.email.clone(),
            KeyCategory::Phone => r.phone.clone(),
            KeyCategory::Url => r.url.clone(),
            KeyCategory::Ipv4 => r.ipv4.clone(),
            KeyCategory::Date => r.identifier.clone(), // date uses its transform; fallback only
        }
    }
}

pub fn normalise_key(key: &str) -> String {
    key.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

/// Classify a JSON key. Returns None for ordinary clinical keys.
pub fn key_category(cfg: &Config, key: &str) -> Option<KeyCategory> {
    if !cfg.rules.json_keys {
        return None;
    }
    let k = normalise_key(key);
    let keys = &cfg.identifier_keys;
    let has = |list: &[String]| list.iter().any(|item| normalise_key(item) == k);
    if has(&keys.name) {
        return Some(KeyCategory::Name);
    }
    if has(&keys.address) {
        return Some(KeyCategory::Address);
    }
    if has(&keys.city) {
        return Some(KeyCategory::City);
    }
    if has(&keys.zip) {
        return Some(KeyCategory::Zip);
    }
    if has(&keys.ssn) {
        return Some(KeyCategory::Ssn);
    }
    if has(&keys.mrn) {
        return Some(KeyCategory::Mrn);
    }
    if has(&keys.account) {
        return Some(KeyCategory::Account);
    }
    if has(&keys.device) {
        return Some(KeyCategory::Device);
    }
    if has(&keys.email) {
        return Some(KeyCategory::Email);
    }
    if has(&keys.phone) {
        return Some(KeyCategory::Phone);
    }
    if has(&keys.url) {
        return Some(KeyCategory::Url);
    }
    if has(&keys.ipv4) {
        return Some(KeyCategory::Ipv4);
    }
    if has(&keys.date) || k.contains("birth") || k.ends_with("date") || k.ends_with("datetime") {
        // Date-key transforms only apply when the dates rule is on.
        if cfg.rules.dates {
            return Some(KeyCategory::Date);
        }
    }
    None
}

fn truncate_zip_str(s: &str) -> String {
    let digits: String = s.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() >= 5 {
        format!("{}00", &digits[..3])
    } else {
        s.to_string()
    }
}

fn deid_walk(engine: &TextEngine, cfg: &Config, record_key: &str, value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                let new_v = match key_category(cfg, k) {
                    Some(KeyCategory::Date) => match v {
                        Value::String(s) => match transform_single_date(engine, cfg, record_key, s) {
                            Some(t) => Value::String(t),
                            None => Value::String(engine.deid_text(cfg, record_key, s)),
                        },
                        _ => deid_walk(engine, cfg, record_key, v),
                    },
                    Some(KeyCategory::Zip) => match v {
                        Value::String(s) => {
                            if cfg.rules.zip {
                                Value::String(truncate_zip_str(s))
                            } else {
                                v.clone()
                            }
                        }
                        Value::Number(n) => {
                            if cfg.rules.zip {
                                match n.as_i64() {
                                    Some(n) if n >= 10000 => Value::from(n / 100 * 100),
                                    _ => v.clone(),
                                }
                            } else {
                                v.clone()
                            }
                        }
                        _ => deid_walk(engine, cfg, record_key, v),
                    },
                    Some(cat) => match v {
                        // Structured value under a redacting key (e.g. a
                        // FHIR HumanName / Address object): recurse so
                        // sub-keys transform individually and structure
                        // survives.
                        Value::Object(_) => deid_walk(engine, cfg, record_key, v),
                        Value::Array(items) => Value::Array(
                            items
                                .iter()
                                .map(|item| match item {
                                    Value::Object(_) => {
                                        deid_walk(engine, cfg, record_key, item)
                                    }
                                    Value::Null => Value::Null,
                                    _ => Value::String(cat.replacement(cfg)),
                                })
                                .collect(),
                        ),
                        Value::Null => Value::Null,
                        _ => Value::String(cat.replacement(cfg)),
                    },
                    None => deid_walk(engine, cfg, record_key, v),
                };
                out.insert(k.clone(), new_v);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| deid_walk(engine, cfg, record_key, item))
                .collect(),
        ),
        Value::String(s) => Value::String(engine.deid_text(cfg, record_key, s)),
        other => other.clone(),
    }
}

fn count_walk(
    engine: &TextEngine,
    cfg: &Config,
    value: &Value,
    counts: &mut BTreeMap<String, usize>,
) {
    match value {
        Value::Object(map) => {
            for (k, v) in map {
                match key_category(cfg, k) {
                    Some(cat) => {
                        *counts.entry(cat.count_name().to_string()).or_insert(0) += 1;
                        // Redacted-by-key subtrees are counted at the
                        // key, not re-scanned as free text; structured
                        // values are still walked for nested keys.
                        if v.is_object() || v.is_array() {
                            count_walk(engine, cfg, v, counts);
                        }
                    }
                    None => count_walk(engine, cfg, v, counts),
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                count_walk(engine, cfg, item, counts);
            }
        }
        Value::String(s) => {
            for (k, n) in engine.count_text(cfg, s) {
                *counts.entry(k).or_insert(0) += n;
            }
        }
        _ => {}
    }
}

/// Extract the record key for date shifting from a JSON document:
/// explicit CLI key wins, else the configured top-level field.
pub fn record_key_for(cfg: &Config, doc: &Value, explicit: Option<&str>) -> String {
    if let Some(k) = explicit {
        return k.to_string();
    }
    if let Some(field) = &cfg.record_key_field {
        if let Value::Object(map) = doc {
            match map.get(field) {
                Some(Value::String(s)) => return s.clone(),
                Some(Value::Number(n)) => return n.to_string(),
                _ => {}
            }
        }
    }
    String::new()
}

pub fn deid_json(
    engine: &TextEngine,
    cfg: &Config,
    doc: &Value,
    explicit_record_key: Option<&str>,
) -> Value {
    let rk = record_key_for(cfg, doc, explicit_record_key);
    deid_walk(engine, cfg, &rk, doc)
}

/// Count identifier hits in a JSON document (key-based + free-text),
/// without producing de-identified output.
pub fn count_json(engine: &TextEngine, cfg: &Config, doc: &Value) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    count_walk(engine, cfg, doc, &mut counts);
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use serde_json::json;

    #[test]
    fn key_normalisation() {
        assert_eq!(normalise_key("date_of_birth"), "dateofbirth");
        assert_eq!(normalise_key("Medical-Record_Number"), "medicalrecordnumber");
    }

    #[test]
    fn nested_name_object_subkeys_transform() {
        let cfg = Config::default();
        let engine = TextEngine::new(&cfg).unwrap();
        let doc = json!({"name": {"family": "Nguyen", "given": ["Mai"]}, "problem": "diabetes"});
        let out = deid_json(&engine, &cfg, &doc, None);
        assert_eq!(out["name"]["family"], json!("[NAME]"));
        assert_eq!(out["name"]["given"], json!(["[NAME]"]));
        assert_eq!(out["problem"], json!("diabetes"));
    }
}
