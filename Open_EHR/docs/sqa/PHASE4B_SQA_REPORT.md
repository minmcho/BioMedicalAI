# Phase 4B SQA report — Specialty boards (ED, radiology, surgery)

Date: 2026-10-15. Gate: `docs/SQA.md`. Data: synthetic only (resources
created through the real API in tests + the live smoke; no seed bundle
changes were needed). Phase 4 split: 4A = billing/claims + pharmacy
(DONE, `docs/sqa/PHASE4A_SQA_REPORT.md`); **4B = ED board / radiology /
surgery board (this report). With 4B gated, Phase 4 is complete.**

## What shipped

- **ED board** (`backend/app/boards.py`, web ED board page):
  - An ED visit is a FHIR Encounter with `class` code EMER whose
    `status` carries the tracking state — `waiting → triage →
    in-treatment → disposition → departed | admitted` — loaded from
    the published Studio workflow config `workflow/ed-flow` (seeded
    v1; the code constant is only a fallback). Config-driven behaviour
    is proven by test: publishing an ed-flow v2 that adds
    `waiting → disposition` flips that transition 409 → 200.
  - `POST /ed/arrivals` records the arrival time (`arrived_at`
    accepted, default now); `GET /ed/board` returns the census grouped
    by state with **`wait_minutes` computed server-side** from the
    recorded arrival (a 95-minute-old arrival reads ≥ 95 in tests, ≥ 90
    in the live smoke), longest-waiting first.
  - Triage (`waiting → triage`) **requires an ESI `acuity`, integer
    1–5** (missing / out-of-range / non-integer → 422), stored as the
    `urn:opencare:extension/ed-acuity` extension and shown on the
    board.
  - `departed`/`admitted` are **governed exits** (bare transition →
    422, the lab pattern): `POST .../admit` creates an inpatient
    Encounter (class IMP, `partOf` → the ED encounter; the link is also
    recorded on the ED encounter), `POST .../depart` closes the period;
    both are 409 from any state the workflow does not allow them from.
- **Radiology** (`backend/app/boards.py`, web Radiology page):
  - `ImagingStudy` added to the generic FHIR surface
    (`app.fhir.SUPPORTED`) with validation: `status`,
    `subject.reference`, and an identifier with system `urn:dicom:uid`
    (else 422).
  - `POST /radiology/studies` creates a study **from a
    ServiceRequest**: the modality must be an active concept in the
    published Studio terminology config `terminology/imaging-modalities`
    (unknown/inactive → 422; unknown order → 404). Study Instance UID
    generated under the DICOM 2.25 UUID root.
  - **DICOMweb** (`GET /dicomweb/studies`, `GET
    /dicomweb/studies/{uid}`): QIDO-RS-shaped study-level **DICOM
    JSON** (PS3.18 hex tags → `{vr, Value}`) derived from the stored
    ImagingStudy — StudyInstanceUID, PatientName (PN), PatientID,
    StudyDate/Time, ModalitiesInStudy, StudyID, series/instance counts
    (0), StudyDescription. UID round-trip asserted in tests and smoke.
  - **Report flow**: `POST /radiology/studies/{id}/report` drafts a
    DiagnosticReport (`preliminary`, `imagingStudy`-linked, one open
    draft per study — a second → 409); `POST
    /radiology/reports/{id}/verify` (`radiology.verify` capability:
    admin/clinician — configurator 403, mirroring `lab.verify`) moves
    it to `final`; a non-imaging report → 422.
  - **Viewer**: the study responses carry a `viewer_url` built
    server-side from the new Studio **integration** config
    (`integration/dicom-viewer`, payload path `viewer.baseUrl`;
    seeded default = the public OHIF demo viewer). A new `integration`
    kind was added to the Studio engine (`CONFIG_KINDS` + payload
    validator + web `ConfigKind`) for external-system connection data.
    The web page renders it as an external link (“Open in viewer”,
    `target=_blank`). **There is no embedded viewer, and studies are
    metadata-only: no pixel data exists anywhere in v1** — stated in
    the API (`has_images: false`, a note on the study body), the web
    UI, the contract and this report.
