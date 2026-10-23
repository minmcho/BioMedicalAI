//! Terminology normalisation — the Rust tier counterpart of
//! `backend/app/pipelines.py::_step_normalise` (Phase 16, BD-50).
//!
//! Semantics are IDENTICAL to the Python core implementation, by
//! construction — this module is a transliteration of it:
//!
//! - The code map is the published `terminology/code-map` payload:
//!   a list of mapping entries `{fromCode, fromSystem?, toCode,
//!   toSystem?, ...}`. A lookup table is built exactly as the Python
//!   builds it: the `(fromSystem, fromCode)` key maps to the entry
//!   (later entries overwrite earlier ones), and the generic
//!   `("", fromCode)` key keeps the FIRST entry seen for that code
//!   (`setdefault` semantics). Entries that are not objects, or
//!   whose `fromCode` is missing/falsy, are skipped.
//! - Each row: only the `code` field is mapped (there is no second
//!   code field in the Python step). The row's `system` field, when
//!   present and truthy, selects the system-specific entry; otherwise
//!   (or when no system-specific entry exists) the generic entry for
//!   the code applies.
//! - On a hit the row gains `source_code` (the ORIGINAL code value,
//!   type preserved), `code` becomes `str(toCode or original code)`,
//!   `system` becomes `str(toSystem)` when `toSystem` is truthy, and
//!   `normalised` is set to `true`.
//! - On a miss the row is passed through UNCHANGED and its code is
//!   reported in the unmapped set.
//! - Rows whose `code` is missing or falsy (absent, null, "", 0,
//!   false, [], {}) pass through unchanged and are NOT reported as
//!   unmapped — the Python `if code:` guard.
//!
//! Truthiness and `str()` follow PYTHON semantics (the parity
//! target), not JavaScript/Rust ones: `""`/`0`/`false`/`[]`/`{}` are
//! falsy, `str(true)` is `"True"`, `str(None)` is `"None"`.

use std::collections::{BTreeSet, HashMap};

use serde_json::Value;

/// The unmapped report: the sorted, de-duplicated unmapped codes
/// (the Python step's `unmapped_codes`, a sorted set) plus the count
/// of ROWS that were unmapped (`unmapped_rows` — a code repeated on
/// three rows counts three rows but one code).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnmappedReport {
    pub codes: Vec<String>,
    pub rows: usize,
}

/// Full outcome of a normalisation pass.
#[derive(Debug, Clone, PartialEq)]
pub struct NormaliseOutcome {
    pub rows: Vec<Value>,
    pub unmapped: UnmappedReport,
    pub mapped: usize,
}

impl NormaliseOutcome {
    /// The `(rows, unmapped)` pair the contract names.
    pub fn into_parts(self) -> (Vec<Value>, UnmappedReport) {
        (self.rows, self.unmapped)
    }
}

/// Python truthiness for a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                true
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `str(value)` for the scalar shapes a code/system field can
/// realistically carry. Containers fall back to JSON text (Python's
/// repr of a list/dict differs in quoting; codes and systems are
/// strings/numbers in every governed code map, so this path is
/// unreachable for parity-relevant data).
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Python `str(value or "")`: falsy collapses to the empty string.
fn py_str_or_empty(v: Option<&Value>) -> String {
    match v {
        Some(val) if truthy(val) => py_str(val),
        _ => String::new(),
    }
}

/// Extract the mapping-entry list from a code-map value.
///
/// Accepted shapes (the first is the governed one — the published
/// `terminology/code-map` payload the Python step reads):
/// - `{"mappings": [ ...entries ]}` (a missing/null `mappings` is an
///   empty list, exactly like Python's `(payload.get("mappings") or [])`)
/// - `[ ...entries ]` (the list itself)
/// - `{"entries": [ ...entries ]}` or `{"entries": {from: to, ...}}`
/// - `{fromCode: toCode | {toCode, toSystem?}, ...}` (a bare map)
/// - `{}` (no mappings)
fn extract_mappings(code_map: &Value) -> Result<Vec<Value>, String> {
    match code_map {
        Value::Array(items) => Ok(items.clone()),
        Value::Object(obj) => {
            if let Some(m) = obj.get("mappings") {
                return match m {
                    Value::Null => Ok(Vec::new()),
                    Value::Array(items) => Ok(items.clone()),
                    other if !truthy(other) => Ok(Vec::new()),
                    _ => Err("codeMap.mappings must be an array".to_string()),
                };
            }
            if let Some(e) = obj.get("entries") {
                return match e {
                    Value::Null => Ok(Vec::new()),
                    Value::Array(items) => Ok(items.clone()),
                    Value::Object(map) => Ok(map_entries_to_mappings(map)),
                    other if !truthy(other) => Ok(Vec::new()),
                    _ => Err("codeMap.entries must be an array or an object".to_string()),
                };
            }
            if obj.is_empty() {
                return Ok(Vec::new());
            }
            // A bare {fromCode: ...} map.
            Ok(map_entries_to_mappings(obj))
        }
        Value::Null => Ok(Vec::new()),
        _ => Err("codeMap must be an object or an array of mapping entries".to_string()),
    }
}

