//! CLI tests for the opencare-term binary (Phase 16, BD-50).
//! Synthetic fixtures only. The contract under test:
//!   opencare-term normalise --format json
//!   stdin  {"codeMap": {...}, "rows": [...]}
//!   stdout {"rows": [...], "unmapped": [...], ...}
//! Invalid input => non-zero exit + a stderr message.

use std::io::Write;
use std::process::{Command, Stdio};

fn run(args: &[&str], stdin_data: &str) -> (i32, String, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_opencare-term"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    // Coordinator adjustment (Phase 16 gate): tolerate a broken
    // pipe here — the usage-error cases make the child exit before
    // stdin is read, and whether the write lands first is a
    // scheduling race (the gate run lost it: BrokenPipe panic).
    // The assertions below are on exit code + stderr, which
    // wait_with_output still collects; on the success paths a lost
    // write would fail those assertions, so nothing is weakened.
    let _ = child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(stdin_data.as_bytes());
    let out = child.wait_with_output().unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

const INPUT: &str = r#"{
  "codeMap": {"mappings": [
    {"fromCode": "GLU", "fromSystem": "http://opencare.org/local/lab",
     "toCode": "2345-7", "toSystem": "http://loinc.org"}
  ]},
  "rows": [
    {"id": "obs-1", "code": "GLU", "value": 95},
    {"id": "obs-2", "code": "XYZ", "value": 1},
    {"id": "pat-a", "family": "Smith"}
  ]
}"#;

#[test]
fn cli_normalise_stdin_stdout() {
    let (code, stdout, stderr) = run(&["normalise", "--format", "json"], INPUT);
    assert_eq!(code, 0, "{stderr}");
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    let rows = v["rows"].as_array().unwrap();
    assert_eq!(rows[0]["code"], serde_json::json!("2345-7"));
    assert_eq!(rows[0]["source_code"], serde_json::json!("GLU"));
    assert_eq!(rows[0]["normalised"], serde_json::json!(true));
    // Unmapped row and code-less row pass through unchanged.
    assert_eq!(rows[1], serde_json::json!({"id": "obs-2", "code": "XYZ", "value": 1}));
    assert_eq!(rows[2], serde_json::json!({"id": "pat-a", "family": "Smith"}));
    assert_eq!(v["unmapped"], serde_json::json!(["XYZ"]));
    assert_eq!(v["unmapped_codes"], serde_json::json!(["XYZ"]));
    assert_eq!(v["mapped"], serde_json::json!(1));
    assert_eq!(v["unmapped_rows"], serde_json::json!(1));
}

#[test]
fn cli_malformed_input_fails_with_stderr() {
    // Not JSON at all.
    let (code, stdout, stderr) = run(&["normalise", "--format", "json"], "{not json");
    assert_ne!(code, 0);
    assert!(stdout.is_empty(), "{stdout}");
    assert!(stderr.contains("opencare-term:"), "{stderr}");
    // Valid JSON, missing rows.
    let (code, _, stderr) = run(
        &["normalise", "--format", "json"],
        r#"{"codeMap": {"mappings": []}}"#,
    );
    assert_ne!(code, 0);
    assert!(!stderr.is_empty());
    // Rows that are not objects.
    let (code, _, stderr) = run(
        &["normalise", "--format", "json"],
        r#"{"codeMap": {"mappings": []}, "rows": [42]}"#,
    );
    assert_ne!(code, 0);
    assert!(!stderr.is_empty());
}

#[test]
fn cli_usage_errors() {
    // Missing subcommand.
    let (code, _, _) = run(&["--format", "json"], "{}");
    assert_eq!(code, 1);
    // Unsupported format.
    let (code, _, _) = run(&["normalise", "--format", "text"], "{}");
    assert_eq!(code, 1);
    // Unknown subcommand.
    let (code, _, _) = run(&["deid", "--format", "json"], "{}");
    assert_eq!(code, 1);
}
