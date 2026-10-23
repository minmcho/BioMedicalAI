//! Integration tests for opencare-deid. All fixtures are synthetic.

use opencare_deid::config::{Config, DateMode};
use opencare_deid::{check_json, check_text, deid_json_value, deid_text, TextEngine};
use serde_json::json;

fn cfg() -> Config {
    Config::default()
}

fn deid(input: &str) -> String {
    deid_text(&cfg(), "rec-1", input).unwrap()
}

// ---------- one test per text rule category ----------

#[test]
fn email_removed() {
    let out = deid("Reach Mai at mai.nguyen@example.org or mai.nguyen+lab@example.co.uk please");
    assert!(!out.contains("mai.nguyen"), "{out}");
    assert_eq!(out.matches("[EMAIL]").count(), 2, "{out}");
}

#[test]
fn phone_and_fax_removed() {
    let out = deid("Tel (415) 555-0132, alt 415-555-0143, fax: 415.555.0144, intl +1 415-555-0155");
    for frag in ["555-0132", "555-0143", "555.0144", "555-0155", "(415)"] {
        assert!(!out.contains(frag), "{frag} survived in {out}");
    }
    assert!(out.matches("[PHONE]").count() >= 4, "{out}");
}

#[test]
fn url_removed() {
    let out = deid("Chart at https://ehr.example.org/patients/123?x=1 and www.example.org/path end");
    assert!(!out.contains("ehr.example.org"), "{out}");
    assert!(!out.contains("www.example.org/path"), "{out}");
    assert_eq!(out.matches("[URL]").count(), 2, "{out}");
}

#[test]
fn ipv4_removed_but_invalid_left() {
    let out = deid("Device at 192.168.10.44 reported; version 999.10.1.1 is not an IP");
    assert!(!out.contains("192.168.10.44"), "{out}");
    assert!(out.contains("[IP]"), "{out}");
    assert!(out.contains("999.10.1.1"), "{out}");
}

#[test]
fn ssn_removed() {
    let out = deid("SSN on file 078-05-1120 verified");
    assert!(!out.contains("078-05-1120"), "{out}");
    assert!(out.contains("[SSN]"), "{out}");
}

#[test]
fn dates_year_only_all_formats() {
    let out = deid(
        "Admitted 2024-03-05, prior visit 03/07/2023, note dated March 9, 2022, \
         review 9 March 2021, labs 2020/11/30, follow-up 12-25-2019.",
    );
    for frag in ["2024-03-05", "03/07/2023", "March 9", "9 March", "2020/11/30", "12-25-2019"] {
        assert!(!out.contains(frag), "{frag} survived in {out}");
    }
    for year in ["2024", "2023", "2022", "2021", "2020", "2019"] {
        assert!(out.contains(year), "year {year} missing in {out}");
    }
}

#[test]
fn zip_truncated_to_three() {
    let out = deid("Lives in San Francisco 94110-2731, office ZIP 60614.");
    assert!(!out.contains("94110"), "{out}");
    assert!(!out.contains("2731"), "{out}");
    assert!(out.contains("94100"), "{out}");
    assert!(out.contains("60600"), "{out}");
}

#[test]
fn labelled_identifiers_removed() {
    let out = deid("MRN: A1234567 seen; Acct # 99887766 billed; Chart No.: CH-2211 filed.");
    for frag in ["A1234567", "99887766", "CH-2211"] {
        assert!(!out.contains(frag), "{frag} survived in {out}");
    }
    assert!(out.contains("MRN: [MRN]"), "{out}");
    assert!(out.contains("[ACCOUNT]"), "{out}");
}

#[test]
fn custom_pattern_applied() {
    let mut c = cfg();
    c.custom_patterns = vec![opencare_deid::config::CustomPattern {
        name: "study-id".into(),
        pattern: r"\bSTUDY-\d{6}\b".into(),
        replacement: "[STUDY]".into(),
    }];
    let out = deid_text(&c, "k", "Enrolled in STUDY-004201 cohort").unwrap();
    assert!(!out.contains("STUDY-004201"), "{out}");
    assert!(out.contains("[STUDY]"), "{out}");
}

