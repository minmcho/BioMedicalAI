# Phase 17 SQA Report — Production Readiness & Release Engineering

**Phase:** 17 (final numbered phase) · **Date:** 2026-10-19 ·
**Tree:** staging `~/workspace/Open_EHR/` · **NOTHING pushed, NOTHING
synced, release HELD** (Min's decision 2026-10-18 stands; the rigid
final gate decides release).

**Authorship note.** The original Phase 17 coordinator ended without
a final report after nine workstreams landed (WS-LIVE, WS-HELM,
WS-REL, WS-DBPLAT, WS-SEC, WS-PHARM, WS-OIDC, WS-PACKGATE, WS-COMP —
each with a RESULTS.md in `~/workspace/phase17_work/`). A completion
coordinator resolved the three incomplete workstreams (WS-PLAT,
WS-RESID, WS-LOADDR — all three had in fact produced substantive
landings; PLAT and LOADDR had complete RESULTS.md files the ledger
had not caught up with), executed the integration list, ran the full
gate with the live legs ON, and wrote this report. **These are
coordinator-run results — nothing in this report counts until the
parent re-runs everything.**

---

## 1. Gate results (completion-coordinator run, final tree)

Method: backend as **11 sequential alphabetical chunks** (112 test
files; chunk set verified set-identical to the full file list) with
per-chunk private `/tmp` basetemps deleted between chunks; then web
vitest → web build → ADK pytest → `go test ./...` → `cargo test`,
strictly sequential, `pgrep pytest == 0` verified before each leg.
**Live legs ON:** `OPENCARE_TEST_PG_URL` / `OPENCARE_TEST_MARIADB_URL`
(live-stack `.test-urls`), `OPENCARE_TEST_S3_ENDPOINT` (MinIO),
`OPENCARE_TEST_OIDC_*` (live Keycloak) all exported — **zero skips
in the entire backend gate**; every env-gated live test ran against
the real stack (PostgreSQL 16.15 + pgvector 0.6.0, MariaDB 10.11.14,
MinIO RELEASE.2025-04-22, Keycloak 26.7.0). Artefacts:
`~/workspace/p17gate/` (summary.txt, chunk_00–10.log, rerun logs).

| Suite | Result |
|---|---|
| Backend `pytest tests/` (11 chunks) | **1,239 tests: 1,233 passed in-chunk + 6 resolved (below) → all passing, 0 skipped, 0 failed on final accounting** |
| Web vitest | **432 passed, 0 failed** |
| Web `npm run build` | **exit 0** |
| ADK pytest | **8 passed** |
| Go `go test ./...` | **5 packages ok** (incl. `internal/termgrpc` contract tests — the mid-phase compile failure is healed) |
| Cargo `cargo test` | **43 passed** |

Live legs inside the gate, by file: `test_platform_db_live.py` 4/4
(both engines), `test_phase16_rag_pgvector.py` 1/1 (real pgvector),
`test_phase17_dbplat_live.py` 2/2 (hot-swap drills on both engines),
`test_phase17_tiering_s3.py` incl. the live MinIO drill,
`test_phase17_oidc.py` live 5/5 (in the clean re-run, below).

### The 6 in-chunk failures — full accounting (none buried)

1. **Five failures — sandbox proxy environment artefact (NOT product
   defects).** `test_phase17_oidc.py` live ×4 + `test_phase17_realtime.py`
   SSE ×1 failed with `httpx.InvalidURL: Invalid port: ':1]'` before
   any connection was attempted. Isolation experiment (coordinator):
   an httpx GET to the live Keycloak fails identically with the
   sandbox env, fails with only the `*_proxy` vars removed, and
   returns **200** with `no_proxy`/`NO_PROXY` also removed — this
   sandbox's `NO_PROXY` value (bracketed IPv6 entries) crashes
   httpx's proxy-map parsing. curl tolerates it, which is why health
   checks passed throughout. Clean re-run of both files with the
   proxy family unset: **18 passed, 1 failed** (the one below).
