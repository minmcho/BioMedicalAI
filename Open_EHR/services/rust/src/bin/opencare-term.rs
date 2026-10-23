//! CLI for the opencare-term terminology-normalisation step
//! (Phase 16, BD-50) — the Rust tier counterpart of the Python
//! pipeline step `terminology-normalise`.
//!
//! Usage:
//!   opencare-term normalise --format json [--in <file|->] [--out <file|->]
//!
//! Reads stdin (or `--in`) JSON:
//!   {"codeMap": {"mappings": [{fromCode, fromSystem?, toCode,
//!                               toSystem?, ...}, ...]},
//!    "rows": [{...}, ...]}
//! and writes stdout (or `--out`) JSON:
//!   {"rows": [...], "unmapped": ["XYZ", ...],
//!    "unmapped_codes": ["XYZ", ...], "mapped": N, "unmapped_rows": M}
//! where `unmapped`/`unmapped_codes` are the sorted, de-duplicated
//! unmapped codes (the Python step's `unmapped_codes` report).
//! Semantics are identical to `pipelines._step_normalise` — see
//! `opencare_deid::term`.
//!
//! Exit codes (mirroring opencare-deid's discipline):
//!   0  success
//!   1  usage error (bad/missing arguments)
//!   3  I/O error (input unreadable / output unwritable)
//!   4  parse error (input is not valid JSON, or codeMap/rows are
//!      missing or malformed)

use std::io::{Read, Write};
use std::process::ExitCode;

use opencare_deid::term::normalise;
use opencare_deid::{deid_text, Config};

const USAGE: &str =
    "usage: opencare-term normalise --format json [--in <file|->] [--out <file|->]";

struct Args {
    input: String,
    output: String,
}

fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut mode: Option<()> = None;
    let mut input = "-".to_string();
    let mut output = "-".to_string();
    let mut format: Option<String> = None;
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
            "normalise" => mode = Some(()),
            "--format" => format = Some(take(&mut i, "--format")?),
            "--in" => input = take(&mut i, "--in")?,
            "--out" => output = take(&mut i, "--out")?,
            "-h" | "--help" => return Err(USAGE.to_string()),
            other => return Err(format!("unknown argument {other:?}\n{USAGE}")),
        }
        i += 1;
    }
    if mode.is_none() {
        return Err(format!("missing subcommand (normalise)\n{USAGE}"));
    }
    match format.as_deref() {
        Some("json") => {}
        Some(other) => {
            return Err(format!(
                "--format must be json, got {other:?}\n{USAGE}"
            ))
        }
        None => return Err(format!("--format json is required\n{USAGE}")),
    }
    Ok(Args { input, output })
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
    let input = read_input(&args.input)?;
    let doc: serde_json::Value = serde_json::from_str(&input)
        .map_err(|e| (4u8, format!("input is not valid JSON: {e}")))?;
    let obj = doc
        .as_object()
        .ok_or_else(|| (4u8, "input must be a JSON object {codeMap, rows}".to_string()))?;
    let code_map = obj
        .get("codeMap")
        .ok_or_else(|| (4u8, "input is missing 'codeMap'".to_string()))?;
    let rows_val = obj
        .get("rows")
        .ok_or_else(|| (4u8, "input is missing 'rows'".to_string()))?;
    let rows = rows_val
        .as_array()
        .ok_or_else(|| (4u8, "'rows' must be a JSON array".to_string()))?;

    let outcome = normalise(code_map, rows).map_err(|e| (4u8, e))?;
    let report = serde_json::json!({
        "rows": outcome.rows,
        "unmapped": outcome.unmapped.codes,
        "unmapped_codes": outcome.unmapped.codes,
        "mapped": outcome.mapped,
        "unmapped_rows": outcome.unmapped.rows,
    });
    let text = serde_json::to_string_pretty(&report)
        .map_err(|e| (3u8, format!("cannot serialise output: {e}")))?;
    write_output(&args.output, &text)
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
            // Phase 17 (BD-53): stderr is a logging boundary — the
            // message is de-identified by the crate's own engine
            // before emission (see src/main.rs).
            let safe = deid_text(&Config::default(), "", &msg)
                .unwrap_or(msg);
            eprintln!("opencare-term: {safe}");
            ExitCode::from(code)
        }
    }
}
