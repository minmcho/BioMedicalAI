//! CLI for the opencare-deid pipeline step.
//!
//! Usage:
//!   opencare-deid [--config cfg.json] deid  [--in f|-] [--out f|-]
//!                                             [--record-key K]
//!                                             [--format auto|json|text]
//!   opencare-deid [--config cfg.json] check [--in f|-]
//!                                             [--record-key K]
//!                                             [--format auto|json|text]
//!
//! `--config` may also appear after the subcommand. With no `--config`,
//! the built-in defaults apply (all rules on, date_mode year-only).
//!
//! Exit codes:
//!   0  success (for `check`: report written; hits are NOT an error)
//!   1  usage error (bad/missing arguments)
//!   2  config error (unreadable or invalid config file)
//!   3  I/O error (input unreadable / output unwritable)
//!   4  parse error (input is not valid JSON in --format json mode)

use std::io::{Read, Write};
use std::process::ExitCode;

use opencare_deid::{check_json, check_text, deid_json_value, deid_text, total_hits, Config};

const USAGE: &str = "usage: opencare-deid [--config <file>] <deid|check> [--in <file|->] [--out <file|->] [--record-key <key>] [--format auto|json|text]";

#[derive(Debug, PartialEq)]
enum Mode {
    Deid,
    Check,
}

struct Args {
    config_path: Option<String>,
    mode: Mode,
    input: String,
    output: String,
    record_key: Option<String>,
    format: String,
}

fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut config_path: Option<String> = None;
    let mut mode: Option<Mode> = None;
    let mut input = "-".to_string();
    let mut output = "-".to_string();
    let mut record_key: Option<String> = None;
    let mut format = "auto".to_string();
    let mut i = 0;
    while i < argv.len() {
        let a = &argv[i];
        let take = |i: &mut usize, flag: &str| -> Result<String, String> {
            *i += 1;
            argv.get(*i)
                .cloned()
                .ok_or_else(|| format!("{flag} requires a value\n{USAGE}"))
        };
        match a.as_str() {
            "--config" => config_path = Some(take(&mut i, "--config")?),
            "--in" => input = take(&mut i, "--in")?,
            "--out" => output = take(&mut i, "--out")?,
            "--record-key" => record_key = Some(take(&mut i, "--record-key")?),
            "--format" => {
                format = take(&mut i, "--format")?;
                if !["auto", "json", "text"].contains(&format.as_str()) {
                    return Err(format!("--format must be auto|json|text\n{USAGE}"));
                }
            }
            "deid" => mode = Some(Mode::Deid),
            "check" => mode = Some(Mode::Check),
            "-h" | "--help" => return Err(USAGE.to_string()),
            other => return Err(format!("unknown argument {other:?}\n{USAGE}")),
        }
        i += 1;
    }
    let mode = mode.ok_or_else(|| format!("missing subcommand (deid|check)\n{USAGE}"))?;
    Ok(Args {
        config_path,
        mode,
        input,
        output,
        record_key,
        format,
    })
}

fn read_input(path: &str) -> Result<String, (u8, String)> {
    if path == "-" {
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .map_err(|e| (3u8, format!("cannot read stdin: {e}")))?;
        Ok(buf)
    } else {
        std::fs::read_to_string(path).map_err(|e| (3u8, format!("cannot read {path}: {e}")))
    }
}

fn write_output(path: &str, content: &str) -> Result<(), (u8, String)> {
    if path == "-" {
        let mut so = std::io::stdout().lock();
        so.write_all(content.as_bytes())
            .and_then(|_| {
                if content.ends_with('\n') {
                    Ok(())
                } else {
                    so.write_all(b"\n")
                }
            })
            .map_err(|e| (3u8, format!("cannot write stdout: {e}")))
    } else {
        std::fs::write(path, content).map_err(|e| (3u8, format!("cannot write {path}: {e}")))
    }
}

fn run(argv: &[String]) -> Result<(), (u8, String)> {
    let args = parse_args(argv).map_err(|e| (1u8, e))?;
    let cfg = match &args.config_path {
        Some(path) => {
            let raw = std::fs::read_to_string(path)
                .map_err(|e| (2u8, format!("cannot read config {path}: {e}")))?;
            Config::from_json_str(&raw).map_err(|e| (2u8, e))?
        }
        None => Config::default(),
    };
    let input = read_input(&args.input)?;

    // Format detection: explicit flag wins; `auto` treats the payload as
    // JSON iff it parses as a JSON object/array.
    let parsed: Option<serde_json::Value> = match args.format.as_str() {
        "json" => Some(
            serde_json::from_str(&input)
                .map_err(|e| (4u8, format!("input is not valid JSON: {e}")))?,
        ),
        "text" => None,
        _ => {
            let t = input.trim_start();
            if t.starts_with('{') || t.starts_with('[') {
                serde_json::from_str(&input).ok()
            } else {
                None
            }
        }
    };

    match args.mode {
        Mode::Deid => {
            let out = match &parsed {
                Some(doc) => {
                    let v = deid_json_value(&cfg, doc, args.record_key.as_deref())
                        .map_err(|e| (2u8, e))?;
                    serde_json::to_string_pretty(&v)
                        .map_err(|e| (3u8, format!("cannot serialise output: {e}")))?
                }
                None => deid_text(&cfg, args.record_key.as_deref().unwrap_or(""), &input)
                    .map_err(|e| (2u8, e))?,
            };
            write_output(&args.output, &out)
        }
        Mode::Check => {
            let counts = match &parsed {
                Some(doc) => check_json(&cfg, doc).map_err(|e| (2u8, e))?,
                None => check_text(&cfg, &input).map_err(|e| (2u8, e))?,
            };
            let report = serde_json::json!({
                "counts": counts,
                "total": total_hits(&counts),
            });
            write_output(
                &args.output,
                &serde_json::to_string_pretty(&report).unwrap(),
            )
        }
    }
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.iter().any(|a| a == "-h" || a == "--help") && argv.len() == 1 {
        println!("{USAGE}");
        return ExitCode::from(0);
    }
    match run(&argv) {
        Ok(()) => ExitCode::from(0),
        Err((code, msg)) => {
            // Phase 17 (BD-53): stderr is a logging boundary. The
            // message passes through this crate's own
            // de-identification engine (default config) before it
            // is emitted, so an error string can never carry an
            // identifier shape to the log.
            let safe = deid_text(&Config::default(), "", &msg)
                .unwrap_or(msg);
            eprintln!("opencare-deid: {safe}");
            ExitCode::from(code)
        }
    }
}
