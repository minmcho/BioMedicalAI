//! opencare-deid — rule-based de-identification step for OpenCare
//! research extracts.
//!
//! Method (stated precisely): rule-based, HIPAA Safe Harbor-*style*
//! identifier removal / generalisation (45 CFR 164.514(b)(2) categories
//! that are mechanically implementable in text/JSON), driven entirely by
//! a JSON config. Date handling has two modes: `year-only` (the Safe
//! Harbor shape) and `shift` (a deterministic per-record-key shift — the
//! limited-dataset style alternative).
//!
//! This crate is NOT an expert-certification determination under
//! 164.514(b)(1), and using it is NOT a compliance guarantee. See
//! `services/rust/README.md`.

pub mod config;
pub mod dateutil;
pub mod json_deid;
pub mod term;
pub mod text;

pub use config::{Config, DateMode};
pub use json_deid::{count_json, deid_json};
pub use term::normalise as term_normalise;
pub use text::TextEngine;

use std::collections::BTreeMap;

/// De-identify free text with a fresh engine built from `cfg`.
pub fn deid_text(cfg: &Config, record_key: &str, input: &str) -> Result<String, String> {
    let engine = TextEngine::new(cfg)?;
    Ok(engine.deid_text(cfg, record_key, input))
}

/// Count identifier-category hits in free text (check / DQ-gate mode).
pub fn check_text(cfg: &Config, input: &str) -> Result<BTreeMap<String, usize>, String> {
    let engine = TextEngine::new(cfg)?;
    Ok(engine.count_text(cfg, input))
}

/// Count identifier-category hits in a JSON document (check mode).
pub fn check_json(
    cfg: &Config,
    doc: &serde_json::Value,
) -> Result<BTreeMap<String, usize>, String> {
    let engine = TextEngine::new(cfg)?;
    Ok(count_json(&engine, cfg, doc))
}

/// De-identify a JSON document (structure preserved).
pub fn deid_json_value(
    cfg: &Config,
    doc: &serde_json::Value,
    explicit_record_key: Option<&str>,
) -> Result<serde_json::Value, String> {
    let engine = TextEngine::new(cfg)?;
    Ok(deid_json(&engine, cfg, doc, explicit_record_key))
}

/// Total hits across a counts map.
pub fn total_hits(counts: &BTreeMap<String, usize>) -> usize {
    counts.values().sum()
}