fn map_entries_to_mappings(
    map: &serde_json::Map<String, Value>,
) -> Vec<Value> {
    let mut out = Vec::new();
    for (from_code, target) in map {
        let entry = match target {
            Value::String(to_code) => serde_json::json!({
                "fromCode": from_code, "toCode": to_code,
            }),
            Value::Object(t) => {
                let mut entry = t.clone();
                entry
                    .entry("fromCode".to_string())
                    .or_insert_with(|| Value::String(from_code.clone()));
                Value::Object(entry)
            }
            _ => continue,
        };
        out.push(entry);
    }
    out
}

/// Normalise `rows` through `code_map`, with semantics identical to
/// `pipelines._step_normalise` (see the module docstring).
///
/// `rows` must be an array of JSON objects; a non-object row is
/// malformed input and returns `Err` (the Python step would raise on
/// it too — `dict(row)` on a non-mapping fails the step).
pub fn normalise(code_map: &Value, rows: &[Value]) -> Result<NormaliseOutcome, String> {
    let mappings = extract_mappings(code_map)?;

    // Lookup construction — transliterated from the Python:
    //   lookup[(str(m.fromSystem or ""), str(m.fromCode))] = m
    //   lookup.setdefault(("", str(m.fromCode)), m)
    let mut specific: HashMap<(String, String), Value> = HashMap::new();
    let mut generic: HashMap<String, Value> = HashMap::new();
    for m in &mappings {
        let Some(obj) = m.as_object() else {
            continue; // isinstance(m, dict) guard
        };
        let Some(from_code_val) = obj.get("fromCode") else {
            continue;
        };
        if !truthy(from_code_val) {
            continue; // m.get("fromCode") guard
        }
        let from_code = py_str(from_code_val);
        let from_system = py_str_or_empty(obj.get("fromSystem"));
        specific.insert((from_system, from_code.clone()), m.clone());
        generic.entry(from_code).or_insert_with(|| m.clone());
    }

    let mut mapped = 0usize;
    let mut unmapped_rows = 0usize;
    let mut unmapped_codes: BTreeSet<String> = BTreeSet::new();
    let mut out: Vec<Value> = Vec::with_capacity(rows.len());

    for (idx, row) in rows.iter().enumerate() {
        let Some(row_obj) = row.as_object() else {
            return Err(format!("rows[{idx}] must be a JSON object"));
        };
        let mut new_row = row_obj.clone();
        // `code = row.get("code"); if code:` — missing/falsy passes through.
        if let Some(code_val) = row_obj.get("code").cloned() {
            if truthy(&code_val) {
                let code_str = py_str(&code_val);
                let system_str = py_str_or_empty(row_obj.get("system"));
                let hit = specific
                    .get(&(system_str, code_str.clone()))
                    .or_else(|| generic.get(&code_str));
                if let Some(hit) = hit {
                    let hit_obj = hit.as_object().cloned().unwrap_or_default();
                    new_row.insert("source_code".to_string(), code_val.clone());
                    // str(hit.get("toCode") or code)
                    let new_code = match hit_obj.get("toCode") {
                        Some(tc) if truthy(tc) => py_str(tc),
                        _ => py_str(&code_val),
                    };
                    new_row.insert("code".to_string(), Value::String(new_code));
                    if let Some(to_system) = hit_obj.get("toSystem") {
                        if truthy(to_system) {
                            let ts = py_str(to_system);
                            new_row.insert("system".to_string(), Value::String(ts));
                        }
                    }
                    new_row.insert("normalised".to_string(), Value::Bool(true));
                    mapped += 1;
                } else {
                    unmapped_rows += 1;
                    unmapped_codes.insert(code_str);
                }
            }
        }
        out.push(Value::Object(new_row));
    }

    Ok(NormaliseOutcome {
        rows: out,
        unmapped: UnmappedReport {
            codes: unmapped_codes.into_iter().collect(),
            rows: unmapped_rows,
        },
        mapped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn code_map() -> Value {
        json!({
            "mappings": [
                {"fromCode": "GLU", "fromSystem": "http://opencare.org/local/lab",
                 "toCode": "2345-7", "toSystem": "http://loinc.org"},
                {"fromCode": "HGB", "fromSystem": "http://opencare.org/local/lab",
                 "toCode": "718-7", "toSystem": "http://loinc.org"},
                // Same code, different source system -> different target.
                {"fromCode": "K", "fromSystem": "http://other.example/lab",
                 "toCode": "9999-9", "toSystem": "http://loinc.org"},
                {"fromCode": "K", "fromSystem": "http://opencare.org/local/lab",
                 "toCode": "2823-3", "toSystem": "http://loinc.org"},
            ]
        })
    }

    #[test]
    fn mapped_code_is_replaced() {
        let rows = vec![json!({"id": "obs-1", "code": "GLU", "value": 95})];
        let out = normalise(&code_map(), &rows).unwrap();
        assert_eq!(out.mapped, 1);
        assert_eq!(out.unmapped.rows, 0);
        assert!(out.unmapped.codes.is_empty());
        let row = &out.rows[0];
        assert_eq!(row["code"], json!("2345-7"));
        assert_eq!(row["source_code"], json!("GLU"));
        assert_eq!(row["normalised"], json!(true));
        // The extract row carried no `system`; the generic fallback
        // hit still applies the mapping's toSystem (Python parity:
        // `system` in the row is not a precondition for the rewrite).
        assert_eq!(row["system"], json!("http://loinc.org"));
        assert_eq!(row["value"], json!(95));
    }

    #[test]
    fn system_specific_entry_wins_over_generic() {
        // The generic ("", "K") slot holds the FIRST K entry (the
        // other.example one, by setdefault); a row naming the local
        // system must get the local target, a row naming the other
        // system gets its own, and a system-less row gets the first.
        let rows = vec![
            json!({"code": "K", "system": "http://opencare.org/local/lab"}),
            json!({"code": "K", "system": "http://other.example/lab"}),
            json!({"code": "K"}),
        ];
        let out = normalise(&code_map(), &rows).unwrap();
        assert_eq!(out.rows[0]["code"], json!("2823-3"));
        assert_eq!(out.rows[1]["code"], json!("9999-9"));
        assert_eq!(out.rows[2]["code"], json!("9999-9"));
        assert_eq!(out.mapped, 3);
    }

    #[test]
    fn unmapped_is_reported_and_row_unchanged() {
        let rows = vec![
            json!({"id": "obs-x", "code": "XYZ", "value": 1}),
            json!({"id": "obs-y", "code": "XYZ", "value": 2}),
            json!({"id": "obs-z", "code": "ABC", "value": 3}),
        ];
        let out = normalise(&code_map(), &rows).unwrap();
        assert_eq!(out.mapped, 0);
        // Three unmapped ROWS, two distinct unmapped CODES, sorted.
        assert_eq!(out.unmapped.rows, 3);
        assert_eq!(out.unmapped.codes, vec!["ABC".to_string(), "XYZ".to_string()]);
        // Rows pass through byte-for-byte (no source_code/normalised).
        assert_eq!(out.rows, rows);
    }

    #[test]
    fn rows_without_a_code_pass_through() {
        let rows = vec![
            json!({"id": "pat-a", "family": "Smith", "given": "Anna"}),
            json!({"id": "obs-empty", "code": ""}),
            json!({"id": "obs-null", "code": null}),
            json!({"id": "obs-zero", "code": 0}),
        ];
        let out = normalise(&code_map(), &rows).unwrap();
        assert_eq!(out.rows, rows);
        assert_eq!(out.mapped, 0);
        assert_eq!(out.unmapped.rows, 0);
        assert!(out.unmapped.codes.is_empty());
    }

    #[test]
    fn malformed_input_errors() {
        // A row that is not a JSON object is malformed.
        let rows = vec![json!({"code": "GLU"}), json!("not-an-object")];
        assert!(normalise(&code_map(), &rows).is_err());
        let rows = vec![json!(42)];
        assert!(normalise(&code_map(), &rows).is_err());
        // A codeMap whose mappings is not an array is malformed.
        let bad = json!({"mappings": "GLU->2345-7"});
        assert!(normalise(&bad, &[]).is_err());
        // A scalar codeMap is malformed.
        assert!(normalise(&json!("nope"), &[]).is_err());
    }

    #[test]
    fn empty_code_map_reports_everything_unmapped() {
        let rows = vec![json!({"code": "GLU"})];
        let out = normalise(&json!({}), &rows).unwrap();
        assert_eq!(out.mapped, 0);
        assert_eq!(out.unmapped.codes, vec!["GLU".to_string()]);
        assert_eq!(out.rows, rows);
    }

    #[test]
    fn entries_without_from_code_are_skipped() {
        let map = json!({"mappings": [
            {"toCode": "0000-0"},
            {"fromCode": "", "toCode": "1111-1"},
            "not-a-mapping",
            {"fromCode": "GLU", "toCode": "2345-7"},
        ]});
        let rows = vec![json!({"code": "GLU"})];
        let out = normalise(&map, &rows).unwrap();
        assert_eq!(out.rows[0]["code"], json!("2345-7"));
        // No toSystem on this entry -> the row gains no system field.
        assert!(out.rows[0].get("system").is_none());
    }
}
