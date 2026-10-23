# opencare-deid — Rust de-identification step

A standalone, rule-based de-identification step for OpenCare research
extracts. It is the Rust de-identification step named as a target in the
Phase 14 pipeline design (`docs/PHASE10_ROADMAP.md`, Track I; SQA report
`docs/sqa/PHASE14_SQA_REPORT.md`: "no de-id step type exists in the
vocabulary, and none is faked"). This crate **is** the step, built so the
pipeline can invoke it as a subprocess. It is **not yet wired into** the
Python pipeline engine — see "Pending integration" below.

## Method — stated precisely

**What this is:** rule-based, HIPAA Safe Harbor-*style* identifier
removal / generalisation for text and JSON payloads, per 45 CFR
164.514(b)(2), implementing the Safe Harbor categories that are
mechanically implementable in text/JSON, driven entirely by a JSON
config file (config is data; no identifier list is compiled in beyond
the documented defaults, and every default can be overridden).

**What this is not:**

- This is **not** an expert-certification determination under
  45 CFR 164.514(b)(1). No statistical analysis is performed and no
  expert has certified anything.
- Using this tool is **not a compliance guarantee**. Safe Harbor also
  requires, among other things, that the covered entity have no actual
  knowledge that the remaining information could identify an individual
  (164.514(b)(2)(ii)) — a property no regex pipeline can establish.
  Rule-based matching misses identifiers it has no pattern or key for
  (e.g. an unlabelled patient name inside free-text prose). A human
  review / QA process must sit between this step and any release.
- No certification of any kind is claimed or implied.

### Safe Harbor categories: handling here

| # | Category (164.514(b)(2)(i)) | Handling |
|---|---|---|
| A | Names | JSON: values under configured name keys replaced. Text: name labels (`Patient Name:`, `Name:`) consume up to three capitalised tokens after the label. Names are **not** detectable by pattern in general — unlabelled names in prose are a known miss (see Limitations). |
| B | Geographic subdivisions smaller than state | JSON: street address + city keys replaced; ZIP truncated to first 3 digits (`truncate3`, +4 dropped). Text: ZIP truncation; `Address:`-style labels consume the street segment (up to the next comma). A city named in running prose after such a label is **not** reliably removable in text mode — a known miss. State itself is retained by design. |
| C | Dates (except year); elements related to dates incl. birth/death/admission/discharge | `date_mode: "year-only"` (default) reduces every detected date to its year — the Safe Harbor shape. `date_mode: "shift"` is the **limited-dataset style alternative** (164.514(e)), *not* Safe Harbor: all dates of one record shift by the same deterministic amount so intervals survive. |
| D | Phone numbers | Text pattern rule + JSON phone keys. |
| E | Fax numbers | Same pattern family as phone (US number shapes) + JSON fax keys. |
| F | Email addresses | Text pattern rule + JSON email keys. |
| G | Social Security numbers | Text `###-##-####` shape rule + JSON ssn keys. |
| H | Medical record numbers | JSON mrn keys + text label patterns (`MRN:`, `Chart No.:`, …). |
| I | Health plan beneficiary numbers | JSON account keys (`memberid`, `policynumber`, …) + label patterns. |
| J | Account numbers | JSON account keys + label patterns (`Acct #`, `Account No.:`, …). |
| K | Certificate/license numbers | Label patterns / custom regex list — add site-specific labels in config. |
| L | Vehicle identifiers / license plates | Custom regex list (site-specific shapes) — none enabled by default. |
| M | Device identifiers and serial numbers | JSON device keys + label patterns (`Device ID:`, `Serial #:`). |
| N | Web URLs | Text pattern rule + JSON url keys. |
| O | IP addresses | Text IPv4 pattern rule (octets validated ≤ 255) + JSON ip keys. IPv6 is **not** implemented. |
| P | Biometric identifiers | Not applicable to text/JSON payloads — nothing to implement here. Any biometric *data* must never be placed in a payload sent to this step. |
| Q | Full-face photographs and comparable images | Not applicable to text/JSON — same note as P. This step processes no images. |
| R | Any other unique identifying number, characteristic, or code | Configurable `custom_patterns` regex list (name + pattern + replacement), plus label patterns and JSON identifier key lists. |

Ages over 89 and aggregation to 90+ (the date-category rider in (C))
are **not** implemented: this step does not know which numbers are ages.
Sites extracting age fields must handle that in their extract query.

## CLI

```
opencare-deid [--config cfg.json] deid  [--in <file|->] [--out <file|->]
                                        [--record-key <key>]
                                        [--format auto|json|text]
opencare-deid [--config cfg.json] check [--in <file|->] [--out <file|->]
                                        [--record-key <key>]
                                        [--format auto|json|text]
```

- `--config` may appear before or after the subcommand. Without it, the
  built-in defaults apply (all rules on, `date_mode: "year-only"`); see
  `examples/default-config.json` for the defaults written out (with a
  placeholder salt).
- Input defaults to stdin (`--in -`), output to stdout (`--out -`).
- `--format auto` (default): the payload is treated as JSON iff it
  parses as a JSON object/array; otherwise as text. `--format json`
  forces JSON (invalid JSON is exit 4); `--format text` forces text.
- `deid` writes the de-identified payload (JSON mode: pretty-printed,
  structure preserved).
- `check` writes **only** a counts report —
  `{"counts": {"email": 1, "json_key:mrn": 1, ...}, "total": N}` — and
  never writes de-identified output. The pipeline can use `check` as a
  DQ gate (e.g. fail/quarantine when expected-identifier counts are zero
  on a source known to contain them, or eyeball hit rates per batch).
- `--record-key` keys the date shift. In JSON mode, when it is absent,
  the top-level field named by `record_key_field` (default `"id"`) is
  used; when neither exists the key is the empty string (still
  deterministic for a given salt).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. For `check`, hits are reported, **not** an error. |
| 1 | Usage error (bad/missing arguments). |
| 2 | Config error (config unreadable or invalid, incl. a bad custom regex or an unsupported `zip_mode`). |
| 3 | I/O error (input unreadable, output unwritable). |
| 4 | Parse error (input not valid JSON under `--format json`). |

## Config schema

All fields optional; omitted fields take the defaults shown in
`examples/default-config.json`.

```jsonc
{
  "rules": {                    // per-category toggles, all default true
    "email": true, "phone": true, "url": true, "ipv4": true,
    "ssn": true, "dates": true, "zip": true,
    "label_identifiers": true,  // "MRN:"-style labelled tokens in text
    "custom_patterns": true,
    "json_keys": true           // JSON key-based transforms
  },
  "date_mode": "year-only",     // "year-only" (Safe Harbor shape, default)
                                // | "shift" (limited-dataset style)
  "shift_salt": "change-me-per-deployment",
  "shift_max_days": 180,        // shift ∈ [-max, +max] days
  "zip_mode": "truncate3",      // only supported value
  "replacements": {             // replacement strings per category
    "email": "[EMAIL]", "phone": "[PHONE]", "url": "[URL]",
    "ipv4": "[IP]", "ssn": "[SSN]", "name": "[NAME]",
    "address": "[ADDRESS]", "city": "[CITY]", "mrn": "[MRN]",
    "account": "[ACCOUNT]", "device": "[DEVICE]", "identifier": "[ID]"
  },
  "label_patterns": ["MRN:", "Acct #", "..."],   // token after label is replaced;
                                                 // replacement category is inferred
                                                 // from the label (mrn/account/device/ssn)
  "identifier_keys": {          // JSON keys per category, matched
                                // case-insensitively ignoring _ - and spaces
    "name": ["name", "patientname", "family", "given", "..."],
    "address": ["address", "street", "line", "..."],
    "city": ["city", "..."], "zip": ["zip", "zipcode", "postalcode"],
    "ssn": ["ssn", "..."], "mrn": ["mrn", "medicalrecordnumber", "..."],
    "account": ["accountnumber", "memberid", "..."],
    "device": ["deviceid", "serialnumber", "udi", "..."],
    "email": ["email", "..."], "phone": ["phone", "fax", "..."],
    "url": ["url", "..."], "ipv4": ["ipaddress", "..."],
    "date": ["dob", "dateofbirth", "..."]
    // additionally: any key whose normalised form ends in "date" or
    // "datetime", or contains "birth", is treated as a date key
  },
  "custom_patterns": [
    {"name": "study-id", "pattern": "\\bSTUDY-\\d{6}\\b", "replacement": "[STUDY]"}
  ],
  "record_key_field": "id"      // or null to disable JSON key lookup
}
```

### Date shifting

`shift = H(shift_salt, record_key) mod (2·shift_max_days + 1) − shift_max_days`,
where `H` is FNV-1a-64 with a splitmix64 finaliser (implemented locally;
stable across runs/platforms — `std`'s `DefaultHasher` is deliberately
not used). Same key → same shift, so all dates in one record move
together and intervals/durations are preserved. The salt is ordinary
configuration: anyone holding the config can recompute a record's
shift, so shifted output is pseudonymised, not anonymised, data.

### Text rules (each individually toggleable and tested)

Email, phone/fax (US shapes: `(415) 555-0132`, `415-555-0132`,
`415.555.0132`, `+1 415-555-0155`, bare 10-digit), URLs (`http(s)://…`,
`www.…`), IPv4 (validated octets), SSN-shaped `###-##-####`, dates
(ISO `2024-03-05` incl. datetime suffixes, `2024/03/05`, `03/05/2024`,
`03-05-2024`, `March 5, 2024`, `5 March 2024` — validated as real
calendar dates; non-dates like `2023-02-29` are left untouched), ZIP /
ZIP+4 → first 3 digits + `00`, labelled identifiers, custom regexes.
Rule order is fixed (URLs → email → IPv4 → SSN → labels → phone →
dates → ZIP → custom) so composite strings are consumed by the right
rule.

### JSON rules

The walker preserves structure (keys, array order/length, nesting,
non-identifier values and their types). For each object member:

- key matches a redaction category → scalar values become the category
  replacement string; arrays map element-wise (objects inside are
  recursed into); object values are recursed into so e.g. a FHIR
  `HumanName`'s `family`/`given` and an `Address`'s `line`/`city`/
  `postalCode` transform individually;
- key matches ZIP → string truncated via its first 5 digits; a numeric
  ZIP ≥ 10000 is truncated numerically (`94110` → `94100`);
- key matches date → a value that is exactly one date is transformed
  (year-only or shifted); other strings fall through to the text rules;
- any other string value → the full text-rule pipeline;
- numbers/booleans/null under non-identifier keys pass through
  untouched (a lab value under `value`/`valueQuantity.value` survives —
  this is asserted by the FHIR-ish bundle test).

Known structural limitation: a generic key like FHIR
`identifier[].value` cannot be classified by key alone (the same `value`
key carries lab results elsewhere), so an MRN sitting in
`identifier[].value` is only removed if it matches a text pattern or a
label/custom rule. Sites should add a custom pattern for their MRN
shape, or classify identifiers in the extract query.

## Limitations (rule-based, by construction)

- Unlabelled names, street addresses, and other prose identifiers in
  free text are **not** reliably detectable by regex and will be
  missed. This step is one layer, not the whole de-identification
  process.
- ZIP truncation matches any standalone 5-digit token (e.g. a 5-digit
  code) in text; phone matching can match other 3-3-4 digit groupings.
  Precision was favoured where a cheap validation exists (real calendar
  dates, IPv4 octet range), but false positives remain possible.
- IPv6, non-US phone/ZIP formats, and ages > 89 handling are not
  implemented.
- `check` counts for a key-matched subtree count the key hit (and hits
  in nested structured values); counts are a gating signal, not an
  audit-grade enumeration.

## How the Python pipeline would invoke it (pending integration)

The Phase 14 engine (`backend/app/pipelines.py`) runs governed steps
from a fixed vocabulary. The intended wiring — **not yet made** — is:

1. Add `"deidentify"` to the step vocabulary in
   `backend/app/configstore.py`'s pipeline validator and a
   `_step_deidentify` implementation + `_STEP_IMPLS` entry in
   `backend/app/pipelines.py` that serialises the run's dataset to a
   temp file, runs
   `opencare-deid --config <site config> deid --in <tmp> --out <tmp.out>`
   (step params carry the config path and the record-key field), checks
   the exit code (non-zero → step failure → existing retry/dead-letter
   path), and loads the output as the step dataset. Optionally run
   `check` first and surface its counts into the step detail / DQ gate.
2. Seed a pipeline definition using the step (a governed Studio config
   change, draft → approve → enable as usual).
3. SQA: extend `backend/tests/test_phase14_pipelines.py` (or a new
   phase test) with a deidentify-step run over synthetic rows.

## Build & test

Toolchain (this workspace): rustup under
`~/workspace/toolchains/` (`RUSTUP_HOME=~/workspace/toolchains/rustup`,
`CARGO_HOME=~/workspace/toolchains/cargo`).

```sh
export RUSTUP_HOME=$HOME/workspace/toolchains/rustup
export CARGO_HOME=$HOME/workspace/toolchains/cargo
export PATH=$CARGO_HOME/bin:$PATH
# The build VM's /tmp is a small tmpfs — keep artefacts off it:
export TMPDIR=$HOME/workspace/tmp/rust
export CARGO_TARGET_DIR=$HOME/workspace/tmp/rust/target
cargo test --manifest-path services/rust/Cargo.toml
cargo build --release --manifest-path services/rust/Cargo.toml
```

`cargo test` also passes with the default target dir; the env vars
above only keep the (large) `target/` tree out of the source tree and
off the small `/tmp`.
