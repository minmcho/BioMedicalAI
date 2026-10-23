//! Regex-based free-text de-identification. Each category is an
//! individually toggleable rule (see `Config.rules`). Rule order matters
//! and is fixed here: URLs and emails first (they can contain digits and
//! dots), then IPs, SSNs, labelled identifiers, phones, dates, ZIPs, and
//! finally operator-supplied custom patterns.

use std::collections::BTreeMap;

use regex::{Captures, Regex};

use crate::config::{Config, DateMode};
use crate::dateutil::{month_from_name, shift_days_for_key, Date};

const MONTHS: &str = "January|February|March|April|May|June|July|August|September|October|November|December|Jan\\.?|Feb\\.?|Mar\\.?|Apr\\.?|Jun\\.?|Jul\\.?|Aug\\.?|Sep\\.?|Sept\\.?|Oct\\.?|Nov\\.?|Dec\\.?";

pub struct TextEngine {
    url: Regex,
    email: Regex,
    ipv4: Regex,
    ssn: Regex,
    phone: Regex,
    date_iso: Regex,
    date_ymd_slash: Regex,
    date_mdy_slash: Regex,
    date_mdy_dash: Regex,
    date_long_mdy: Regex,
    date_long_dmy: Regex,
    zip: Regex,
    labels: Vec<(Regex, String)>, // (pattern, replacement)
    custom: Vec<(String, Regex, String)>,
}

fn is_name_label(label: &str) -> bool {
    label.to_ascii_lowercase().contains("name")
}

fn is_address_label(label: &str) -> bool {
    label.to_ascii_lowercase().contains("address")
}

fn label_replacement(label: &str, cfg: &Config) -> String {
    let l = label.to_ascii_lowercase();
    let r = &cfg.replacements;
    if is_name_label(label) {
        r.name.clone()
    } else if is_address_label(label) {
        r.address.clone()
    } else if l.contains("mrn") || l.contains("medical record") || l.contains("chart") {
        r.mrn.clone()
    } else if l.contains("acct")
        || l.contains("account")
        || l.contains("member")
        || l.contains("policy")
        || l.contains("claim")
        || l.contains("visit")
        || l.contains("encounter")
    {
        r.account.clone()
    } else if l.contains("device") || l.contains("serial") {
        r.device.clone()
    } else if l.contains("ssn") || l.contains("social") {
        r.ssn.clone()
    } else {
        r.identifier.clone()
    }
}

