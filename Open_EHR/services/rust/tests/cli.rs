//! CLI tests for the opencare-deid binary (synthetic fixtures only).

use std::io::Write;
use std::process::{Command, Stdio};

fn run(args: &[&str], stdin_data: &str) -> (i32, String, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_opencare-deid"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(stdin_data.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

fn tmpfile(name: &str, content: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("opencare-deid-cli-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join(name);
    std::fs::write(&p, content).unwrap();
    p
}

#[test]
fn cli_deid_text_stdin_stdout() {
    let (code, stdout, _) = run(&["deid"], "Mail mai@example.org on 2024-03-05");
    assert_eq!(code, 0);
    assert!(stdout.contains("[EMAIL]"), "{stdout}");
    assert!(stdout.contains("2024") && !stdout.contains("2024-03-05"), "{stdout}");
}

#[test]
fn cli_deid_json_file_with_config() {
    let cfg = tmpfile(
        "cfg.json",
        r#"{"date_mode": "year-only", "shift_salt": "s", "replacements": {"name": "[PATIENT]"}}"#,
    );
    let input = tmpfile(
        "in.json",
        r#"{"patient_name": "Mai Nguyen", "dob": "1985-04-12", "hba1c": 6.1}"#,
    );
    let (code, stdout, stderr) = run(
        &[
            "--config",
            cfg.to_str().unwrap(),
            "deid",
            "--in",
            input.to_str().unwrap(),
            "--out",
            "-",
        ],
        "",
    );
    assert_eq!(code, 0, "{stderr}");
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(v["patient_name"], serde_json::json!("[PATIENT]"));
    assert_eq!(v["dob"], serde_json::json!("1985"));
    assert_eq!(v["hba1c"], serde_json::json!(6.1));
}

#[test]
fn cli_check_reports_counts_only() {
    let (code, stdout, _) = run(
        &["check"],
        "SSN 078-05-1120, mail mai@example.org, call 415-555-0132",
    );
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert!(v["total"].as_u64().unwrap() >= 3, "{v}");
    assert_eq!(v["counts"]["ssn"], serde_json::json!(1));
    // check mode must not emit de-identified payload text
    assert!(!stdout.contains("[SSN]"), "{stdout}");
}

#[test]
fn cli_exit_codes() {
    // usage error
    let (code, _, _) = run(&["bogus-subcommand"], "");
    assert_eq!(code, 1);
    // config error
    let bad = tmpfile("bad.json", "{not json");
    let (code, _, _) = run(&["--config", bad.to_str().unwrap(), "deid"], "x");
    assert_eq!(code, 2);
    // io error
    let (code, _, _) = run(&["deid", "--in", "/nonexistent/payload.txt"], "");
    assert_eq!(code, 3);
    // parse error in forced json mode
    let (code, _, _) = run(&["deid", "--format", "json"], "{not json");
    assert_eq!(code, 4);
}