- **Surgery board** (`backend/app/boards.py`, web Surgery board page):
  - A case is a FHIR Procedure (its `status` carries the case state)
    linked by extensions to a scheduling Appointment carrying the room
    (a Location participant) and the real start/end times. Rooms are
    the published Studio terminology config `terminology/or-rooms`
    (OR-1…OR-4 seeded); the case machine (`scheduled → pre-op → in-op
    → pacu → done`, cancellable from the early states) is the
    published Studio workflow config `workflow/surgery-flow`.
    `done`/`cancelled` settle the linked Appointment
    (fulfilled/cancelled).
  - `GET /surgery/board?date=` returns the day's cases **grouped by
    room** (every configured room listed, even when empty), cases in
    start order.
  - **Overlap rule**: scheduling and rescheduling compare the
    requested interval against the **real stored start/end** of every
    non-cancelled appointment in the same room — an overlap is a
    **409 before any write**. Adjacent intervals are allowed,
    different rooms never conflict, and cancelling a case frees its
    room (all asserted).
- **Web**: ED board page (arrival form, census grouped by state,
  acuity select + Triage, Start treatment / To disposition, Admit /
  Depart), Radiology page (modality select from config, studies table
  with report status + external viewer link, study detail with report
  draft + verify), Surgery board page (date picker, schedule form,
  rooms with per-case state actions + reschedule, 409 conflicts
  surfaced inline). Nav: all three boards for staff roles incl.
  viewer (read-only — write controls hidden in-page and enforced
  server-side); patients get no nav and 403 from the API.

## Gate results (exact)

1. **Unit + API tests**
   - Backend: `python -m pytest -q` → **139 passed** (127 pre-existing
     + 12 new, all in `tests/test_phase4b_boards.py`: config seeding +
     config-driven ED machine, ED full flow, admit linkage, governed
     exits/validation, ED roles + audit, radiology flow + verify
     gating, generic ImagingStudy validation, DICOMweb shape + UID
     round-trip, surgery flow + board grouping, overlap rules, surgery
     validation + roles + audit).
   - Web: `npm test` → **81 passed** (62 pre-existing + 19 new:
     `src/api/boardsClient.test.ts` 6, `pages/EdBoardPage.test.tsx` 4,
     `pages/RadiologyPage.test.tsx` 4, `pages/SurgeryBoardPage.test.tsx`
     5; `auth/session.test.ts` nav expectations updated for the three
     boards — still 6 tests). `npm run build` → **exit 0**.
2. **FHIR validation** — ImagingStudy required fields enforced on the
   generic surface (missing UID identifier or subject → 422, tested);
   module-level negatives: triage without/with invalid acuity → 422,
   unknown ED state → 422, non-EMER encounter on ED endpoints → 422,
   unknown/inactive modality → 422, report without conclusion → 422,
   non-imaging report verify → 422, unknown room → 422, `end <= start`
   → 422, malformed board date → 422.
3. **RBAC negative tests** — patient → 403 on `/ed/board`,
   `/ed/arrivals`, `/dicomweb/studies`, `/radiology/*` reads are
   staff-only, `/surgery/board`, `/surgery/cases`; unauthenticated →
   401 on board reads and writes; viewer → 403 on ED arrival and case
   scheduling (200 on all board reads); configurator → 403 on imaging
   report verify (`radiology.verify` excludes it by default, like
   `lab.verify`).
4. **Audit assertions** — ED writes `ed.arrival`,
   `ed.encounter.transition`, `ed.encounter.admit`,
   `ed.encounter.depart`; surgery writes `surgery.case.schedule`,
   `surgery.case.transition`, `surgery.case.reschedule`; radiology
   writes `radiology.study.create`, `radiology.report.create`,
   `radiology.report.verify` — all through the same hash-chained
   `append_audit`. The ED and surgery suites assert the rows exist and
   `verify_chain` passes; config seeds write the standard
   `studio.config.create/publish` rows.