// ---------- date shifting ----------

fn shift_cfg() -> Config {
    let mut c = cfg();
    c.date_mode = DateMode::Shift;
    c.shift_salt = "test-salt".into();
    c.shift_max_days = 180;
    c
}

#[test]
fn date_shift_is_deterministic_per_key() {
    let c = shift_cfg();
    let a = deid_text(&c, "patient-001", "Visit 2024-03-05, labs 2024-03-12").unwrap();
    let b = deid_text(&c, "patient-001", "Visit 2024-03-05, labs 2024-03-12").unwrap();
    assert_eq!(a, b);
    // Same shift for both dates => the 7-day interval is preserved.
    let dates: Vec<&str> = a.split(|ch: char| !(ch.is_ascii_digit() || ch == '-'))
        .filter(|s| s.len() == 10)
        .collect();
    assert_eq!(dates.len(), 2, "{a}");
    let d1 = parse_iso(dates[0]);
    let d2 = parse_iso(dates[1]);
    assert_eq!(d2 - d1, 7, "interval changed: {a}");
}

#[test]
fn date_shift_differs_across_keys_and_is_bounded() {
    let c = shift_cfg();
    let base = opencare_deid::dateutil::Date::new(2024, 6, 15).unwrap().to_days();
    let mut shifts = std::collections::HashSet::new();
    for i in 0..40 {
        let key = format!("patient-{i:03}");
        let out = deid_text(&c, &key, "Visit 2024-06-15").unwrap();
        let shifted = out
            .split(|ch: char| !(ch.is_ascii_digit() || ch == '-'))
            .find(|s| s.len() == 10)
            .unwrap_or_else(|| panic!("no shifted date in {out}"));
        let delta = parse_iso(shifted) - base;
        assert!((-180..=180).contains(&delta), "shift {delta} out of bound");
        shifts.insert(delta);
    }
    // Different keys must (overwhelmingly) get different shifts.
    assert!(shifts.len() > 20, "only {} distinct shifts", shifts.len());
    let direct_a = opencare_deid::dateutil::shift_days_for_key("test-salt", "patient-001", 180);
    let direct_b = opencare_deid::dateutil::shift_days_for_key("test-salt", "patient-002", 180);
    assert_ne!(direct_a, direct_b);
}

fn parse_iso(s: &str) -> i64 {
    let p: Vec<i64> = s.split('-').map(|x| x.parse().unwrap()).collect();
    opencare_deid::dateutil::Date::new(p[0], p[1], p[2])
        .unwrap()
        .to_days()
}

// ---------- JSON ----------

#[test]
fn json_key_transforms_and_structure_preserved() {
    let doc = json!({
        "id": "rec-9",
        "patient_name": "Mai Nguyen",
        "mrn": "A1234567",
        "ssn": "078-05-1120",
        "email": "mai.nguyen@example.org",
        "phone": "415-555-0132",
        "address": "1234 Grove Street",
        "city": "San Francisco",
        "zip": "94110-2731",
        "dob": "1985-04-12",
        "device_id": "SN-8842-X",
        "account_number": "99887766",
        "diagnosis": "Type 2 diabetes mellitus",
        "hba1c": 6.1,
        "medications": ["metformin", "lisinopril"],
        "active": true,
        "notes": null
    });
    let out = deid_json_value(&cfg(), &doc, None).unwrap();
    // Structure preserved: same keys, same array length, clinical values intact.
    let mut in_keys: Vec<&String> = doc.as_object().unwrap().keys().collect();
    let mut out_keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    in_keys.sort();
    out_keys.sort();
    assert_eq!(in_keys, out_keys);
    assert_eq!(out["medications"], json!(["metformin", "lisinopril"]));
    assert_eq!(out["hba1c"], json!(6.1));
    assert_eq!(out["diagnosis"], json!("Type 2 diabetes mellitus"));
    assert_eq!(out["active"], json!(true));
    assert_eq!(out["notes"], json!(null));
    // Identifier transforms.
    assert_eq!(out["patient_name"], json!("[NAME]"));
    assert_eq!(out["mrn"], json!("[MRN]"));
    assert_eq!(out["ssn"], json!("[SSN]"));
    assert_eq!(out["email"], json!("[EMAIL]"));
    assert_eq!(out["phone"], json!("[PHONE]"));
    assert_eq!(out["address"], json!("[ADDRESS]"));
    assert_eq!(out["city"], json!("[CITY]"));
    assert_eq!(out["zip"], json!("94100"));
    assert_eq!(out["dob"], json!("1985"));
    assert_eq!(out["device_id"], json!("[DEVICE]"));
    assert_eq!(out["account_number"], json!("[ACCOUNT]"));
}

