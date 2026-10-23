"""Phase 17, BD-53 (CMP-32) — PHI redaction at the logging boundary.

The proof: a synthetic-day workload (real API requests carrying
synthetic PHI-shaped values + the log lines services emit about
them) is captured through the redaction boundary and scanned —
ZERO unredacted PHI patterns may survive, while redaction markers
prove the lines were emitted and redacted, not absent. Cross-service
legs scan the Go and Rust services' emitted output and the ADK
mirror's spec parity. Synthetic data only.
"""
import io
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import logredact
from tests.conftest import ADMIN, CLINICIAN

ROOT = Path(__file__).resolve().parents[2]  # Open_EHR/
SSN = "123-45-6789"
EMAIL = "jane.synthetic@example.org"
PHONE = "(415) 555-0132"
CARD = "4111 1111 1111 1111"

DIRTY = (f"Patient Name: Jane Synthetic | DOB: 1980-04-12 | "
         f"MRN: SYN-88213 | ssn {SSN} | email {EMAIL} | "
         f"phone {PHONE} | card {CARD} | "
         f"Authorization: Bearer abcdefgh-token-1234 | "
         f"Address: 44 Synthetic Way, Springfield")


# ---------------------------------------------------------------------------
# Unit level: the rule set
# ---------------------------------------------------------------------------

def test_redact_text_catches_every_phi_shape():
    out = logredact.redact_text(DIRTY)
    scan = logredact.scan_text(out)
    assert scan["clean"] is True and scan["total"] == 0
    for secret in (SSN, EMAIL, PHONE, CARD, "Jane Synthetic",
                   "1980-04-12", "SYN-88213", "abcdefgh-token-1234",
                   "44 Synthetic Way"):
        assert secret not in out
    assert scan["redaction_markers"] >= 6


def test_redact_leaves_ordinary_clinical_text_alone():
    benign = ("BP 120/80, HR 72, dose 5 mg twice daily, glucose "
              "5.5 mmol/L, admitted 2026-10-19, ward 4 West bed 12")
    assert logredact.redact_text(benign) == benign
    assert logredact.scan_text(benign)["clean"] is True


def test_scan_counts_survivors_before_redaction():
    scan = logredact.scan_text(DIRTY)
    assert scan["clean"] is False and scan["total"] >= 6
    assert scan["patterns"]["ssn"] >= 1
    assert scan["patterns"]["email"] == 1


def test_filter_redacts_formatted_args():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logredact.RedactingFilter())
    logger = logging.getLogger("test.bd53.filter")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.error("lookup failed for patient ssn %s email %s", SSN, EMAIL)
    emitted = stream.getvalue()
    assert SSN not in emitted and EMAIL not in emitted
    assert "[REDACTED-SSN]" in emitted
    assert logredact.scan_text(emitted)["clean"] is True


# ---------------------------------------------------------------------------
# The synthetic-day log scan (backend)
# ---------------------------------------------------------------------------

def test_synthetic_day_workload_log_scan_is_clean(client):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        # (Re)install the boundary so the fresh capture handler is
        # covered, exactly like POST /security/log-redaction/refresh.
        logredact.install_log_redaction()
        assert any(isinstance(f, logredact.RedactingFilter)
                   for f in handler.filters)

        # --- A synthetic day of real requests carrying PHI shapes.
        r = client.post("/fhir/Patient", headers=CLINICIAN, json={
            "resourceType": "Patient", "id": "pat-logscan",
            "name": [{"family": "Synthetic", "given": ["Jane"]}],
            "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                            "value": SSN}],
            "telecom": [{"system": "email", "value": EMAIL},
                        {"system": "phone", "value": PHONE}],
            "birthDate": "1980-04-12"})
        assert r.status_code == 201, r.text
        r = client.post("/messages", headers=CLINICIAN, json={
            "patient": "pat-logscan",
            "text": f"Synthetic follow-up: reach me at {EMAIL} or "
                    f"{PHONE}; my ssn is {SSN} for verification",
            "topic": "check-in"})
        assert r.status_code == 201, r.text
        r = client.post("/privacy/break-glass", headers=CLINICIAN,
                        json={"patient_ref": "Patient/pat-logscan",
                              "reason": "synthetic drill"})
        assert r.status_code == 201, r.text

        # --- The lines services emit ABOUT that work (the leak
        # surface this BD exists to close): naive log statements
        # embedding the same values, through the guarded logger.
        workload = logging.getLogger("opencare.workload")
        workload.error("message send failed for Patient Name: %s, "
                       "contact %s / %s, ssn %s",
                       "Jane Synthetic", EMAIL, PHONE, SSN)
        workload.info("statement run: MRN: %s DOB: %s card %s",
                      "SYN-88213", "1980-04-12", CARD)
        try:
            raise RuntimeError(f"upstream echo: {DIRTY}")
        except RuntimeError:
            workload.exception("agent run failed")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    captured = stream.getvalue()
    assert captured.strip(), "the workload must actually emit logs"
    scan = logredact.scan_text(captured)
    assert scan["total"] == 0 and scan["clean"] is True, scan
    assert scan["redaction_markers"] >= 5
    for secret in (SSN, EMAIL, PHONE, CARD, "Jane Synthetic"):
        assert secret not in captured


# ---------------------------------------------------------------------------
# Governed config: status, scan endpoint, custom rules
# ---------------------------------------------------------------------------