5. **Completeness / no-noop** — every route's effect is a persisted row
   observed by an independent read: board groups/counts after each
   transition; the inpatient Encounter read back with its `partOf`
   link; DICOMweb JSON re-derived from the stored study (list vs
   single-UID GET are byte-identical in the test); verify state read
   back via the study detail; surgery board grouping read after
   scheduling; the overlap 409 is proven to write nothing (the live
   smoke's board still totals 1 after the refused create).
   Config-driven behaviour is proven, not just claimed (ed-flow v2
   flips a transition; modalities/rooms/viewer all read from published
   config at request time).
6. **Security checklist** — no secrets in code or fixtures; synthetic
   data only (synthetic patient names/ids in tests + smoke); patients
   are excluded from every board surface (403) and board resources
   still pass through the generic layer's patient read scoping;
   wait times, UIDs and viewer URLs are computed server-side — the
   caller supplies no price/total/identity the server trusts; the
   viewer base URL comes only from Studio config (admin/configurator
   governed), never from request input; **no new dependencies**
   (backend and web use the existing stacks only).
7. **Data integrity** — all three boards are state machines over the
   published workflow configs with 409 on out-of-order transitions;
   terminal ED states are reachable only through the endpoints that do
   the associated work (link creation / period close); the room
   overlap check runs before any write on create and reschedule
   (excluding the case's own appointment on a move); case ↔
   appointment link integrity is validated on every case operation
   (a non-case Procedure → 422).
8. **Docs** — `contracts/fhir-resources.md` gained the full Phase 4B
   section (ED / radiology / DICOMweb / surgery contracts, the
   overlap rule, and the metadata-only + external-viewer statements)
   and its Planned list no longer names 4B; `README.md` gained a
   Phase 4B section, the web-pages list entry and layout updates;
   `docs/PHASES.md` records Phase 4 and 4B DONE — **Phase 4 closes**;
   this report.

## Live verification beyond unit tests (actually run)

- **Live HTTP smoke** (real uvicorn on a throwaway SQLite DB, fresh
  process, real HTTP via httpx): **28/28 checks PASS** — health
  reports phase `4b`; ED arrival recorded 90 minutes in the past reads
  back on the board with `wait_minutes >= 90`; triage without acuity →
  422, with ESI 2 → 200; in-treatment → disposition → admit creates
  the IMP inpatient Encounter with the `partOf` link and empties the
  board; patient → 403 on the board; study created from a real
  ServiceRequest carries a config-built `viewer_url` containing the
  UID and `has_images: false`; DICOMweb list + UID round-trip agree;
  report draft → configurator verify 403 → clinician verify `final`;
  surgery case schedules, an overlapping same-room case → 409 with
  the board still totalling 1, case → pre-op; patient → 403 on the
  surgery board. (Smoke client used `trust_env=False` — this VM's
  ambient `no_proxy` contains `[::1]`, which breaks httpx's default
  env handling; the same documented quirk the seed loader handles.)

## NOT RUN / not claimed (honest list)

- **Real DICOM pixel data / ingest NOT RUN, not built** — no DICOM
  files are parsed, no series/instances exist, and there are no
  WADO-RS retrieval endpoints. Studies are synthetic **metadata
  only**; DICOMweb serves study-level metadata derived from
  ImagingStudy resources, nothing more.
- **Embedded viewer NOT RUN / not built** — by design. The only viewer
  surface is an external link to an OHIF-style viewer whose base URL
  is Studio integration config; the seeded default points at the
  public OHIF demo, which was **not** opened or verified from this VM
  (no browser check) — the link format
  (`{baseUrl}/viewer?StudyInstanceUIDs={uid}`) is asserted in tests,
  the target site's behaviour is not claimed.
- **PostgreSQL runtime NOT RUN** — no Docker/Postgres on this VM
  (same standing caveat as every prior phase); the suite runs on
  SQLite through the portable SQLAlchemy layer.
- **Browser click-through E2E NOT RUN** — the web pages are covered by
  Vitest component tests against stubbed clients plus the live HTTP
  smoke of every API the pages call; no headless-browser run was
  performed.
- **Live Keycloak/OIDC run NOT RUN** (standing) — auth verified in dev
  token mode and via the OIDC unit tests from earlier phases.
- **Full US Core profile validation NOT RUN** (standing) — validation
  remains resourceType + required-fields per `docs/SQA.md`.