#[test]
fn json_freetext_values_pass_through_text_rules() {
    let doc = json!({
        "note": "Call patient at 415-555-0132 or mai@example.org before 2024-03-05. ZIP 94110.",
        "code": "E11.9"
    });
    let out = deid_json_value(&cfg(), &doc, None).unwrap();
    let note = out["note"].as_str().unwrap();
    assert!(!note.contains("415-555-0132"), "{note}");
    assert!(!note.contains("mai@example.org"), "{note}");
    assert!(note.contains("2024"), "{note}");
    assert!(!note.contains("2024-03-05"), "{note}");
    assert!(out["code"] == json!("E11.9"));
}

#[test]
fn json_shift_mode_uses_record_key_field() {
    let c = shift_cfg();
    let doc = json!({"id": "patient-001", "dob": "1985-04-12", "service_date": "2024-03-05"});
    let a = deid_json_value(&c, &doc, None).unwrap();
    let b = deid_json_value(&c, &doc, None).unwrap();
    assert_eq!(a, b, "same record key must give identical output");
    assert_ne!(a["dob"], json!("1985-04-12"));
    // different id => (almost surely) different dob shift
    let doc2 = json!({"id": "patient-002", "dob": "1985-04-12", "service_date": "2024-03-05"});
    let other = deid_json_value(&c, &doc2, None).unwrap();
    assert_ne!(a["dob"], other["dob"]);
}

// ---------- end-to-end fixtures ----------

const NOTE: &str = "Progress note — Patient Name: Mai Nguyen (DOB 04/12/1985), MRN: A1234567, SSN: 078-05-1120. \
Address: 1234 Grove Street, San Francisco, CA 94110-2731. Phone (415) 555-0132, fax 415-555-0144, \
email mai.nguyen@example.org. Seen 2024-03-05 for type 2 diabetes; HbA1c 6.1 %. \
Continues metformin 500 mg twice daily. Portal: https://portal.example.org/mai — last login from 192.168.10.44. \
Follow-up scheduled March 20, 2024. Acct # 99887766 billed.";

#[test]
fn clinical_note_end_to_end() {
    let out = deid(NOTE);
    for frag in [
        "Mai Nguyen", "04/12/1985", "A1234567", "078-05-1120", "1234 Grove Street",
        "94110", "555-0132", "555-0144", "mai.nguyen@example.org", "2024-03-05",
        "portal.example.org", "192.168.10.44", "March 20, 2024", "99887766",
    ] {
        assert!(!out.contains(frag), "{frag} survived in:\n{out}");
    }
    // Clinical content survives.
    for keep in ["type 2 diabetes", "HbA1c 6.1 %", "metformin 500 mg"] {
        assert!(out.contains(keep), "{keep} lost in:\n{out}");
    }
    // Year-only dates: years remain.
    assert!(out.contains("1985"), "{out}");
    assert!(out.contains("2024"), "{out}");
}