def test_status_scan_and_custom_rule_via_studio_config(client):
    status = client.get("/security/log-redaction/status", headers=ADMIN)
    assert status.status_code == 200, status.text
    status = status.json()
    assert status["spec_version"] == "log-redaction-v1"
    assert "integration/log-redaction" in status["config"]
    assert "ssn" in status["builtin_rules"]

    dirty = client.post("/security/log-redaction/scan", headers=ADMIN,
                        json={"text": DIRTY})
    assert dirty.json()["clean"] is False

    payload = logredact.default_logredaction_payload()
    payload["customRules"] = [{"name": "synth-token",
                               "pattern": r"SYNTH-\d{4}",
                               "replacement": "[REDACTED-SYNTH]"}]
    draft = client.post("/studio/config/integration/log-redaction",
                        headers=ADMIN, json=payload)
    assert draft.status_code == 201, draft.text
    pub = client.post(
        "/studio/config/integration/log-redaction/versions/"
        f"{draft.json()['version']}/publish", headers=ADMIN)
    assert pub.status_code == 200, pub.text
    refreshed = client.post("/security/log-redaction/refresh",
                            headers=ADMIN)
    assert refreshed.status_code == 200
    assert "synth-token" in refreshed.json()["rules"]
    out = client.post("/security/log-redaction/redact", headers=ADMIN,
                      json={"text": "token SYNTH-1234 on file"})
    assert out.json()["redacted"] == "token [REDACTED-SYNTH] on file"
    # The module-level active set is restored for other tests.
    logredact._active = list(logredact._BUILTIN_COMPILED)


# ---------------------------------------------------------------------------
# Cross-service proof: ADK parity + Go/Rust emitted-output scans
# ---------------------------------------------------------------------------

def test_adk_mirror_matches_backend_spec():
    adk_dir = ROOT / "backend" / "adk"
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from app.logredact import BUILTIN_RULES, SPEC_VERSION, redact_text;"
        "print(SPEC_VERSION);"
        "print(','.join(n for n, _p, _r in BUILTIN_RULES));"
        "print(redact_text('ssn 123-45-6789 email a@b.org'))"
        % adk_dir)
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == logredact.SPEC_VERSION
    assert lines[1].split(",") == [n for n, _p, _r
                                   in logredact.BUILTIN_RULES]
    assert "123-45-6789" not in lines[2] and "[REDACTED-SSN]" in lines[2]


def test_go_services_log_scan():
    go = shutil.which("go") or str(ROOT.parent / "workspace" / "tools"
                                   / "go" / "bin" / "go")
    if not Path(go).exists():
        go = str(Path.home() / "workspace" / "tools" / "go" / "bin" / "go")
    if not Path(go).exists():
        pytest.skip("go toolchain not available")
    # Package scope: every Go package EXCEPT internal/termgrpc,
    # which is sibling Phase 17 work-in-progress that does not
    # compile in this tree (server.go imports google.golang.org/grpc
    # and a generated gen/opencare/v1 package, neither present) —
    # recorded as NOT RUN in the WS-SEC results, not silently passed.
    listing = subprocess.run([go, "list", "./..."],
                             cwd=ROOT / "services" / "go",
                             capture_output=True, text=True, timeout=300)
    assert listing.returncode == 0, listing.stderr
    packages = [p for p in listing.stdout.splitlines()
                if p.strip() and "termgrpc" not in p]
    assert packages, "no Go packages found"
    proc = subprocess.run([go, "test"] + packages,
                          cwd=ROOT / "services" / "go",
                          capture_output=True, text=True, timeout=900)
    emitted = proc.stdout + proc.stderr
    assert proc.returncode == 0, emitted[-3000:]
    scan = logredact.scan_text(emitted)
    assert scan["clean"] is True, scan
    # Source-level proof: no Go log/print call site formats a raw
    # payload variable — errors cross via redact.Redact.
    offenders = []
    for go_file in (ROOT / "services" / "go").rglob("*.go"):
        if go_file.name.endswith("_test.go"):
            continue
        for line in go_file.read_text().splitlines():
            if re.search(r"log\.Print|fmt\.Print", line) and (
                    "payload" in line or "raw" in line.lower()
                    and "Redact" not in line):
                offenders.append(f"{go_file.name}: {line.strip()}")
    assert offenders == [], offenders


def test_rust_services_log_scan():
    cargo_home = Path.home() / "workspace" / "tools" / "cargo"
    cargo = cargo_home / "bin" / "cargo"
    if not cargo.exists():
        pytest.skip("cargo toolchain not available")
    env = dict(os.environ)
    env["RUSTUP_HOME"] = str(Path.home() / "workspace" / "tools"
                              / "rustup")
    env["CARGO_HOME"] = str(cargo_home)
    proc = subprocess.run([str(cargo), "test", "--offline"],
                          cwd=ROOT / "services" / "rust", env=env,
                          capture_output=True, text=True, timeout=1200)
    emitted = proc.stdout + proc.stderr
    assert proc.returncode == 0, emitted[-3000:]
    scan = logredact.scan_text(emitted)
    assert scan["clean"] is True, scan
    # The built CLI's stderr boundary: feed it PHI on stdin in check
    # mode and confirm stderr (the log channel) stays clean while the
    # de-id report goes to stdout where it belongs.
    import glob
    binaries = glob.glob(str(ROOT / "services" / "rust" / "target"
                             / "debug" / "opencare-deid"))
    assert binaries, "opencare-deid binary not built by cargo test"
    run = subprocess.run(
        [binaries[0], "check", "--in", "-", "--format", "text"],
        input=f"note for Jane Synthetic ssn {SSN} email {EMAIL}",
        capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert logredact.scan_text(run.stderr)["clean"] is True
    assert SSN not in run.stderr