impl TextEngine {
    pub fn new(cfg: &Config) -> Result<TextEngine, String> {
        let build = |p: &str| Regex::new(p).map_err(|e| format!("internal pattern error: {e}"));
        let mut labels = Vec::new();
        for label in &cfg.label_patterns {
            // Name labels consume up to three capitalised name tokens
            // ("Patient Name: Mai Thi Nguyen"); address labels consume
            // the street segment up to the next comma/semicolon/newline;
            // all other labels consume exactly one identifier token.
            let value_pat = if is_name_label(label) {
                r"([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,2})"
            } else if is_address_label(label) {
                r"([^\n,;]+)"
            } else {
                r"([A-Za-z0-9][A-Za-z0-9\-]*)"
            };
            let pat = format!(r"(?i)({})\s*{}", regex::escape(label), value_pat);
            labels.push((build(&pat)?, label_replacement(label, cfg)));
        }
        let mut custom = Vec::new();
        for p in &cfg.custom_patterns {
            custom.push((
                p.name.clone(),
                Regex::new(&p.pattern).map_err(|e| format!("custom pattern {:?}: {e}", p.name))?,
                p.replacement.clone(),
            ));
        }
        Ok(TextEngine {
            url: build(r#"(?i)\b(?:https?://|www\.)[^\s<>"'\)\]]+"#)?,
            email: build(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")?,
            ipv4: build(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")?,
            ssn: build(r"\b\d{3}-\d{2}-\d{4}\b")?,
            phone: build(
                r"(?:\+1[\s.\-]?)?(?:\(\d{3}\)\s?|\b\d{3}[\s.\-])\d{3}[\s.\-]\d{4}\b|\b\d{10}\b",
            )?,
            date_iso: build(
                r"\b(\d{4})-(\d{2})-(\d{2})([T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:?\d{2})?)?",
            )?,
            date_ymd_slash: build(r"\b(\d{4})/(\d{2})/(\d{2})\b")?,
            date_mdy_slash: build(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")?,
            date_mdy_dash: build(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")?,
            date_long_mdy: build(&format!(
                r"(?i)\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b"
            ))?,
            date_long_dmy: build(&format!(
                r"(?i)\b(\d{{1,2}})\s+({MONTHS})\s+(\d{{4}})\b"
            ))?,
            zip: build(r"\b(\d{5})(?:-\d{4})?\b")?,
            labels,
            custom,
        })
    }

    fn shift(&self, cfg: &Config, record_key: &str) -> i64 {
        shift_days_for_key(&cfg.shift_salt, record_key, cfg.shift_max_days)
    }

    /// Transform one validated date; invalid dates are returned verbatim
    /// by callers (the closures leave the match unchanged).
    fn render_date(
        &self,
        cfg: &Config,
        record_key: &str,
        date: Date,
        time_suffix: &str,
    ) -> String {
        match cfg.date_mode {
            DateMode::YearOnly => format!("{:04}", date.year),
            DateMode::Shift => {
                let shifted = date.add_days(self.shift(cfg, record_key));
                format!("{}{}", shifted.format_iso(), time_suffix)
            }
        }
    }

    fn apply_dates(&self, cfg: &Config, record_key: &str, text: &str) -> String {
        let shift = self.shift(cfg, record_key);
        let render = |cfg: &Config, d: Date, suffix: &str| -> String {
            match cfg.date_mode {
                DateMode::YearOnly => format!("{:04}", d.year),
                DateMode::Shift => format!("{}{}", d.add_days(shift).format_iso(), suffix),
            }
        };
        let out = self
            .date_iso
            .replace_all(text, |c: &Captures| {
                let (y, m, d) = (num(c, 1), num(c, 2), num(c, 3));
                match Date::new(y, m, d) {
                    Some(date) => render(cfg, date, c.get(4).map(|g| g.as_str()).unwrap_or("")),
                    None => c[0].to_string(),
                }
            })
            .into_owned();
        let out = self
            .date_ymd_slash
            .replace_all(&out, |c: &Captures| {
                let (y, m, d) = (num(c, 1), num(c, 2), num(c, 3));
                match Date::new(y, m, d) {
                    Some(date) => render(cfg, date, ""),
                    None => c[0].to_string(),
                }
            })
            .into_owned();
        let mdy = |c: &Captures| {
            let (m, d, y) = (num(c, 1), num(c, 2), num(c, 3));
            match Date::new(y, m, d) {
                Some(date) => render(cfg, date, ""),
                None => c[0].to_string(),
            }
        };
        let out = self.date_mdy_slash.replace_all(&out, mdy).into_owned();
        let out = self.date_mdy_dash.replace_all(&out, mdy).into_owned();
        let out = self
            .date_long_mdy
            .replace_all(&out, |c: &Captures| {
                let m = month_from_name(&c[1]);
                let (d, y) = (num(c, 2), num(c, 3));
                match (m, Date::new(y, m.unwrap_or(0), d)) {
                    (Some(_), Some(date)) => render(cfg, date, ""),
                    _ => c[0].to_string(),
                }
            })
            .into_owned();
        let out = self
            .date_long_dmy
            .replace_all(&out, |c: &Captures| {
                let m = month_from_name(&c[2]);
                let (d, y) = (num(c, 1), num(c, 3));
                match (m, Date::new(y, m.unwrap_or(0), d)) {
                    (Some(_), Some(date)) => render(cfg, date, ""),
                    _ => c[0].to_string(),
                }
            })
            .into_owned();
        out
    }

    /// De-identify one free-text string. `record_key` keys the date shift
    /// (ignored in year-only mode).
    pub fn deid_text(&self, cfg: &Config, record_key: &str, text: &str) -> String {
        let r = &cfg.replacements;
        let rules = &cfg.rules;
        let mut out = text.to_string();
        if rules.url {
            out = self.url.replace_all(&out, r.url.as_str()).into_owned();
        }
        if rules.email {
            out = self.email.replace_all(&out, r.email.as_str()).into_owned();
        }
        if rules.ipv4 {
            out = self
                .ipv4
                .replace_all(&out, |c: &Captures| {
                    if valid_ipv4(&c[0]) {
                        r.ipv4.clone()
                    } else {
                        c[0].to_string()
                    }
                })
                .into_owned();
        }
        if rules.ssn {
            out = self.ssn.replace_all(&out, r.ssn.as_str()).into_owned();
        }
        if rules.label_identifiers {
            for (re, replacement) in &self.labels {
                out = re
                    .replace_all(&out, |c: &Captures| {
                        format!("{} {}", &c[1], replacement)
                    })
                    .into_owned();
            }
        }
        if rules.phone {
            out = self.phone.replace_all(&out, r.phone.as_str()).into_owned();
        }
        if rules.dates {
            out = self.apply_dates(cfg, record_key, &out);
        }
        if rules.zip {
            out = self
                .zip
                .replace_all(&out, |c: &Captures| format!("{}00", &c[1][..3]))
                .into_owned();
        }
        if rules.custom_patterns {
            for (_, re, replacement) in &self.custom {
                out = re.replace_all(&out, replacement.as_str()).into_owned();
            }
        }
        out
    }

    /// Count matches per category without producing de-identified output
    /// (the pipeline's `check` / DQ-gating mode).
    pub fn count_text(&self, cfg: &Config, text: &str) -> BTreeMap<String, usize> {
        let rules = &cfg.rules;
        let mut counts: BTreeMap<String, usize> = BTreeMap::new();
        let mut add = |k: &str, n: usize| {
            if n > 0 {
                *counts.entry(k.to_string()).or_insert(0) += n;
            }
        };
        if rules.url {
            add("url", self.url.find_iter(text).count());
        }
        if rules.email {
            add("email", self.email.find_iter(text).count());
        }
        if rules.ipv4 {
            add(
                "ipv4",
                self.ipv4.find_iter(text).filter(|m| valid_ipv4(m.as_str())).count(),
            );
        }
        if rules.ssn {
            add("ssn", self.ssn.find_iter(text).count());
        }
        if rules.label_identifiers {
            let n: usize = self.labels.iter().map(|(re, _)| re.find_iter(text).count()).sum();
            add("label_identifier", n);
        }
        if rules.phone {
            add("phone", self.phone.find_iter(text).count());
        }
        if rules.dates {
            let n = self.date_iso.find_iter(text).count()
                + self.date_ymd_slash.find_iter(text).count()
                + self.date_mdy_slash.find_iter(text).count()
                + self.date_mdy_dash.find_iter(text).count()
                + self.date_long_mdy.find_iter(text).count()
                + self.date_long_dmy.find_iter(text).count();
            add("date", n);
        }
        if rules.zip {
            add("zip", self.zip.find_iter(text).count());
        }
        if rules.custom_patterns {
            for (name, re, _) in &self.custom {
                add(&format!("custom:{name}"), re.find_iter(text).count());
            }
        }
        counts
    }
}

fn num(c: &Captures, i: usize) -> i64 {
    c[i].parse().unwrap_or(0)
}

fn valid_ipv4(s: &str) -> bool {
    s.split('.').all(|p| p.parse::<u32>().map(|n| n <= 255).unwrap_or(false))
        && s.split('.').count() == 4
}

/// Public helper used by the JSON walker for single-date string values:
/// returns the transformed string if `s` is exactly one date (optionally
/// with an ISO time suffix), else None.
pub fn transform_single_date(
    engine: &TextEngine,
    cfg: &Config,
    record_key: &str,
    s: &str,
) -> Option<String> {
    let trimmed = s.trim();
    // ISO date / datetime
    if let Some(c) = engine.date_iso.captures(trimmed) {
        if c[0].len() == trimmed.len() {
            let date = Date::new(num(&c, 1), num(&c, 2), num(&c, 3))?;
            return Some(engine.render_date(
                cfg,
                record_key,
                date,
                c.get(4).map(|g| g.as_str()).unwrap_or(""),
            ));
        }
    }
    for re in [&engine.date_ymd_slash] {
        if let Some(c) = re.captures(trimmed) {
            if c[0].len() == trimmed.len() {
                let date = Date::new(num(&c, 1), num(&c, 2), num(&c, 3))?;
                return Some(engine.render_date(cfg, record_key, date, ""));
            }
        }
    }
    for re in [&engine.date_mdy_slash, &engine.date_mdy_dash] {
        if let Some(c) = re.captures(trimmed) {
            if c[0].len() == trimmed.len() {
                let date = Date::new(num(&c, 3), num(&c, 1), num(&c, 2))?;
                return Some(engine.render_date(cfg, record_key, date, ""));
            }
        }
    }
    if let Some(c) = engine.date_long_mdy.captures(trimmed) {
        if c[0].len() == trimmed.len() {
            let m = month_from_name(&c[1])?;
            let date = Date::new(num(&c, 3), m, num(&c, 2))?;
            return Some(engine.render_date(cfg, record_key, date, ""));
        }
    }
    if let Some(c) = engine.date_long_dmy.captures(trimmed) {
        if c[0].len() == trimmed.len() {
            let m = month_from_name(&c[2])?;
            let date = Date::new(num(&c, 3), m, num(&c, 1))?;
            return Some(engine.render_date(cfg, record_key, date, ""));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    fn engine(cfg: &Config) -> TextEngine {
        TextEngine::new(cfg).unwrap()
    }

    #[test]
    fn invalid_ipv4_left_alone() {
        let cfg = Config::default();
        let e = engine(&cfg);
        assert_eq!(e.deid_text(&cfg, "k", "version 999.1.1.1 stays"), "version 999.1.1.1 stays");
        assert_eq!(e.deid_text(&cfg, "k", "host 10.0.0.7 gone"), "host [IP] gone");
    }

    #[test]
    fn invalid_date_left_alone() {
        let cfg = Config::default();
        let e = engine(&cfg);
        // 2023-02-29 does not exist; a rule must not rewrite non-dates.
        assert_eq!(e.deid_text(&cfg, "k", "code 2023-02-29 x"), "code 2023-02-29 x");
    }

    #[test]
    fn clinical_numbers_survive() {
        let cfg = Config::default();
        let e = engine(&cfg);
        let s = "HbA1c 6.1 %, dose 500 mg, ratio 1/2 tablet, BP 120/80";
        assert_eq!(e.deid_text(&cfg, "k", s), s);
    }
}