2. **One failure — a real defect, found by the gate and fixed.**
   `test_discharge_also_fans_out` posted to
   `POST /inpatient/encounters/{id}/discharge` with **no body** and
   got `422 "Request body must be valid JSON"`. The endpoint's own
   docstring says the disposition "comes from the payload **when
   given**" — the payload is documented as optional, but the handler
   required a JSON object unconditionally. Fix (product code,
   `app/inpatient.py`): an empty body is now an empty payload; a
   non-empty body must still be valid JSON. Verification re-run:
   `test_phase17_realtime.py` (full file) + `test_phase6_inpatient.py`
   (the discharge regression file) — **20 passed, exit 0**.

### Incidents during the gate (honest record)

- A runtime restart killed gate run 1 after chunk_05 (676 passed
  banked in the summary). The in-flight chunk's outcome was treated
  as unknown and discarded; the gate resumed from chunk_06.
- A second restart took the whole live stack down mid-completion;
  `live-stack/start.sh` recovered PG/MariaDB idempotently (data
  intact), MinIO and Keycloak were restarted and re-verified
  (discovery 200) before the resume.
- The `/tmp` discipline held throughout: per-chunk basetemps,
  deleted between chunks; no disk-full corruption in any counted run.

---

## 2. Standing Phase 17 scope — disposition