#[test]
fn fhirish_bundle_end_to_end() {
    let bundle = json!({
        "resourceType": "Bundle",
        "id": "bundle-1",
        "entry": [
            {"resource": {
                "resourceType": "Patient",
                "id": "patient-001",
                "name": [{"family": "Nguyen", "given": ["Mai"]}],
                "birthDate": "1985-04-12",
                "telecom": [
                    {"system": "phone", "value": "415-555-0132"},
                    {"system": "email", "value": "mai.nguyen@example.org"}
                ],
                "address": [{"line": ["1234 Grove Street"], "city": "San Francisco", "postalCode": "94110-2731", "state": "CA"}],
                "identifier": [{"system": "urn:mrn", "value": "A1234567"}]
            }},
            {"resource": {
                "resourceType": "Observation",
                "id": "obs-1",
                "status": "final",
                "code": {"text": "HbA1c"},
                "effectiveDateTime": "2024-03-05T09:30:00Z",
                "valueQuantity": {"value": 6.1, "unit": "%"}
            }},
            {"resource": {
                "resourceType": "MedicationRequest",
                "id": "med-1",
                "medicationCodeableConcept": {"text": "metformin 500 mg"},
                "authoredOn": "2024-03-05",
                "note": [{"text": "Patient called from 415-555-0132 on 2024-03-04 about refills."}]
            }}
        ]
    });
    let out = deid_json_value(&cfg(), &bundle, None).unwrap();
    let s = serde_json::to_string(&out).unwrap();
    for frag in ["Nguyen", "Mai", "1985-04-12", "415-555-0132", "mai.nguyen@example.org",
                 "1234 Grove Street", "San Francisco", "94110", "2024-03-05", "2024-03-04"] {
        assert!(!s.contains(frag), "{frag} survived in:\n{s}");
    }
    // Clinical content + structure survive.
    assert_eq!(out["entry"][1]["resource"]["valueQuantity"]["value"], json!(6.1));
    assert_eq!(out["entry"][1]["resource"]["code"]["text"], json!("HbA1c"));
    assert_eq!(out["entry"][2]["resource"]["medicationCodeableConcept"]["text"], json!("metformin 500 mg"));
    assert_eq!(out["entry"].as_array().unwrap().len(), 3);
    assert!(s.contains("1985"), "{s}");
    assert!(s.contains("2024"), "{s}");
    // State is NOT smaller-than-state geography: it survives by design.
    assert_eq!(out["entry"][0]["resource"]["address"][0]["state"], json!("CA"));
}

// ---------- config behaviour ----------

#[test]
fn disabled_rule_leaves_category_untouched() {
    let mut c = cfg();
    c.rules.email = false;
    let out = deid_text(&c, "k", "Mail mai@example.org, call 415-555-0132").unwrap();
    assert!(out.contains("mai@example.org"), "{out}");
    assert!(!out.contains("415-555-0132"), "{out}");
}

#[test]
fn disabled_json_keys_leaves_key_values_but_text_rules_still_apply() {
    let mut c = cfg();
    c.rules.json_keys = false;
    let doc = json!({"patient_name": "MaiNguyen", "city": "Springfield"});
    let out = deid_json_value(&c, &doc, None).unwrap();
    assert_eq!(out["patient_name"], json!("MaiNguyen"));
    assert_eq!(out["city"], json!("Springfield"));
}

#[test]
fn check_mode_counts_without_output() {
    let counts = check_text(&cfg(), NOTE).unwrap();
    assert!(counts.get("email").copied().unwrap_or(0) >= 1, "{counts:?}");
    assert!(counts.get("ssn").copied().unwrap_or(0) >= 1, "{counts:?}");
    assert!(counts.get("phone").copied().unwrap_or(0) >= 2, "{counts:?}");
    assert!(counts.get("date").copied().unwrap_or(0) >= 2, "{counts:?}");
    let jcounts = check_json(&cfg(), &json!({"mrn": "A1", "note": "mail a@b.co"})).unwrap();
    assert_eq!(jcounts.get("json_key:mrn").copied().unwrap_or(0), 1, "{jcounts:?}");
    assert!(jcounts.get("email").copied().unwrap_or(0) >= 1, "{jcounts:?}");
}

#[test]
fn engine_reuse_matches_one_shot() {
    let c = cfg();
    let e = TextEngine::new(&c).unwrap();
    assert_eq!(e.deid_text(&c, "k", NOTE), deid(NOTE));
}
