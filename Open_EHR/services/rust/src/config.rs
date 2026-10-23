//! JSON configuration for the de-identification step. Config is data:
//! the binary takes `--config <file>`; every rule can be toggled and every
//! replacement string / key list / label pattern is configurable.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DateMode {
    /// Safe Harbor shape: reduce every date to its year.
    YearOnly,
    /// Limited-dataset style alternative: shift all dates of one record
    /// by the same deterministic per-key amount.
    Shift,
}

impl Default for DateMode {
    fn default() -> Self {
        DateMode::YearOnly
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Rules {
    pub email: bool,
    pub phone: bool,
    pub url: bool,
    pub ipv4: bool,
    pub ssn: bool,
    pub dates: bool,
    pub zip: bool,
    pub label_identifiers: bool,
    pub custom_patterns: bool,
    /// JSON key-based transforms (names, address keys, dob keys, ...).
    pub json_keys: bool,
}

impl Default for Rules {
    fn default() -> Self {
        Rules {
            email: true,
            phone: true,
            url: true,
            ipv4: true,
            ssn: true,
            dates: true,
            zip: true,
            label_identifiers: true,
            custom_patterns: true,
            json_keys: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Replacements {
    pub email: String,
    pub phone: String,
    pub url: String,
    pub ipv4: String,
    pub ssn: String,
    pub name: String,
    pub address: String,
    pub city: String,
    pub mrn: String,
    pub account: String,
    pub device: String,
    pub identifier: String,
}

impl Default for Replacements {
    fn default() -> Self {
        Replacements {
            email: "[EMAIL]".into(),
            phone: "[PHONE]".into(),
            url: "[URL]".into(),
            ipv4: "[IP]".into(),
            ssn: "[SSN]".into(),
            name: "[NAME]".into(),
            address: "[ADDRESS]".into(),
            city: "[CITY]".into(),
            mrn: "[MRN]".into(),
            account: "[ACCOUNT]".into(),
            device: "[DEVICE]".into(),
            identifier: "[ID]".into(),
        }
    }
}

/// JSON keys (matched case-insensitively, ignoring `_`/`-`/spaces) whose
/// values receive a category transform.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct IdentifierKeys {
    pub name: Vec<String>,
    pub address: Vec<String>,
    pub city: Vec<String>,
    pub zip: Vec<String>,
    pub ssn: Vec<String>,
    pub mrn: Vec<String>,
    pub account: Vec<String>,
    pub device: Vec<String>,
    pub email: Vec<String>,
    pub phone: Vec<String>,
    pub url: Vec<String>,
    pub ipv4: Vec<String>,
    /// Explicit date keys; additionally, any key whose normalised form
    /// ends in `date`/`datetime`, or contains `birth`, is treated as a
    /// date (see `json_deid::key_category`).
    pub date: Vec<String>,
}

fn v(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

impl Default for IdentifierKeys {
    fn default() -> Self {
        IdentifierKeys {
            name: v(&[
                "name", "patientname", "fullname", "firstname", "lastname", "middlename",
                "givenname", "familyname", "family", "given", "mothername", "mothersmaidenname",
                "contactname", "emergencycontactname", "subscribername", "guarantorname",
            ]),
            address: v(&[
                "address", "street", "streetaddress", "addressline1", "addressline2",
                "mailingaddress", "homeaddress", "billingaddress",
                // FHIR Address.line (street lines). Removing "line" from
                // this list in config disables that if a payload uses
                // "line" for something else.
                "line",
            ]),
            city: v(&["city", "town", "cityname"]),
            zip: v(&["zip", "zipcode", "postalcode", "postal"]),
            ssn: v(&["ssn", "socialsecuritynumber", "socialsecurity"]),
            mrn: v(&[
                "mrn", "medicalrecordnumber", "medicalrecord", "chartnumber", "patientmrn",
            ]),
            account: v(&[
                "account", "accountnumber", "healthplannumber", "healthplanid", "memberid",
                "policynumber", "insuranceid", "subscriberid", "claimnumber", "visitnumber",
                "encounternumber",
            ]),
            device: v(&[
                "deviceid", "deviceidentifier", "serialnumber", "udi", "deviceudi",
                "implantid",
            ]),
            email: v(&["email", "emailaddress", "mail"]),
            phone: v(&[
                "phone", "phonenumber", "telephone", "mobile", "cellphone", "fax",
                "faxnumber", "contactphone", "homenumber", "workphone",
            ]),
            url: v(&["url", "website", "webpage", "homepage"]),
            ipv4: v(&["ip", "ipaddress", "ipv4", "ipv4address", "hostaddress"]),
            date: v(&[
                "dob", "dateofbirth", "birthdate", "birthdatetime", "dod", "dateofdeath",
            ]),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CustomPattern {
    pub name: String,
    pub pattern: String,
    #[serde(default = "default_custom_replacement")]
    pub replacement: String,
}

fn default_custom_replacement() -> String {
    "[ID]".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub rules: Rules,
    pub date_mode: DateMode,
    /// Salt for the deterministic per-record-key date shift. Change it
    /// per deployment / extract; it is configuration, never a secret key
    /// store — anyone holding the config can recompute shifts.
    pub shift_salt: String,
    /// Bound for the date shift: shifts land in [-max, +max] days.
    pub shift_max_days: i64,
    /// Only supported value: "truncate3" (keep first 3 ZIP digits,
    /// zero the rest, drop +4).
    pub zip_mode: String,
    pub replacements: Replacements,
    /// Label prefixes whose following token is an identifier, e.g.
    /// "MRN:", "Acct #", "Chart No.". Matched case-insensitively.
    pub label_patterns: Vec<String>,
    pub identifier_keys: IdentifierKeys,
    pub custom_patterns: Vec<CustomPattern>,
    /// JSON field (top level) whose value keys the date shift when the
    /// CLI `--record-key` is not given. Default "id"; set null to disable.
    pub record_key_field: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            rules: Rules::default(),
            date_mode: DateMode::YearOnly,
            shift_salt: "opencare-deid-default-salt".into(),
            shift_max_days: 180,
            zip_mode: "truncate3".into(),
            replacements: Replacements::default(),
            label_patterns: v(&[
                "Patient Name:", "Name:", "Address:", "Home Address:", "Mailing Address:",
                "MRN:", "MRN #", "Medical Record Number:", "Medical Record No.:",
                "Chart #:", "Chart No.:", "Acct #", "Acct:", "Account #", "Account No.:",
                "Member ID:", "Policy #:", "Claim #:", "Device ID:", "Serial #:",
                "Serial No.:", "Visit #:", "Encounter #:", "SSN:", "Social Security #:",
            ]),
            identifier_keys: IdentifierKeys::default(),
            custom_patterns: Vec::new(),
            record_key_field: Some("id".into()),
        }
    }
}

impl Config {
    pub fn from_json_str(s: &str) -> Result<Config, String> {
        let cfg: Config =
            serde_json::from_str(s).map_err(|e| format!("invalid config JSON: {e}"))?;
        cfg.validate()?;
        Ok(cfg)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.zip_mode != "truncate3" {
            return Err(format!(
                "unsupported zip_mode {:?} (only \"truncate3\" is implemented)",
                self.zip_mode
            ));
        }
        if self.shift_max_days < 0 {
            return Err("shift_max_days must be >= 0".into());
        }
        for p in &self.custom_patterns {
            regex::Regex::new(&p.pattern)
                .map_err(|e| format!("invalid custom pattern {:?}: {e}", p.name))?;
        }
        Ok(())
    }
}
