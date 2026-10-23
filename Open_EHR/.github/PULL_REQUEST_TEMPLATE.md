<!--
Thank you for contributing to OpenCare EHR. Please complete every
section — the SQA gate is part of this project's clinical-safety
culture. See CONTRIBUTING.md for the ground rules.
-->

## What this change does

## Configuration coverage

<!-- What is System Studio configuration vs code, and why. Per the
standing rule: configure, don't fork. -->

## Compliance coverage

<!-- Required when the change touches a regulated workflow (HIPAA,
CLIA, EMTALA, DEA/USP/DSCSA, 42 CFR Part 2, ...). Otherwise: N/A. -->

## Test evidence

<!-- Exact commands and counts, e.g.:
  backend: pytest tests/ (two halves) — NNN + NNN passed
  web: npm test — NNN passed; npm run build — clean
  adk: pytest — N passed; go: go test ./... ; rust: cargo test -->

## NOT RUN

<!-- Anything not verified, with the reason. NOT RUN is an honest
state, never a failure to confess. -->

## Safety checklist

- [ ] Synthetic data only; no PHI, no credentials, no real identifiers
- [ ] New write paths are hash-chain audited (field names, not values)
- [ ] New route groups added to both web proxy configs
- [ ] No dead controls; dropdowns populated or disabled with a reason
- [ ] I have added myself to CONTRIBUTORS.md (first PR only)