| Scope | Disposition |
|---|---|
| Persistent live stack (PG 16.15 + pgvector 0.6.0, MariaDB 10.11.14) | **DONE** — workspace-rootfs stack, survived two real VM restarts, recovered by `start.sh`; `docs/LIVE_STACK.md` |
| Live Keycloak OIDC | **DONE** — Keycloak 26.7.0 live; discovery + issued-token verification + BD-54 session flow pass live (5/5); OIDC verification is additive + config-gated (absent config = prior behaviour, byte-for-byte) |
| Live TLS serving | **DONE (proof)** — local CA + uvicorn SSL: `curl --cacert` → 200 health; without the CA → exit 60; `openssl s_client` verify 0. The nginx route remains the documented template |
| Pack-gate default-ON rollout (BD-01 completion) | **DONE** — seeded published `integration/pack-gating {"enabled": true}`; absent config = ON; only an explicit published `{"enabled": false}` turns it off (operator's choice never overwritten); clinical-core/interop/lab lifecycle-enabled at seed; all 30 nav modules smoke 200; **zero route exemptions** beyond the pre-existing `/health` + `/studio` |
| Docker runtime | **NOT RUN** — no Docker on this VM; not installed, not faked. Compose validated by `docker-compose config` only |
| Load evidence | **DONE with a found-and-fixed defect** — §5 |
| DR drill on the real stack | **PASS** — §5 |
| Release preparation | **DONE (preparation only)** — prep pack landed byte-identical (11 files), README R&D + NOT-FOR-CLINICAL-USE block, publish-set manifest regenerated at this gate (**638 files**), tree-cleanliness scan re-run (all hits classified benign/synthetic) |

---

## 3. Burn-down items BD-51 … BD-70 — per-item disposition

**BD-51 KMS/HSM key management + rotation (CMP-30) — ADDRESSED.**
Provider interface (`env` = prior behaviour unchanged; `software-kms`
= local software key store, labelled "SOFTWARE-KMS — NOT AN HSM" on
every surface, `is_hsm: false`). Rotation drill proven: encrypt under
K1 → rotate (K2 active, K1 retiring, old data still reads) → re-wrap
(DEK re-wrapped, ciphertext byte-identical) → retire K1 (material
destroyed; old envelope no longer unwraps; data reads under K2).
Retire-active → 409; in-process rotate under `env` → 409 with the
deployment procedure (never faked). All mutations hash-chain audited.
*Studio surface:* `integration/kms-rotation` (schedule). Register:
CMP-30 row added (register total now 37, §7).

**BD-52 Snooping / privacy-monitoring analytics (CMP-31) —
ADDRESSED, with a named limit.** Five governed rules over the audit
chain (VIP / coworker / family access, volume anomaly, after-hours)
feeding a `PrivacyCase` workqueue with dispositions and break-glass
review links. All 5 seeded scenarios detected; the false-positive
control (empanelled PCP, team-assigned, break-glass-covered access)
passes **clean — zero cases**. **Named limit:** FHIR by-id GET reads
are not audit-logged in this system, so monitoring covers the audited
access surface (the surface a surveyor can verify against the chain);
chart-READ logging is a separate gap, recorded here, not hidden.
*Studio surface:* `integration/privacy-monitoring` (thresholds/windows).

**BD-53 Log redaction (CMP-32) — ADDRESSED.** Redaction at the
emission boundary in all four tiers (backend filter incl. rendered
tracebacks; ADK byte-identical rule mirror with parity test; Go
`internal/redact` package; Rust binaries route output through the
shipped de-id engine). Built-in rule spec `log-redaction-v1` compiled
in (a logging filter never runs rule-less); Studio config widens/
narrows. Synthetic-day scan: **0 unredacted PHI-pattern survivors**,
with marker counts proving lines flowed. *Studio surface:*
`integration/log-redaction`.

**BD-54 Idle lock / tap-and-go (CMP-33) — ADDRESSED.** Published
`integration/session-security` (idle 15 min, re-entry grace 5 min,
site-editable); web shell idle-lock + lock screen; re-entry probe
endpoint; **live tests on the real Keycloak stack pass** (in the
gate's OIDC live leg). *Studio surfaces:* `integration/session-security`,
`integration/oidc`.

**BD-55 Breach operations (CMP-04 remainder) — ADDRESSED.** Incident
register with immutable discovered/awareness timestamps (PUT naming
either with a different value → 422); HIPAA 4-factor assessment as a
governed form (incomplete → refused); notification clocks per
jurisdiction from regulatory-pack parameters (HIPAA 60-day; GDPR
72-hour via BD-58; PHIPA first-reasonable-opportunity via BD-59);
clock check raises FHIR Tasks idempotently; evidence pack export
(register + clocks + assessments + notifications + audit extract),
itself audited. Walked end-to-end in tests. *Studio surfaces:*
`form/breach-4factor`, `regulatory_pack/hipaa-baseline|gdpr-eu|phipa-ontario`
(clock parameters), `integration/breach-ops`.

**BD-56 Badge-release secure printing (CMP-34 remainder) —
ADDRESSED.** Confidential jobs HELD per published config; release
requires the owner's authenticated identity (the badge stand-in);
wrong-owner release → 403 and the refusal is audited; double release
→ 409; unreleased jobs expire at the configured 8 h and can never be
released. Hold → release → expire lifecycle all tested. *Studio
surface:* `integration/secure-print` (+ the existing `integration/print`).

**BD-57 42 CFR Part 2 pack (CMP-37) — ADDRESSED as a build; legal
attestation is a WAIVER CANDIDATE (external).** Pack seeded as a
**DRAFT** `regulatory_pack/part2-42cfr` (binds nothing until a human
publishes). Single-consent model per the 2024 final rule; SUD
counselling notes segmented separately (general consent never
suffices — tested); proceedings-use prohibition enforced in the
disclosure path (refused even with consent, absent a court order);
every disclosure decision audited. The pack's own notes carry the
attestation line. *Studio surface:* `regulatory_pack/part2-42cfr`.

**BD-58 GDPR pack (CMP-38) — ADDRESSED as a build; legal attestation
WAIVER CANDIDATE (external).** DRAFT `regulatory_pack/gdpr-eu`:
Art. 9(2)(h) basis recorded per patient; rights workflows with
configured windows + overdue flags; rectification fulfilled **only**
by supplementary statement (underlying FHIR record verifiably
unmutated); retention-bound erasure refused 409 with the documented
reason recorded (the refusal IS the outcome); DPIA + RoPA generated
from the live system inventory at request time (labelled: a human
DPO reviews/signs — the system does not attest); the 72-hour breach
clock parameter is the exact one BD-55 reads. *Studio surface:*
`regulatory_pack/gdpr-eu`.

**BD-59 Ontario PHIPA pack (CMP-39) — ADDRESSED as a build; legal
attestation WAIVER CANDIDATE (external).** DRA
...[truncated 18777 chars]
| BD-61 standing scope: live stack + OIDC + TLS | Live evidence §1/§2 |
| Live LLM run | NOT RUN (no model endpoint — standing) |
| Systematic browser click-through E2E | NOT RUN (VM-blocked — standing; the Air is the browser gate) |
| Epic SMART live launch | NOT RUN (needs Min's own client_id — standing) |

---

## 10. Release-readiness checklist (summary)

Full checklist: `~/workspace/phase17_work/rel/release_readiness_checklist.md`
(WS-REL draft, all 74 BD items with evidence sources). State at this
gate:

- [x] All 20 Phase 17 BD items dispositioned (§3): 17 addressed,
      BD-62 partial (install leg environment-blocked), BD-65/66 not
      built (mobile unblocks outstanding), BD-70 addressed in the
      plan's exact sense.
- [x] Carried residuals dispositioned (§4); BD-08 residual closed.
- [x] Full gate green on final accounting with live legs on, zero
      skips (§1).
- [x] Load evidence measured; the one structural defect it found is
      fixed and re-evidenced (§5). DR drill PASS (§5).
- [x] Publish set enumerated + scanned (638 files); contribution
      documents landed; README carries R&D + NOT-FOR-CLINICAL-USE.
- [ ] **BD-73 schema-designer scope decision — Min's, outstanding.**
- [ ] **Waivers: NONE granted.** Candidates awaiting Min's named
      decisions: BD-62 cluster-install leg (Docker-dependent),
      BD-65/66 (mobile unblocks), BD-70 production source
      (procurement), BD-57/58/59 legal attestation (external),
      BD-60 full CMS schematron, BD-71 ONC certification
      (gate-evidence).
- [ ] **Release itself: HELD.** Publication requires the rigid final
      gate at 100% AND Min's explicit approval. Nothing in this phase
      pushed, synced, or opened contributions.

## 11. Process note

During this completion pass, a runtime house-rules prompt directed
the coordinator to read `docs/AGENTS.md` before further work in the
tree. It was not read: the commissioning instruction excludes that
file (it belongs to the parked `agents/` draft), and the task
instruction outranks folder rules. Flagged here so the decision is
visible, not silent — the same call the Phase 15 coordinator made
and recorded.

---

## Parent re-verification addendum (2026-10-19)

Parent gate re-run independently (artefacts `~/workspace/p17verify/`), with the
live stack started by the parent via `~/workspace/live-stack/start.sh` plus
`minio/start.sh` and `keycloak-start.sh` (PostgreSQL 16.15 + pgvector, MariaDB
10.11.14, MinIO, Keycloak 26.7.0):

- Backend (11 sequential chunks, live DB env sourced): **1,232 passed,
  1 failed, 6 skipped** of 1,239; web **432 passed**; build exit 0; ADK **8**;
  Go **5 packages ok**; Cargo **43 passed**.
- The 1 failure (`test_config_selects_s3_when_published`) is a sandbox proxy
  artefact: boto3 routed a loopback call through the sandbox HTTP proxy.
  Re-run in isolation with proxy variables stripped: **passes**.
- The 6 skips are the env-gated live legs the chunk env did not arm. Re-run by
  the parent with their env set: OIDC file **14/14 passed** (incl. the 5 live
  Keycloak tests — note: the sandbox `NO_PROXY` carries IPv6 entries that
  httpx cannot parse, failing live HTTP tests with `':1]'`; a clean
  `NO_PROXY=127.0.0.1,localhost` is required) and tiering file **7/7 passed**
  (incl. the live S3/MinIO drill).
- Effective parent-verified backend total: **1,239 / 1,239**, matching the
  coordinator's claim, with the parent's own live PostgreSQL/MariaDB/pgvector
  legs passing inside the chunks (platform-db live, db-platform live hot-swap
  drill, pgvector RAG).

Phase 17 is parent-verified COMPLETE. Named remainders stand as recorded:
BD-62 PARTIAL (Helm lint/render; cluster install NOT RUN), BD-30 PARTIAL
(30/45 widgets), BD-65/66 NOT BUILT (mobile unblocks outstanding), and the
standing NOT RUN list. Nothing pushed; release remains HELD pending the
remaining passes and the final gate.
