"""Studio configuration store — shared core (no FastAPI imports, so both the
Studio config router and the security layer can use it without import cycles).

Everything a deploying organisation changes — forms, workflows, terminology,
navigation, branding, permissions — is configuration *data* with a
draft -> publish lifecycle, versioning and audit (docs/STUDIO_CUSTOMIZATION.md).
This module owns: the kind registry, per-kind payload validation, published
("effective") lookups, the capability defaults that published permission
overrides merge over, and a small structural diff for version comparison.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ConfigStatus, Role, StaffMember, StudioConfig, UserAccount

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

CONFIG_KINDS = ("form", "workflow", "terminology", "navigation", "branding", "permissions",
                "cohort", "integration", "orderset", "measure",
                # Phase 10: agent definitions (Track A), automation rules
                # (Track B), dashboards (Track B/E), regulatory packs
                # (Track J). All follow the same draft -> publish
                # governance as every other kind.
                "agent", "rule", "dashboard", "regulatory_pack",
                # Phase 13: report definitions (Track F) and import
                # mapping profiles (Track F) — same governance.
                "report", "mapping",
                # Phase 14: pipeline definitions (Track I) — governed
                # data-processing pipelines; same draft -> publish
                # governance as every other kind.
                "pipeline",
                # Platform pass: protocol / SOP definitions (governed
                # clinical protocols executed by app.protocols) — same
                # draft -> publish governance as every other kind.
                "protocol",
                # Platform pass: report-engine definitions and the
                # indicator library (app.report_engine / app.indicators).
                "report-definition", "indicator",
                # Platform pass: Studio DB platform (app.dbplatform) —
                # connection profiles, deployment schema extensions,
                # transformer/export profiles. Credential-free by
                # validator law (vault key NAMES only).
                "dbplatform", "dbplatform-schema", "dbplatform-transform",
                # Platform pass WS10 (Studio full parity): the AI
                # provider registry (app.ai_providers — key REFERENCES
                # only, never secrets) and the endpoint/tool
                # DECLARATION registries (app.studio_ops — born
                # disabled, no code execution).
                "ai-provider", "endpoint", "tool",
                # Phase 16 (Intelligence & Research): supervised agent
                # workflows (app.agent_workflows), governed knowledge
                # documents for RAG (app.rag), MCP server registrations
                # (app.mcp_a2a — credential-free by validator law) and
                # governed analytics views (app.analytics — dataset
                # allowlist, never SQL). Same draft -> publish
                # governance as every other kind.
                "agentflow", "knowledge", "mcp_server", "analytics_view",
                # Inventory & Materials pass: the governed inventory
                # taxonomy (categories/subcategories, adjustment reason
                # codes, expiry warning window) consumed by
                # app.inventory — same draft -> publish governance as
                # every other kind.
                "inventory")

# The one key under which the role/capability matrix lives (kind=permissions).
PERMISSIONS_KEY = "role-matrix"

# Capability -> roles allowed by default. A published permissions config may
# override individual (capability, role) cells; everything else falls back to
# these defaults. Enforced server-side in the API dependencies
# (app.security.require_capability), never by hiding UI alone.
CAPABILITY_DEFAULTS: dict[str, tuple[str, ...]] = {
    "fhir.write": ("admin", "configurator", "clinician"),
    "fhir.inbox": ("admin", "configurator", "clinician"),
    "studio.manage": ("admin", "configurator"),
    # Read gates (realignment): these used to be hardcoded role tuples in
    # the modules (STAFF_READ / POPHEALTH_ROLES). The defaults below are
    # copied tuple-for-tuple from those constants, so behaviour is
    # unchanged until a site publishes a permissions override.
    "studio.read": ("admin", "configurator", "clinician", "viewer"),
    "lab.read": ("admin", "configurator", "clinician", "viewer"),
    "pharmacy.read": ("admin", "configurator", "clinician", "viewer"),
    "boards.read": ("admin", "configurator", "clinician", "viewer"),
    "pophealth.view": ("admin", "clinician"),
    "pophealth.run": ("admin",),
    # Phase 2 (Lab LIS). Ordering and bench work follow the fhir.write
    # pattern; verification/release is a clinical sign-off, so the
    # configurator (a systems role, not a clinician) is excluded by default.
    "lab.order": ("admin", "configurator", "clinician"),
    "lab.work": ("admin", "configurator", "clinician"),
    "lab.verify": ("admin", "clinician"),
    # Phase 4A (billing + pharmacy). Billing: clinicians may VIEW claims
    # (their patients' encounters are billed from the chart they work in);
    # managing the claim lifecycle is a revenue-cycle role. A patient
    # caller's access to their OWN claims is handled in app.billing (own
    # claims only, read-only) — the patient role is deliberately not in
    # these defaults. Pharmacy: dispensing is pharmacy/clinical work;
    # barcode med-admin is a bedside clinical act (admin/clinician).
    "billing.view": ("admin", "configurator", "clinician"),
    "billing.manage": ("admin", "configurator"),
    "pharmacy.dispense": ("admin", "configurator", "clinician"),
    "pharmacy.administer": ("admin", "clinician"),
    # Phase 11 (pharmacy department depth). Order verification is a
    # clinical sign-off (the lab.verify pattern — configurator
    # excluded); inventory/procurement and sterile compounding are
    # pharmacy operations work; controlled-substance counts and
    # witnessed waste are two-person clinical acts (admin/clinician).
    "pharmacy.verify": ("admin", "clinician"),
    "pharmacy.inventory": ("admin", "configurator", "clinician"),
    "pharmacy.compound": ("admin", "configurator", "clinician"),
    "pharmacy.cs": ("admin", "clinician"),
    # Phase 4B (specialty boards). Board work follows the fhir.write
    # pattern; imaging-report verification is a clinical sign-off, so —
    # like lab.verify — the configurator is excluded by default.
    "ed.work": ("admin", "configurator", "clinician"),
    "radiology.work": ("admin", "configurator", "clinician"),
    "radiology.verify": ("admin", "clinician"),
    "surgery.work": ("admin", "configurator", "clinician"),
    # Phase 5 (orders + documentation). Placing/signing orders and
    # authoring documentation are clinical write work (fhir.write
    # pattern); reads follow the staff-read pattern incl. viewer.
    # Per-transition restrictions (e.g. who may SIGN) live in the
    # published order-flow workflow config, not here.
    "orders.read": ("admin", "configurator", "clinician", "viewer"),
    "orders.place": ("admin", "configurator", "clinician"),
    "notes.read": ("admin", "configurator", "clinician", "viewer"),
    "notes.write": ("admin", "configurator", "clinician"),
    # Phase 6 (inpatient operations). Reads follow the boards pattern
    # (staff incl. viewer; the census is staff-only operational data —
    # the patient role is deliberately absent); bed/ADT work is
    # clinical-operations work on the fhir.write pattern. Housekeeping
    # flips (cleaning -> available) ride inpatient.work in v1; a site
    # can split them out by publishing a permissions override plus a
    # capability of its own.
    "inpatient.read": ("admin", "configurator", "clinician", "viewer"),
    "inpatient.work": ("admin", "configurator", "clinician"),
    # Phase 7 (access & revenue cycle). Referral intake/triage,
    # authorisations, eligibility checks and the waitlist are
    # registration/access work: reads follow the staff-read pattern,
    # writes follow the fhir.write pattern. HIM coding is revenue-cycle
    # work: coders read the queue (billing.view pattern — no viewer)
    # and assign codes under a billing.manage-like gate; the queue
    # surfaces clinical documentation references, so the patient role
    # is deliberately absent from both.
    "access.read": ("admin", "configurator", "clinician", "viewer"),
    "access.work": ("admin", "configurator", "clinician"),
    "coding.read": ("admin", "configurator", "clinician"),
    "coding.work": ("admin", "configurator"),
    # Phase 8 (interoperability). The interface log is staff operational
    # data (message metadata + patient refs, never message bodies in
    # list views beyond the raw text an interface analyst needs) —
    # reads follow the staff-read pattern; dispatch / replay are
    # interface-engine operations (admin + configurator, like
    # studio.manage). Bulk export is a SYSTEM-LEVEL, record-level
    # extract: admin/configurator only, every job and download audited.
    "interop.read": ("admin", "configurator", "clinician", "viewer"),
    "interop.manage": ("admin", "configurator"),
    "interop.export": ("admin", "configurator"),
    # Phase 9 (engagement + quality/research). Secure messaging is
    # clinical communication: staff write/read under one capability;
    # the patient side is scoped to the caller's own record in
    # app.engagement (the portal pattern), not by this capability.
    "messages.use": ("admin", "configurator", "clinician"),
    # Patient self-scheduling: the portal appointment-request endpoint.
    # A site turns it off (or on for more roles) by publishing a
    # permissions override — the solo-practice profile pack does
    # exactly that. Staff confirm requests through the ordinary
    # Appointment ADT machine either way.
    "portal.self-schedule": ("admin", "clinician", "patient"),
    # Research workbench aggregate export (app.pophealth): a
    # de-identified, aggregate-only extract — gated like
    # interop.export (admin/configurator), every export audited.
    "pophealth.export": ("admin", "configurator"),
    # Phase 10 (agents + privacy). Agents (Track A): viewing the agent
    # roster and running an enabled agent are clinical-assistant acts;
    # the agent DEFINITIONS are Studio config (studio.manage), and a
    # run can only ever produce a draft for human review — never a
    # signed clinical act — so clinician is in both defaults.
    "agents.view": ("admin", "configurator", "clinician"),
    "agents.run": ("admin", "configurator", "clinician"),
    # Break-the-glass (Track K): emergency chart access is a clinical
    # act (admin/clinician); the mandatory post-hoc review of every
    # opening is a privacy/governance act (admin/configurator). The
    # two capabilities are deliberately held by different role sets:
    # the person who opens is never the only person who can review.
    "privacy.breakglass": ("admin", "clinician"),
    "privacy.review": ("admin", "configurator"),
    # Phase 12 (care-continuum operations, Track D). The continuum
    # surfaces are staff operations data (queues, boards, timers):
    # reads follow the staff-read pattern incl. viewer; working a
    # queue (triage timers, assignments, bed requests, transfer
    # decisions, EVS, transport) is clinical-operations work on the
    # fhir.write pattern. The patient role is deliberately absent —
    # a patient's own aftercare surfaces live in the portal.
    "continuum.read": ("admin", "configurator", "clinician", "viewer"),
    "continuum.work": ("admin", "configurator", "clinician"),
    # Safety event reporting (Armstrong pattern): ANY staff role may
    # file a report — a reporting system that gates filing defeats
    # itself — while triage / RCA work is the safety team's.
    "safety.report": ("admin", "configurator", "clinician", "viewer"),
    "safety.review": ("admin", "configurator", "clinician"),
    # Phase 13 (people & data platform, Tracks E + F). The workforce
    # directory / credentials / rosters / on-call are staff operational
    # data: reads follow the staff-read pattern incl. viewer; managing
    # people records is workforce administration (admin/configurator —
    # the same split as studio.manage). Running a catalogue report is
    # a staff read act (the report definitions are governed config and
    # every run is audited); dataset EXPORT and data IMPORT are the
    # system-level data movements — admin/configurator only, every
    # call audited with row counts (the interop.export pattern).
    "workforce.read": ("admin", "configurator", "clinician", "viewer"),
    "workforce.work": ("admin", "configurator"),
    # Front door & master data pass. Account administration is an
    # admin-only act (creating/disabling sign-in identities is the
    # HIPAA unique-user-identification control surface). Registration
    # work — registering patients and checking duplicates — is
    # front-desk/access work: the viewer role is this system's
    # non-clinical staff role, so it is included alongside the
    # clinical write roles; the raw FHIR write surface stays
    # fhir.write-gated as before.
    "accounts.manage": ("admin",),
    "registration.work": ("admin", "configurator", "clinician", "viewer"),
    "reports.run": ("admin", "configurator", "clinician", "viewer"),
    "data.export": ("admin", "configurator"),
    "data.import": ("admin", "configurator"),
    # Phase 14 (devices, IoT, lake & pipelines — Tracks H + I). The
    # device registry, feed status, lake browser and pipeline runs are
    # staff operational data: reads follow the staff-read pattern incl.
    # viewer. Device management (register/calibrate/status), ingestion
    # simulation and quarantine decisions are operations work on the
    # fhir.write pattern; running pipelines and deciding dedup
    # candidates are data-platform operations (admin/configurator,
    # the studio.manage pattern) — patient data moves only through
    # governed, audited actions.
    "devices.read": ("admin", "configurator", "clinician", "viewer"),
    "devices.manage": ("admin", "configurator", "clinician"),
    # Inventory & Materials pass: stock visibility is staff
    # operational data (staff-read incl. viewer, the devices.read
    # pattern); item-master / PAR / cycle-count management and stock
    # movements (receiving, issue, transfer, adjustment, case issue)
    # are materials-operations work on the fhir.write pattern.
    "inventory.read": ("admin", "configurator", "clinician", "viewer"),
    "inventory.manage": ("admin", "configurator", "clinician"),
    "inventory.move": ("admin", "configurator", "clinician"),
    "inventory.verify": ("admin", "clinician"),
    "pipelines.run": ("admin", "configurator"),
    # Phase 15 (departments, clinical depth & operations). Support
    # services / operations (transport, supply chain, diet office,
    # interpreters, patient experience, GME, CUSP, HICS, hospital-at-
    # home, command-centre desks, access line): reads follow the
    # staff-read pattern incl. viewer; work is clinical-operations
    # work on the fhir.write pattern. Care coordination (eConsults,
    # episodes, coordinating physician, ICU, therapy scheduling):
    # same pattern. Utilisation review is case-management work
    # (coding.work pattern — no viewer). Immunisation capture is
    # clinical work; reads are staff incl. viewer. Governed list
    # (dropdown) draft management rides studio.manage for the auth
    # roles PLUS a department-manager job-role path enforced inside
    # app.listmgmt (the capability alone is the role gate; the job
    # role check is additional, never a substitute published in the
    # matrix). RFID: registry reads are staff incl. viewer; tag/reader
    # management is data-platform operations work. Barcode lookup is
    # a staff read; label registration rides the owning modules' work
    # capabilities, with barcode.manage for the registry itself.
    "support.read": ("admin", "configurator", "clinician", "viewer"),
    "support.work": ("admin", "configurator", "clinician"),
    "coord.read": ("admin", "configurator", "clinician", "viewer"),
    "coord.work": ("admin", "configurator", "clinician"),
    "ur.read": ("admin", "configurator", "clinician"),
    "ur.work": ("admin", "configurator", "clinician"),
    "imm.read": ("admin", "configurator", "clinician", "viewer"),
    "imm.work": ("admin", "configurator", "clinician"),
    "lists.manage": ("admin", "configurator"),
    "rfid.read": ("admin", "configurator", "clinician", "viewer"),
    "rfid.manage": ("admin", "configurator", "clinician"),
    "barcode.read": ("admin", "configurator", "clinician", "viewer"),
    "barcode.manage": ("admin", "configurator", "clinician"),
    # Phase 15 (infection prevention + payer care-management).
    # Infection prevention is clinical surveillance work (staff reads
    # incl. viewer; case work on the fhir.write pattern). Care
    # management rides the coord.* pair above (programmes, panels,
    # caseloads are care-coordination work).
    "ip.read": ("admin", "configurator", "clinician", "viewer"),
    "ip.work": ("admin", "configurator", "clinician"),
    # Phase 16 (Intelligence & Research). Federation reads are staff
    # reads (record location + consented summaries); federation work
    # (peer query, reconciliation) is clinical-operations work on the
    # fhir.write pattern. Directories follow the same split (read-only
    # federation of the staff directory; sync is an admin/configurator
    # act). Agent validation is a governance + clinical-safety act.
    # Innovation pilots are governed programme work. RAG search is
    # deliberately clinician-tier (viewer excluded — row-level chart
    # surface; sites may widen via permissions override). MCP tool use
    # includes the patient role ONLY through the module's published
    # patient-scope guardrails. Analytics views are row-level and
    # exclude viewer by default (the rag.search pattern). Research and
    # e-signature follow the study-governance pattern; e-signing is a
    # clinical sign-off. EMPI stewardship is a data-governance act.
    # Document intake is HIM work. Maintenance reads are staff reads;
    # work orders are HTM operations work.
    "federation.read": ("admin", "configurator", "clinician", "viewer"),
    "federation.work": ("admin", "configurator", "clinician"),
    "directories.read": ("admin", "configurator", "clinician", "viewer"),
    "directories.sync": ("admin", "configurator"),
    "agents.validate": ("admin", "configurator", "clinician"),
    "innovation.manage": ("admin", "configurator", "clinician"),
    "rag.search": ("admin", "configurator", "clinician"),
    "mcp.use": ("admin", "configurator", "clinician", "viewer", "patient"),
    "a2a.use": ("admin", "configurator", "clinician"),
    "genomics.read": ("admin", "configurator", "clinician", "viewer"),
    "genomics.work": ("admin", "configurator", "clinician"),
    "research.read": ("admin", "configurator", "clinician", "viewer"),
    "research.work": ("admin", "configurator", "clinician"),
    "esign.sign": ("admin", "configurator", "clinician"),
    "empi.steward": ("admin", "configurator", "clinician"),
    "intake.work": ("admin", "configurator", "clinician"),
    "analytics.use": ("admin", "configurator", "clinician"),
    "maintenance.read": ("admin", "configurator", "clinician", "viewer"),
    "maintenance.work": ("admin", "configurator", "clinician"),
    # Workforce, Teams & Panels pass. Team reads (rosters, panels,
    # coverage, privileges) are staff operational data — the
    # staff-read pattern incl. viewer; team work (membership,
    # assignment decisions, empanelment, coverage, privilege grants)
    # is clinical-operations work on the fhir.write pattern.
    # Onboarding (HR record -> credentialing/privileging -> linked
    # account) is workforce administration — admin/configurator, the
    # workforce.work split.
    "teams.read": ("admin", "configurator", "clinician", "viewer"),
    "teams.work": ("admin", "configurator", "clinician"),
    "workforce.onboard": ("admin", "configurator"),
    # Phase 17 (WS-SEC: BD-51/52/53). Key management (rotation,
    # re-wrap, retire) is a security-administration act; privacy
    # monitoring (snooping analytics + case disposition) sits with
    # the privacy-officer role set that already holds privacy.review;
    # the log-redaction surface (status / scan / refresh) is the
    # same governance tier. None of the three is a clinical act, so
    # clinician/viewer/patient are deliberately absent.
    "security.kms": ("admin", "configurator"),
    "privacy.monitor": ("admin", "configurator"),
    "security.logs": ("admin", "configurator"),
}

ROLE_NAMES = tuple(r.value for r in Role)

# FHIR Questionnaire item types accepted by the form designer.
FORM_ITEM_TYPES = ("group", "display", "boolean", "decimal", "integer", "string",
                   "text", "date", "dateTime", "time", "choice", "open-choice",
                   "quantity")

# The Appointment ADT workflow, seeded as published workflow config v1
# (key "appointment-adt") so the state machine is configuration data from the
# start. fhir.APPOINTMENT_TRANSITIONS is the hardcoded fallback used only if
# the published config is disabled/removed; behaviour is identical either way.
ADT_WORKFLOW_KEY = "appointment-adt"
DEFAULT_ADT_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"booked", "cancelled"},
    "pending": {"booked", "cancelled"},
    "booked": {"arrived", "cancelled"},
    "arrived": {"fulfilled", "cancelled"},
    "checked-in": {"fulfilled", "cancelled"},
    "fulfilled": set(),
    "cancelled": set(),
    "noshow": set(),
}


def default_adt_workflow_payload() -> dict:
    states = sorted(DEFAULT_ADT_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_ADT_TRANSITIONS)
        for dst in sorted(DEFAULT_ADT_TRANSITIONS[src])
    ]
    return {"states": states, "transitions": transitions}


# ---------------------------------------------------------------------------
# Shell defaults: navigation + branding (seeded published v1 at startup,
# mirroring the web shell's built-in defaults EXACTLY — same modules,
# labels, order and per-module roles as web/src/auth/session.ts — so the
# shell can consume the published config with zero visible change, and a
# site re-skins/re-menus the deployment by publishing over them.)
# ---------------------------------------------------------------------------

NAVIGATION_KEY = "main"
BRANDING_KEY = "default"
# Home & Module UX pass: job profiles ride the EXISTING navigation
# kind under this key (no new config kind) — see JOB PROFILES below.
JOB_PROFILES_KEY = "job-profiles"
#: Inventory & Materials pass: the governed inventory taxonomy lives
#: under its own kind (kind "inventory", key "taxonomy") so the item
#: master validates against published configuration, never code.
INVENTORY_TAXONOMY_KEY = "taxonomy"

_STAFF = ["admin", "configurator", "clinician", "viewer"]
_WRITE_STAFF = ["admin", "configurator", "clinician"]

# Home & Module UX pass — NAV REGROUP (fixed contract, PROGRESS
# ledger): the shell's group order. 10 groups of 2–6 modules each
# (spec N-2). Mirrored by web/src/auth/session.ts NAV_GROUP_ORDER.
NAV_GROUP_ORDER: tuple[str, ...] = (
    "Patient Care",
    "Access & Revenue",
    "Clinical",
    "Diagnostics & Pharmacy",
    "Operations",
    "Safety & Support",
    "People & Teams",
    "Insights",
    "Data Platform",
    "System",
)

# (module, label, roles, group) in shell order — mirrors NAV + canAccess in
# web/src/auth/session.ts. Depth pass (Workstream 1): each module also
# carries a navigation `group` so the shell can render grouped headings;
# groups are configuration data — a site regroups by publishing a new
# navigation version, never code. Home & Module UX pass: the tuple is
# in NAV_GROUP_ORDER (group-contiguous) order per the fixed regroup
# contract — group labels and the group/array order changed; every
# module's roles and label are exactly as before the regroup.
_DEFAULT_NAV_MODULES: tuple[tuple[str, str, list[str], str], ...] = (
    # Patient Care
    ("patients", "Patients", _STAFF, "Patient Care"),
    ("inbox", "Results inbox", _WRITE_STAFF, "Patient Care"),
    ("portal", "My results", ["patient"], "Patient Care"),
    # Access & Revenue
    ("scheduling", "Scheduling", _WRITE_STAFF, "Access & Revenue"),
    ("billing", "Billing", ["admin", "configurator", "clinician", "patient"],
     "Access & Revenue"),
    # Clinical
    ("ed", "ED board", _STAFF, "Clinical"),
    ("radiology", "Radiology", _STAFF, "Clinical"),
    ("surgery", "Surgery board", _STAFF, "Clinical"),
    # Diagnostics & Pharmacy
    ("lab", "Laboratory", _STAFF, "Diagnostics & Pharmacy"),
    ("pharmacy", "Pharmacy", _STAFF, "Diagnostics & Pharmacy"),
    # Operations
    ("beds", "Bed board", _STAFF, "Operations"),
    # Phase 12 (Track D): care-continuum operations surfaces. Reads
    # ride continuum.read (staff incl. viewer). The safety module is
    # the exception (UX audit S-10): its page is the triage queue,
    # gated on safety.review (admin / configurator / clinician), so
    # the seed names exactly those roles — the earlier _STAFF seed
    # promised the viewer a module its own page gate refuses, and
    # the two artefacts silently disagreed. Filing a report rides
    # safety.report (staff-wide, viewer included) from the report
    # surfaces; the queue itself is the safety team's. The
    # seed-vs-capability consistency test in
    # tests/test_nav_seed_consistency.py pins every module's seed
    # roles to its page gate so this class of drift cannot recur
    # silently.
    ("command", "Command Center", _STAFF, "Operations"),
    ("er-ops", "ER operations", _STAFF, "Operations"),
    ("unit-board", "Unit board", _STAFF, "Operations"),
    ("clinic", "Clinic day", _STAFF, "Operations"),
    ("aftercare", "Aftercare", _STAFF, "Operations"),
    # Safety & Support. The safety module is the exception (UX audit
    # S-10, see the Phase 12 note above): its seed roles are exactly
    # the safety.review holders (_WRITE_STAFF).
    ("safety", "Safety", _WRITE_STAFF, "Safety & Support"),
    # Phase 15: support services + support operations (Hospital at
    # Home, transport, command desks, access line, supply chain, diet
    # office, interpreters, patient experience, CUSP, HICS, GME).
    # Reads ride support.read (staff incl. viewer).
    ("support", "Support services", _STAFF, "Safety & Support"),
    # Inventory & Materials pass: the dedicated materials module
    # (item master, stock by placement location, append-only movement
    # ledger, PAR/expiry worklists, case draw). It sits with Support
    # services — the Phase 15 home of supply chain — which also keeps
    # every group inside the regroup contract's 2-6 module band.
    # Reads ride inventory.read (staff incl. viewer), so the seed
    # roles mirror _STAFF exactly (the test_nav_seed_consistency law).
    ("inventory", "Inventory & Materials", _STAFF, "Safety & Support"),
    # People & Teams. Phase 13 (Track E): workforce (staff directory,
    # credentials, rosters, on-call). Reads ride workforce.read.
    ("workforce", "Workforce", _STAFF, "People & Teams"),
    # Workforce, Teams & Panels pass: care teams, team panels,
    # empanelment and coverage. Reads ride teams.read (staff incl.
    # viewer), so the seed roles mirror _STAFF exactly (the
    # test_nav_seed_consistency law).
    ("teams", "Teams", _STAFF, "People & Teams"),
    # Insights. Phase 13 (Track F): the report catalogue / documents /
    # export centre. Reads ride reports.run. Phase 16: analytics
    # (governed views + vocabulary) — analytics.use excludes viewer.
    ("pophealth", "Population health", ["admin", "clinician"], "Insights"),
    ("reports", "Reports", _STAFF, "Insights"),
    ("analytics", "Analytics", _WRITE_STAFF, "Insights"),
    # Data Platform. Phase 14 (Tracks H + I): the device registry /
    # feed status and the data platform (lake, pipelines, quarantine,
    # dedup). Reads ride devices.read (staff incl. viewer).
    ("devices", "Devices", _STAFF, "Data Platform"),
    ("data-platform", "Data platform", _STAFF, "Data Platform"),
    # Phase 16 (Intelligence & Research): federation (BD-36/37 peer
    # exchange + reconciliation), research (studies, coverage,
    # billing grids, genomics, e-signature) and the document-intake
    # worklist. Seed roles mirror each page's capability gate exactly
    # (the test_nav_seed_consistency law): federation.read /
    # research.read include viewer; intake.work excludes it.
    ("federation", "Federation", _STAFF, "Data Platform"),
    ("research", "Research", _STAFF, "Data Platform"),
    ("documents", "Documents", _WRITE_STAFF, "Data Platform"),
    # System. The import centre's nav entry is limited to the roles
    # holding data.import by default (Phase 13, Track F).
    ("studio", "System Studio", ["admin", "configurator"], "System"),
    ("imports", "Import centre", ["admin", "configurator"], "System"),
)

NAV_MODULE_KEYS: frozenset[str] = frozenset(
    module for module, _label, _roles, _group in _DEFAULT_NAV_MODULES)

# Home & Module UX pass: every nav item gains an OPTIONAL one-line
# `purpose` (shown on the Home card menu) and an `icon` name. Seeded
# for all 30 modules; the strings mirror web/src/auth/session.ts NAV
# purpose/icon fallbacks EXACTLY.
_NAV_PURPOSE_ICON: dict[str, tuple[str, str]] = {
    "patients": ("Find, register and open patient records", "users"),
    "inbox": ("Review results and messages awaiting action", "inbox"),
    "portal": ("Your results, messages and appointments", "heart"),
    "scheduling": ("Book, move and manage appointments", "calendar"),
    "billing": ("Claims, payments and patient statements", "receipt"),
    "ed": ("Track emergency department patients live", "siren"),
    "radiology": ("Imaging orders, studies and reports", "scan"),
    "surgery": ("Operating lists and perioperative flow", "scalpel"),
    "lab": ("Laboratory orders, results and benches", "flask"),
    "pharmacy": ("Verification, dispensing and stock", "pill"),
    "beds": ("Bed state, admissions and transfers", "bed"),
    "command": ("House-wide flow and capacity oversight", "command"),
    "er-ops": ("Emergency operations and boarding", "activity"),
    "unit-board": ("Unit census, rounds and discharge readiness", "board"),
    "clinic": ("Today's clinics, arrivals and rooming", "stethoscope"),
    "aftercare": ("Follow-up, referrals and care transitions", "route"),
    "safety": ("Safety events, triage and follow-up", "shield"),
    "support": ("Support services and hospital operations", "lifebuoy"),
    "workforce": ("Staff directory, rosters and credentials", "id-badge"),
    "teams": ("Care teams, panels and coverage", "people"),
    "pophealth": ("Registries and population cohorts", "chart"),
    "reports": ("Run reports and export documents", "file"),
    "analytics": ("Governed measures, views and indicators", "graph"),
    "devices": ("Device registry, feeds and maintenance", "device"),
    "data-platform": ("Data lake, pipelines and data quality", "database"),
    "federation": ("Peer exchange and record reconciliation", "network"),
    "research": ("Studies, cohorts and research operations", "microscope"),
    "documents": ("Document intake and processing worklist", "folder"),
    "studio": ("Configure modules, navigation and branding", "sliders"),
    "imports": ("Import data files and review mappings", "upload"),
    "inventory": ("Stock, supplies, implants and materials across locations", "boxes"),
}

# Deep-link sections seeded for modules whose pages have named
# sub-views. A section names a view INSIDE its module — it is never a
# module/page/form entry itself (the modules-only rule still applies).
_DEFAULT_NAV_SECTIONS: dict[str, list[dict[str, str]]] = {
    "lab": [
        {"label": "Orders & Results", "section": "orders"},
        {"label": "Microbiology", "section": "microbiology"},
        {"label": "Virology", "section": "virology"},
        {"label": "Blood Bank", "section": "blood-bank"},
        {"label": "Pathology", "section": "pathology"},
        {"label": "QC & EQA", "section": "qc"},
        # Phase 15 (Min): the Test Manager + Panel Manager live INSIDE
        # the Laboratory module as management tabs alongside QC & EQA,
        # composing onto the governed drafts (publish in Studio); the
        # Specimens section carries the aliquot/chain-of-custody view.
        {"label": "Test Catalogue", "section": "test-catalogue"},
        {"label": "Panels", "section": "panels"},
        {"label": "Specimens", "section": "specimens"},
        {"label": "Send-outs", "section": "sendouts"},
        {"label": "POCT", "section": "poct"},
    ],
    "command": [
        # Phase 15: command desks + the transfer/access line join the
        # Command Center as deep-link sections (support_ops data).
        {"label": "Desks", "section": "desks"},
        {"label": "Access line", "section": "access-line"},
    ],
    "devices": [
        # Phase 15 (H): the RFID tag registry / reader events panel
        # rides the Devices module (Phase 14 device spine).
        {"label": "RFID", "section": "rfid"},
        # Phase 16 (I): HTM work orders + preventive maintenance ride
        # the Devices module the same way.
        {"label": "Maintenance", "section": "maintenance"},
    ],
    "pharmacy": [
        {"label": "Verification", "section": "verification"},
        {"label": "Inventory & procurement", "section": "inventory"},
        {"label": "Controlled substances", "section": "controlled"},
        {"label": "Compounding", "section": "compounding"},
        {"label": "BCMA", "section": "bcma"},
    ],
}


def default_navigation_payload() -> dict:
    items = []
    for index, (module, label, roles, group) in enumerate(_DEFAULT_NAV_MODULES):
        item: dict = {"module": module, "label": label, "roles": list(roles),
                      "order": index + 1, "visible": True, "group": group}
        purpose_icon = _NAV_PURPOSE_ICON.get(module)
        if purpose_icon:
            item["purpose"], item["icon"] = purpose_icon
        sections = _DEFAULT_NAV_SECTIONS.get(module)
        if sections:
            item["sections"] = [dict(s) for s in sections]
        items.append(item)
    return {"items": items, "groupOrder": list(NAV_GROUP_ORDER)}


# ---------------------------------------------------------------------------
# Job profiles (Home & Module UX pass): navigation kind, key
# "job-profiles", payload {"profiles": {"<job_role>": {"modules":
# [...]}}}. A site narrows each JOB ROLE's menu to the modules that
# job actually works in; the effective menu is the published-nav
# role/permissions resolution INTERSECTED with the profile (see
# effective_modules). Seeded defaults cover the people.py workforce
# job catalogue; front-desk-like roles stay at <= 6 modules.
# ---------------------------------------------------------------------------

def default_job_profiles_payload() -> dict:
    return {"profiles": {
        "physician": {"modules": [
            "patients", "inbox", "scheduling", "lab", "pharmacy", "ed",
            "radiology", "unit-board", "clinic", "aftercare"]},
        "nurse": {"modules": [
            "patients", "inbox", "scheduling", "beds", "unit-board",
            "clinic", "aftercare", "safety"]},
        "pharmacist": {"modules": [
            "pharmacy", "patients", "inbox", "lab"]},
        "lab-technologist": {"modules": [
            "lab", "patients", "inbox", "devices"]},
        "radiographer": {"modules": [
            "radiology", "patients", "inbox", "scheduling"]},
        "unit-manager": {"modules": [
            "beds", "command", "unit-board", "scheduling", "safety",
            "workforce", "teams", "reports"]},
        "department-head": {"modules": [
            "command", "beds", "safety", "workforce", "teams",
            "pophealth", "reports", "analytics"]},
        "executive": {"modules": [
            "command", "beds", "workforce", "pophealth", "reports",
            "analytics"]},
        "admin-staff": {"modules": [
            "patients", "scheduling", "billing", "workforce",
            "reports", "documents"]},
        "support-staff": {"modules": [
            "support", "beds", "command", "scheduling"]},
        "front-desk": {"modules": [
            "patients", "inbox", "scheduling", "billing", "clinic",
            "support"]},
    }}


#: Operator roles administer the whole system: job profiles scope
#: STAFF menus, never operator menus. An operator sign-in linked to a
#: staff record (the demo directory links dev-admin to an executive)
#: must still receive the full role-resolved menu — the seeded
#: executive profile otherwise hides Laboratory, Pharmacy, Surgery
#: and Studio from the administrator's own card Home. The web shell
#: applies the same exemption in session.ts applyJobProfile, and the
#: /home/effective-modules endpoint reports job_role null for
#: operators, so no layer can narrow an operator's menu.
OPERATOR_ROLES = frozenset({"admin", "configurator"})


def caller_job_role(session: Session, user) -> str | None:
    """The caller's job role, resolved from their linked staff
    record: a StaffMember whose `username` matches the sign-in, else
    the StaffMember named by their UserAccount's `staff_id` link
    (the Front Door / workforce-onboarding link). None when the
    sign-in has no staff record — the caller is then un-profiled."""
    if user is None or not getattr(user, "username", None):
        return None
    staff = session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).first()
    if staff is None:
        account = session.scalars(select(UserAccount).where(
            UserAccount.username == user.username)).first()
        if account is not None and account.staff_id:
            staff = session.scalars(select(StaffMember).where(
                StaffMember.staff_id == account.staff_id)).first()
    if staff is not None and isinstance(staff.job_role, str) \
            and staff.job_role.strip():
        return staff.job_role
    return None


def _published_nav_items(session: Session) -> list[dict]:
    """The effective navigation items: the PUBLISHED navigation/main
    payload's items, falling back to the built-in seed payload when
    nothing is published."""
    cfg = published_config(session, "navigation", NAVIGATION_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    items = payload.get("items")
    if isinstance(items, list) and items:
        return [i for i in items if isinstance(i, dict)]
    return default_navigation_payload()["items"]


def _role_resolved_modules(session: Session, user) -> list[str]:
    """The published-nav role resolution for the caller: visible
    items whose `roles` include the caller's role, in published
    order. (The seed roles mirror each page's capability gate — the
    test_nav_seed_consistency law — so this IS the role/permissions
    resolution the shell applies.)"""
    role_value = user.role.value if getattr(user, "role", None) else None
    resolved = []
    for item in _published_nav_items(session):
        if item.get("visible") is False:
            continue
        roles = item.get("roles")
        if isinstance(roles, list) and role_value not in roles:
            continue
        module = item.get("module")
        if isinstance(module, str) and module and module not in resolved:
            resolved.append(module)
    return resolved


def effective_modules(session: Session, user) -> list[str]:
    """The caller's effective menu: ordered module keys from the
    published-nav role/permissions resolution, intersected with
    their job profile (published navigation/job-profiles) WHEN their
    job role is resolvable from their linked staff record and a
    profile exists for it. An empty intersection falls back to the
    un-profiled resolution — a profile NEVER empties the menu. The
    patient role is unaffected (portal resolution only). Operator
    roles (admin / configurator) are EXEMPT: they administer the
    whole system, so the job-profile intersection never applies to
    them, however their sign-in is staff-linked."""
    base = _role_resolved_modules(session, user)
    if user is None or getattr(user, "role", None) is None \
            or user.role.value == "patient":
        return base
    if user.role.value in OPERATOR_ROLES:
        return base
    job_role = caller_job_role(session, user)
    if job_role is None:
        return base
    cfg = published_config(session, "navigation", JOB_PROFILES_KEY)
    profiles = ((cfg.payload or {}).get("profiles")
                if cfg is not None else None)
    if not isinstance(profiles, dict):
        return base
    profile = profiles.get(job_role)
    if not isinstance(profile, dict):
        return base
    modules = profile.get("modules")
    if not isinstance(modules, list):
        return base
    wanted = {m for m in modules if isinstance(m, str)}
    filtered = [m for m in base if m in wanted]
    return filtered or base


def ensure_job_profiles_default(session: Session) -> None:
    """Seed navigation/job-profiles as published v1 at startup,
    following the studio_config._seed_published pattern exactly
    (insert-if-absent: any existing version means the seed already
    ran — never overwrite a site's republication). Lives here, next
    to the payload it seeds, because app.studio_config is outside
    this pass's file ownership; called from app.main at startup."""
    if list_config_versions(session, "navigation", JOB_PROFILES_KEY):
        return
    from .audit import append_audit
    row = StudioConfig(kind="navigation", key=JOB_PROFILES_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=default_job_profiles_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:navigation/{JOB_PROFILES_KEY}@v1",
                 payload={"kind": "navigation", "key": JOB_PROFILES_KEY,
                          "version": 1, "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:navigation/{JOB_PROFILES_KEY}@v1",
                 payload={"kind": "navigation", "key": JOB_PROFILES_KEY,
                          "version": 1, "superseded_version": None,
                          "seeded_default": True})
    session.commit()


def default_inventory_taxonomy_payload() -> dict:
    """Seed inventory taxonomy (Inventory & Materials pass): the
    hospital materials vocabulary — eight categories with detailed
    subcategories, the adjustment reason codes stock corrections must
    name, and the expiry warning window (days) the expiry worklist
    reads. Sites reshape all of it through the governed Studio
    draft -> publish lifecycle (kind "inventory", key "taxonomy");
    this payload is only the first-boot default."""

    def cat(code, label, subs, class_policy=None):
        entry = {"code": code, "label": label,
                 "subcategories": [{"code": c, "label": l} for c, l in subs]}
        if class_policy is not None:
            entry["classPolicy"] = dict(class_policy)
        return entry

    return {
        "categories": [
            cat("instruments", "Instruments", [
                ("surgical-instruments", "Surgical instruments"),
                ("endoscopic-instruments", "Endoscopic instruments"),
                ("orthopedic-instruments", "Orthopaedic instruments"),
                ("ophthalmic-instruments", "Ophthalmic instruments"),
                ("dental-instruments", "Dental instruments"),
                ("instrument-sets", "Instrument sets & trays"),
            ]),
            cat("medical-appliances", "Medical appliances", [
                ("mobility-aids", "Mobility aids"),
                ("respiratory-appliances", "Respiratory appliances"),
                ("patient-handling", "Patient handling & transfer"),
                ("stretchers-trolleys", "Stretchers & trolleys"),
                ("examination-appliances", "Examination appliances"),
            ]),
            cat("surgical-supplies", "Surgical appliances & supplies", [
                ("sutures-staples", "Sutures & staples"),
                ("sterile-drapes-gowns", "Sterile drapes & gowns"),
                ("surgical-dressings", "Surgical dressings"),
                ("catheters-drains", "Catheters & drains"),
                ("anesthesia-supplies", "Anaesthesia supplies"),
                ("sterile-gloves", "Sterile gloves"),
            ]),
            cat("implants", "Implants", [
                ("orthopedic-implants", "Orthopaedic implants"),
                ("spinal-implants", "Spinal implants"),
                ("cardiovascular-implants", "Cardiovascular implants"),
                ("intraocular-lenses", "Intraocular lenses"),
                ("dental-implants", "Dental implants"),
                ("surgical-mesh", "Surgical mesh"),
            ]),
            cat("consumables", "Consumables", [
                ("syringes-needles", "Syringes & needles"),
                ("iv-consumables", "IV consumables"),
                ("wound-dressings", "Wound dressings (non-sterile)"),
                ("specimen-containers", "Specimen containers"),
                ("disinfectants-wipes", "Disinfectants & wipes"),
                ("tubing-lines", "Tubing & lines"),
            ]),
            cat("ppe", "PPE", [
                ("exam-gloves", "Examination gloves"),
                ("surgical-masks", "Surgical masks"),
                ("respirators", "Respirators (N95/FFP)"),
                ("gowns-aprons", "Gowns & aprons"),
                ("eye-protection", "Eye protection"),
                ("caps-covers", "Caps & shoe covers"),
            ]),
            cat("lab-reagents", "Lab reagents", [
                ("chemistry-reagents", "Chemistry reagents"),
                ("hematology-reagents", "Haematology reagents"),
                ("microbiology-media", "Microbiology media & plates"),
                ("immunoassay-reagents", "Immunoassay reagents"),
                ("stains-dyes", "Stains & dyes"),
                ("calibrators-controls", "Calibrators & controls"),
            ], class_policy={
                "containerTracked": True, "openStabilityDays": 30,
                "initialStage": "quarantine", "verificationRequired": True,
                "verificationValidityDays": None}),
            cat("linen", "Linen", [
                ("bed-linen", "Bed linen"),
                ("blankets", "Blankets"),
                ("patient-gowns", "Patient gowns"),
                ("towels", "Towels"),
                ("theatre-linen", "Theatre linen"),
            ]),
        ],
        "reasonCodes": [
            {"code": "damaged", "label": "Damaged"},
            {"code": "breakage", "label": "Breakage"},
            {"code": "lost", "label": "Lost / not found"},
            {"code": "expired-disposal", "label": "Expired — disposed"},
            {"code": "recall", "label": "Manufacturer recall"},
            {"code": "contamination", "label": "Contaminated"},
            {"code": "recount-correction", "label": "Recount correction"},
            {"code": "other", "label": "Other (note required)"},
        ],
        "quarantineReasonCodes": [
            {"code": "awaiting-verification",
             "label": "Awaiting verification"},
            {"code": "temperature-excursion",
             "label": "Temperature excursion"},
            {"code": "damaged-packaging", "label": "Damaged packaging"},
            {"code": "other", "label": "Other"},
        ],
        "recallReasonCodes": [
            {"code": "manufacturer-recall", "label": "Manufacturer recall"},
            {"code": "fda-recall", "label": "FDA recall"},
            {"code": "internal-quality", "label": "Internal quality issue"},
            {"code": "other", "label": "Other"},
        ],
        "disposalReasonCodes": [
            {"code": "expired", "label": "Expired"},
            {"code": "contaminated", "label": "Contaminated"},
            {"code": "damaged", "label": "Damaged"},
            {"code": "hazmat-disposal", "label": "Hazmat disposal"},
            {"code": "other", "label": "Other"},
        ],
        "expiryWarningDays": 90,
    }


def ensure_inventory_defaults(session: Session) -> None:
    """Seed inventory/taxonomy as published v1 at startup, following
    the studio_config._seed_published pattern exactly (insert-if-
    absent: any existing version means the seed already ran — never
    overwrite a site's republication). Called from app.main at
    startup, next to ensure_job_profiles_default."""
    if list_config_versions(session, "inventory", INVENTORY_TAXONOMY_KEY):
        return
    from .audit import append_audit
    row = StudioConfig(kind="inventory", key=INVENTORY_TAXONOMY_KEY,
                       version=1, status=ConfigStatus.published,
                       payload=default_inventory_taxonomy_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:inventory/{INVENTORY_TAXONOMY_KEY}@v1",
                 payload={"kind": "inventory",
                          "key": INVENTORY_TAXONOMY_KEY,
                          "version": 1, "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:inventory/{INVENTORY_TAXONOMY_KEY}@v1",
                 payload={"kind": "inventory",
                          "key": INVENTORY_TAXONOMY_KEY,
                          "version": 1, "superseded_version": None,
                          "seeded_default": True})
    session.commit()


def default_branding_payload() -> dict:
    # orgName + primaryColor are the web shell's literals (App.tsx brand
    # heading, styles.css --accent); portalTitle is the portal heading.
    return {"orgName": "OpenCare", "portalTitle": "My results",
            "primaryColor": "#0b6e4f"}


# ---------------------------------------------------------------------------
# Workflow machine loading (shared by every config-driven state machine)
# ---------------------------------------------------------------------------

def machine_from_payload(payload, default_transitions):
    """(transitions, known states, per-transition roles) from a workflow
    payload. `roles` maps (from, to) -> list of role names for transitions
    that declare a `roles` list; transitions without one are absent from
    the map, meaning any otherwise-authorised role may perform them."""
    payload = payload or {}
    transitions: dict[str, set[str]] = {}
    roles: dict[tuple[str, str], list[str]] = {}
    for t in payload.get("transitions") or []:
        if isinstance(t, dict) and t.get("from") and t.get("to"):
            transitions.setdefault(t["from"], set()).add(t["to"])
            if isinstance(t.get("roles"), list):
                roles[(t["from"], t["to"])] = [
                    r for r in t["roles"] if isinstance(r, str)]
    states = set(payload.get("states") or transitions.keys())
    for targets in transitions.values():
        states |= targets
    return transitions, states, roles


def load_workflow_machine(session: Session, key: str,
                          default_transitions):
    """machine_from_payload over the PUBLISHED workflow config `key`,
    falling back to the built-in map when nothing is published."""
    cfg = published_config(session, "workflow", key)
    if cfg is not None:
        return machine_from_payload(cfg.payload, default_transitions)
    return ({src: set(dst) for src, dst in default_transitions.items()},
            set(default_transitions.keys()), {})


def transition_role_allowed(roles: dict, current, target, role_value: str) -> bool:
    """True when the transition declares no role restriction, or the
    caller's role is in the declared list. A restriction naming no valid
    caller (empty list) blocks everyone — that is what the config says."""
    allowed = roles.get((current, target))
    return allowed is None or role_value in allowed


def payload_state_set(payload, field: str, fallback) -> set:
    """A state-set field (governedStates / activeStates / ...) from a
    workflow payload, falling back to the built-in set when the field is
    absent or malformed."""
    values = (payload or {}).get(field)
    if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values):
        return set(values)
    return set(fallback)


def workflow_payload(session: Session, key: str) -> dict | None:
    cfg = published_config(session, "workflow", key)
    return (cfg.payload or {}) if cfg is not None else None


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_config_version(session: Session, kind: str, key: str, version: int) -> StudioConfig | None:
    return session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key,
                                   StudioConfig.version == version)).first()


def list_config_versions(session: Session, kind: str, key: str) -> list[StudioConfig]:
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key)
        .order_by(StudioConfig.version.asc())).all())


def published_config(session: Session, kind: str, key: str) -> StudioConfig | None:
    """The effective (published) version of one config object, or None."""
    return session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key,
                                   StudioConfig.status == ConfigStatus.published)).first()


def published_configs(session: Session, kind: str) -> list[StudioConfig]:
    """All effective (published) config objects of one kind."""
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.status == ConfigStatus.published)
        .order_by(StudioConfig.key.asc())).all())


# ---------------------------------------------------------------------------
# Permissions: published overrides merged over built-in defaults
# ---------------------------------------------------------------------------

def role_allowed(session: Session, role: Role, capability: str) -> bool:
    cfg = published_config(session, "permissions", PERMISSIONS_KEY)
    if cfg is not None:
        overrides = (cfg.payload or {}).get("capabilities", {}).get(capability, {})
        if isinstance(overrides, dict) and role.value in overrides:
            return bool(overrides[role.value])
    return role.value in CAPABILITY_DEFAULTS.get(capability, ())


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

class ConfigPayloadError(Exception):
    """Payload failed validation. status_code is 422 for structural problems;
    400 for the navigation modules-only rule (a product rule, not a syntax
    problem), so the API can return exactly the code the rule specifies."""

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _fail(detail: str, status_code: int = 422):
    raise ConfigPayloadError(detail, status_code=status_code)


def _require_obj(payload) -> dict:
    if not isinstance(payload, dict):
        _fail("Config payload must be a JSON object")
    return payload


def _require_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"'{field}' is required and must be a non-empty string")
    return value


def _check_roles(value, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or any(r not in ROLE_NAMES for r in value):
        _fail(f"'{field}' must be a list of role names drawn from {list(ROLE_NAMES)}")


def validate_form(payload) -> None:
    """FHIR Questionnaire-shaped form definition."""
    payload = _require_obj(payload)
    if payload.get("resourceType") not in (None, "Questionnaire"):
        _fail("'resourceType' must be 'Questionnaire' for form configs")
    items = payload.get("item")
    if not isinstance(items, list) or not items:
        _fail("Form config requires a non-empty 'item' list")
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"item[{i}] must be an object")
        link_id = _require_str(item.get("linkId"), f"item[{i}].linkId")
        if link_id in seen:
            _fail(f"Duplicate linkId '{link_id}' in form items")
        seen.add(link_id)
        _require_str(item.get("text"), f"item[{i}].text")
        if item.get("type") not in FORM_ITEM_TYPES:
            _fail(f"item[{i}].type must be one of {list(FORM_ITEM_TYPES)}")
        if "required" in item and not isinstance(item["required"], bool):
            _fail(f"item[{i}].required must be a boolean")
        if "valueSet" in item and not isinstance(item["valueSet"], str):
            _fail(f"item[{i}].valueSet must be a string reference")


def validate_workflow(payload) -> None:
    """State-machine definition: states + transitions (from/to/action/roles)."""
    payload = _require_obj(payload)
    states = payload.get("states")
    if not isinstance(states, list) or not states or any(
            not isinstance(s, str) or not s for s in states):
        _fail("Workflow config requires a non-empty 'states' list of strings")
    if len(set(states)) != len(states):
        _fail("Workflow 'states' must be unique")
    transitions = payload.get("transitions")
    if not isinstance(transitions, list):
        _fail("Workflow config requires a 'transitions' list")
    state_set = set(states)
    for i, t in enumerate(transitions):
        if not isinstance(t, dict):
            _fail(f"transitions[{i}] must be an object")
        for field in ("from", "to"):
            if t.get(field) not in state_set:
                _fail(f"transitions[{i}].{field} must be one of the declared states")
        if "action" in t and not isinstance(t["action"], str):
            _fail(f"transitions[{i}].action must be a string")
        _check_roles(t.get("roles"), f"transitions[{i}].roles")
    # Optional policy fields the owning modules read at runtime (the
    # realignment moved these state sets / limits out of code constants):
    # governedStates (bare transitions refused), activeStates (board
    # census), specimenStates (lab specimen mirror), exportableStates
    # (837 export) — each a list of declared states.
    for field in ("governedStates", "activeStates", "specimenStates",
                  "exportableStates"):
        values = payload.get(field)
        if values is not None:
            if not isinstance(values, list) or any(
                    not isinstance(v, str) or v not in state_set for v in values):
                _fail(f"'{field}' must be a list of declared states")
    max_appeals = payload.get("maxAppeals")
    if max_appeals is not None and (isinstance(max_appeals, bool)
                                    or not isinstance(max_appeals, int)
                                    or max_appeals < 0):
        _fail("'maxAppeals' must be a non-negative integer")
    # Phase 7: the claim lifecycle may additionally carry
    # `requireCoding` (boolean) — when true, app.billing refuses claim
    # generation for an encounter with no HIM coding record. Absent =
    # false (the pre-Phase-7 behaviour).
    require_coding = payload.get("requireCoding")
    if require_coding is not None and not isinstance(require_coding, bool):
        _fail("'requireCoding' must be a boolean")


def validate_terminology(payload) -> None:
    """Code list / value set: concepts with code, display, active flag, and an
    optional referenceRange ({low, high, criticalLow, criticalHigh, unit})
    — lab panels, analytes and their ranges are data of exactly this shape.
    Extra concept fields (e.g. a panel's `components` list) pass through
    unvalidated: they are data for the owning module, not structure."""
    payload = _require_obj(payload)
    concepts = payload.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        _fail("Terminology config requires a non-empty 'concepts' list")
    seen: set[str] = set()
    for i, c in enumerate(concepts):
        if not isinstance(c, dict):
            _fail(f"concepts[{i}] must be an object")
        code = _require_str(c.get("code"), f"concepts[{i}].code")
        if code in seen:
            _fail(f"Duplicate concept code '{code}'")
        seen.add(code)
        _require_str(c.get("display"), f"concepts[{i}].display")
        if "active" in c and not isinstance(c["active"], bool):
            _fail(f"concepts[{i}].active must be a boolean")
        rng = c.get("referenceRange")
        if rng is not None:
            if not isinstance(rng, dict):
                _fail(f"concepts[{i}].referenceRange must be an object")
            for bound in ("low", "high", "criticalLow", "criticalHigh"):
                if bound in rng and not isinstance(rng[bound], (int, float)):
                    _fail(f"concepts[{i}].referenceRange.{bound} must be a number")
            if "unit" in rng and not isinstance(rng["unit"], str):
                _fail(f"concepts[{i}].referenceRange.unit must be a string")
        # Lab Depth D1 (compendium): age/sex-partitioned reference
        # ranges + the per-test directory attributes. Both are
        # OPTIONAL concept fields — validated only when present, so
        # every other terminology payload is unchanged. The lab
        # module (app.lab) is the consumer: partitions are selected
        # by interpret_value against the patient's age/sex, and the
        # directory's specimen requirements are enforced at order
        # placement.
        partitions = c.get("referenceRangePartitions")
        if partitions is not None:
            if not isinstance(partitions, list) or not partitions:
                _fail(f"concepts[{i}].referenceRangePartitions must be a "
                      "non-empty list")
            for j, part in enumerate(partitions):
                field = f"concepts[{i}].referenceRangePartitions[{j}]"
                if not isinstance(part, dict):
                    _fail(f"{field} must be an object")
                if "label" in part and not isinstance(part["label"], str):
                    _fail(f"{field}.label must be a string")
                if part.get("sex") not in (None, "male", "female", "any"):
                    _fail(f"{field}.sex must be 'male', 'female' or 'any'")
                for bound in ("ageMinYears", "ageMaxYears"):
                    value = part.get(bound)
                    if value is not None and (
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or value < 0):
                        _fail(f"{field}.{bound} must be a non-negative number")
                if part.get("ageMinYears") is not None \
                        and part.get("ageMaxYears") is not None \
                        and part["ageMinYears"] > part["ageMaxYears"]:
                    _fail(f"{field}.ageMinYears must not exceed ageMaxYears")
                for bound in ("low", "high", "criticalLow", "criticalHigh"):
                    if bound in part and not isinstance(part[bound], (int, float)):
                        _fail(f"{field}.{bound} must be a number")
        directory = c.get("directory")
        if directory is not None:
            _validate_lab_directory(directory, f"concepts[{i}].directory")


LAB_DIRECTORY_AVAILABILITY_MODES = ("24-7", "batched", "send-out")
LAB_DIRECTORY_TAT_CLOCKS = ("order-to-result", "receipt-to-result")
LAB_DIRECTORY_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _validate_lab_directory(directory, field: str) -> None:
    """Lab Depth D1: one concept's `directory` object — performing
    section/site, method, specimen requirements, availability and
    per-priority TAT targets (each with a DECLARED clock). Every
    sub-field is optional; an unset specimen requirement never
    blocks ordering (app.lab enforces only SET requirements)."""
    if not isinstance(directory, dict):
        _fail(f"'{field}' must be an object")
    for name in ("performingSection", "performingSite", "method"):
        if name in directory and not isinstance(directory[name], str):
            _fail(f"'{field}.{name}' must be a string")
    specimen = directory.get("specimen")
    if specimen is not None:
        if not isinstance(specimen, dict):
            _fail(f"'{field}.specimen' must be an object")
        for name in ("container", "specimenType"):
            if name in specimen and not isinstance(specimen[name], str):
                _fail(f"'{field}.specimen.{name}' must be a string")
        volume = specimen.get("minimumVolumeMl")
        if volume is not None and (isinstance(volume, bool)
                                   or not isinstance(volume, (int, float))
                                   or volume <= 0):
            _fail(f"'{field}.specimen.minimumVolumeMl' must be a "
                  "positive number")
        if "fastingRequired" in specimen \
                and not isinstance(specimen["fastingRequired"], bool):
            _fail(f"'{field}.specimen.fastingRequired' must be a boolean")
    availability = directory.get("availability")
    if availability is not None:
        if not isinstance(availability, dict):
            _fail(f"'{field}.availability' must be an object")
        mode = availability.get("mode")
        if mode not in LAB_DIRECTORY_AVAILABILITY_MODES:
            _fail(f"'{field}.availability.mode' must be one of "
                  f"{list(LAB_DIRECTORY_AVAILABILITY_MODES)}")
        if mode == "batched":
            schedule = availability.get("batchSchedule")
            if not isinstance(schedule, dict):
                _fail(f"'{field}.availability.batchSchedule' is required "
                      "when mode is 'batched'")
            days = schedule.get("days")
            if not isinstance(days, list) or not days or any(
                    d not in LAB_DIRECTORY_WEEKDAYS for d in days):
                _fail(f"'{field}.availability.batchSchedule.days' must be "
                      f"a non-empty list drawn from "
                      f"{list(LAB_DIRECTORY_WEEKDAYS)}")
            times = schedule.get("times")
            if not isinstance(times, list) or not times or any(
                    not isinstance(t, str)
                    or len(t) != 5 or t[2] != ":"
                    or not t[:2].isdigit() or not t[3:].isdigit()
                    for t in times):
                _fail(f"'{field}.availability.batchSchedule.times' must be "
                      "a non-empty list of 'HH:MM' strings")
        if mode == "send-out":
            lab_name = availability.get("referenceLab")
            if not isinstance(lab_name, str) or not lab_name.strip():
                _fail(f"'{field}.availability.referenceLab' is required "
                      "when mode is 'send-out'")
    targets = directory.get("tatTargets")
    if targets is not None:
        if not isinstance(targets, list) or not targets:
            _fail(f"'{field}.tatTargets' must be a non-empty list")
        seen: set[tuple] = set()
        for j, target in enumerate(targets):
            tfield = f"{field}.tatTargets[{j}]"
            if not isinstance(target, dict):
                _fail(f"'{tfield}' must be an object")
            priority = target.get("priority")
            if not isinstance(priority, str) or not priority.strip():
                _fail(f"'{tfield}.priority' is required and must be a "
                      "non-empty string")
            if target.get("clock") not in LAB_DIRECTORY_TAT_CLOCKS:
                _fail(f"'{tfield}.clock' must be one of "
                      f"{list(LAB_DIRECTORY_TAT_CLOCKS)}")
            minutes = target.get("targetMinutes")
            if isinstance(minutes, bool) \
                    or not isinstance(minutes, (int, float)) or minutes <= 0:
                _fail(f"'{tfield}.targetMinutes' must be a positive number")
            key = (priority, target.get("clock"))
            if key in seen:
                _fail(f"'{tfield}' duplicates the (priority, clock) pair "
                      f"{key} — one target per pair")
            seen.add(key)


NAV_MODULES_ONLY_MESSAGE = (
    "Navigation menu items may only reference modules: pages and forms live "
    "inside modules and cannot be menu entries (System Studio rule)."
)


_SECTION_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


PURPOSE_MAX_LENGTH = 140
ICON_MAX_LENGTH = 40


def validate_job_profiles(payload) -> None:
    """Job profiles (Home & Module UX pass): {"profiles":
    {"<job_role>": {"modules": [module keys]}}}. Job role names are
    site data — unknown roles are ALLOWED (sites define their own
    job catalogue) — but every module key must be a real navigation
    module, so a profile can never point the menu at nothing."""
    payload = _require_obj(payload)
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        _fail("'profiles' must be an object mapping job role -> "
              "{'modules': [module keys]}")
    for job_role, profile in profiles.items():
        if not isinstance(job_role, str) or not job_role.strip():
            _fail("Job profile keys must be non-empty job role strings")
        if not isinstance(profile, dict):
            _fail(f"profiles.{job_role} must be an object with a "
                  "'modules' list")
        modules = profile.get("modules")
        if not isinstance(modules, list) or any(
                not isinstance(m, str) for m in modules):
            _fail(f"profiles.{job_role}.modules must be a list of "
                  "module key strings")
        for module in modules:
            if module not in NAV_MODULE_KEYS:
                _fail(f"profiles.{job_role}.modules names unknown "
                      f"module '{module}'")


def validate_inventory(payload) -> None:
    """Inventory taxonomy (Inventory & Materials pass):
    {"categories": [{"code", "label", "subcategories": [{"code",
    "label"}]}], "reasonCodes": [{"code", "label"}],
    "expiryWarningDays": int}. The item master and every stock
    adjustment validate against this published vocabulary, so codes
    are slug-shaped and unique (subcategory codes unique within their
    category), every category carries at least one subcategory, and
    the expiry warning window is a bounded day count."""
    payload = _require_obj(payload)
    for key in payload:
        if key not in ("categories", "reasonCodes", "expiryWarningDays",
                       "quarantineReasonCodes", "recallReasonCodes",
                       "disposalReasonCodes"):
            _fail(f"inventory taxonomy has unknown field '{key}'")

    def _validate_reason_list(field, required):
        reasons = payload.get(field)
        if reasons is None:
            if required:
                _fail(f"'{field}' must be a non-empty list")
            return
        if not isinstance(reasons, list) or not reasons:
            _fail(f"'{field}' must be a non-empty list")
        seen: set[str] = set()
        for entry in reasons:
            if not isinstance(entry, dict):
                _fail(f"each entry in '{field}' must be an object "
                      "{code, label}")
            code = entry.get("code")
            if not isinstance(code, str) \
                    or not _SECTION_SLUG_RE.match(code):
                _fail(f"code '{code}' in '{field}' must be a slug "
                      "(a-z, 0-9, -)")
            if code in seen:
                _fail(f"duplicate code '{code}' in '{field}'")
            seen.add(code)
            label = entry.get("label")
            if not isinstance(label, str) or not label.strip():
                _fail(f"code '{code}' in '{field}' needs a non-empty "
                      "label")

    def _validate_class_policy(policy, owner):
        if not isinstance(policy, dict):
            _fail(f"classPolicy on '{owner}' must be an object")
        for key in policy:
            if key not in ("containerTracked", "openStabilityDays",
                           "initialStage", "verificationRequired",
                           "verificationValidityDays"):
                _fail(f"classPolicy on '{owner}' has unknown field "
                      f"'{key}'")
        for key in ("containerTracked", "verificationRequired"):
            if key in policy and not isinstance(policy[key], bool):
                _fail(f"classPolicy '{key}' on '{owner}' must be a "
                      "boolean")
        if "openStabilityDays" in policy:
            value = policy["openStabilityDays"]
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, int)
                                      or value < 0):
                _fail(f"classPolicy 'openStabilityDays' on '{owner}' "
                      "must be an integer >= 0 or null")
        if "initialStage" in policy:
            if policy["initialStage"] not in ("received", "quarantine",
                                              "released"):
                _fail(f"classPolicy 'initialStage' on '{owner}' must "
                      "be one of received|quarantine|released")
        if "verificationValidityDays" in policy:
            value = policy["verificationValidityDays"]
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, int)
                                      or value <= 0):
                _fail(f"classPolicy 'verificationValidityDays' on "
                      f"'{owner}' must be an integer > 0 or null")

    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        _fail("'categories' must be a non-empty list")
    seen_categories: set[str] = set()
    for entry in categories:
        if not isinstance(entry, dict):
            _fail("each category must be an object "
                  "{code, label, subcategories}")
        code = entry.get("code")
        if not isinstance(code, str) or not _SECTION_SLUG_RE.match(code):
            _fail(f"category code '{code}' must be a slug (a-z, 0-9, -)")
        if code in seen_categories:
            _fail(f"duplicate category code '{code}'")
        seen_categories.add(code)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            _fail(f"category '{code}' needs a non-empty label")
        if "classPolicy" in entry:
            _validate_class_policy(entry["classPolicy"], code)
        subs = entry.get("subcategories")
        if not isinstance(subs, list) or not subs:
            _fail(f"category '{code}' must carry at least one "
                  "subcategory")
        seen_subs: set[str] = set()
        for sub in subs:
            if not isinstance(sub, dict):
                _fail(f"category '{code}' subcategories must be "
                      "objects {code, label}")
            scode = sub.get("code")
            if not isinstance(scode, str) or not _SECTION_SLUG_RE.match(scode):
                _fail(f"subcategory code '{scode}' in '{code}' must "
                      "be a slug (a-z, 0-9, -)")
            if scode in seen_subs:
                _fail(f"duplicate subcategory code '{scode}' in "
                      f"category '{code}'")
            seen_subs.add(scode)
            slabel = sub.get("label")
            if not isinstance(slabel, str) or not slabel.strip():
                _fail(f"subcategory '{scode}' in '{code}' needs a "
                      "non-empty label")
            if "classPolicy" in sub:
                _validate_class_policy(sub["classPolicy"], scode)
    _validate_reason_list("reasonCodes", required=True)
    _validate_reason_list("quarantineReasonCodes", required=False)
    _validate_reason_list("recallReasonCodes", required=False)
    _validate_reason_list("disposalReasonCodes", required=False)
    window = payload.get("expiryWarningDays")
    if isinstance(window, bool) or not isinstance(window, int) \
            or not 1 <= window <= 365:
        _fail("'expiryWarningDays' must be an integer between 1 "
              "and 365")


def validate_navigation(payload) -> None:
    """Menu items: label, module, roles, order, visible, plus optional
    `group` (string), optional `sections` (deep-links into the
    module), and optional `purpose` (one line, <= 140 chars) + `icon`
    (short name) for the Home card menu. Modules ONLY — an item
    naming a page or a form is rejected with HTTP 400; a sections
    entry naming a module/page/form is rejected the same way
    (sections are views inside a module, never menu entries
    themselves).

    The navigation kind also carries the job-profiles object (key
    "job-profiles"): a payload with `profiles` and no `items` is
    validated by validate_job_profiles instead."""
    payload = _require_obj(payload)
    if "profiles" in payload:
        validate_job_profiles(payload)
        if "items" not in payload:
            return
    items = payload.get("items")
    if not isinstance(items, list):
        _fail("Navigation config requires an 'items' list")
    group_order = payload.get("groupOrder")
    if group_order is not None:
        if not isinstance(group_order, list) or any(
                not isinstance(g, str) or not g.strip()
                for g in group_order):
            _fail("'groupOrder' must be a list of non-empty group "
                  "label strings")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"items[{i}] must be an object")
        if item.get("type") in ("page", "form") or item.get("page") or item.get("form"):
            _fail(NAV_MODULES_ONLY_MESSAGE, status_code=400)
        if not isinstance(item.get("module"), str) or not item["module"].strip():
            _fail(NAV_MODULES_ONLY_MESSAGE, status_code=400)
        _require_str(item.get("label"), f"items[{i}].label")
        _check_roles(item.get("roles"), f"items[{i}].roles")
        if "order" in item and not isinstance(item["order"], int):
            _fail(f"items[{i}].order must be an integer")
        if "visible" in item and not isinstance(item["visible"], bool):
            _fail(f"items[{i}].visible must be a boolean")
        if "group" in item:
            group = item["group"]
            if not isinstance(group, str) or not group.strip():
                _fail(f"items[{i}].group must be a non-empty string")
        if "purpose" in item:
            purpose = item["purpose"]
            if not isinstance(purpose, str) or not purpose.strip():
                _fail(f"items[{i}].purpose must be a non-empty string")
            if len(purpose) > PURPOSE_MAX_LENGTH:
                _fail(f"items[{i}].purpose must be at most "
                      f"{PURPOSE_MAX_LENGTH} characters")
        if "icon" in item:
            icon = item["icon"]
            if not isinstance(icon, str) or not icon.strip():
                _fail(f"items[{i}].icon must be a non-empty string")
            if len(icon) > ICON_MAX_LENGTH:
                _fail(f"items[{i}].icon must be at most "
                      f"{ICON_MAX_LENGTH} characters")
        if "sections" in item:
            sections = item["sections"]
            if not isinstance(sections, list):
                _fail(f"items[{i}].sections must be a list")
            for j, entry in enumerate(sections):
                if not isinstance(entry, dict):
                    _fail(f"items[{i}].sections[{j}] must be an object")
                if entry.get("module") or entry.get("page") or entry.get("form") \
                        or entry.get("type") in ("page", "form", "module"):
                    _fail(NAV_MODULES_ONLY_MESSAGE, status_code=400)
                _require_str(entry.get("label"),
                             f"items[{i}].sections[{j}].label")
                section = entry.get("section")
                if not isinstance(section, str) or not _SECTION_SLUG_RE.match(section):
                    _fail(f"items[{i}].sections[{j}].section must be a slug "
                          "matching ^[a-z0-9][a-z0-9-]*$")


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


THEME_DENSITIES = ("comfortable", "compact")
THEME_CORNERS = ("rounded", "sharp")
THEME_FONT_SCALES = ("standard", "large")
THEME_STATUS_COLOR_KEYS = ("ok", "warn", "bad", "info")
_THEME_FIELDS = ("density", "corner", "statusColors", "fontScale",
                 "groupColors")


def validate_branding(payload) -> None:
    payload = _require_obj(payload)
    _require_str(payload.get("orgName"), "orgName")
    _require_str(payload.get("portalTitle"), "portalTitle")
    if "logoUrl" in payload and not isinstance(payload["logoUrl"], str):
        _fail("'logoUrl' must be a string")
    color = payload.get("primaryColor")
    if color is not None and not (isinstance(color, str) and _COLOR_RE.match(color)):
        _fail("'primaryColor' must be a #RRGGBB hex colour")
    # Home & Module UX pass: the OPTIONAL theme object (fixed
    # contract — {density, corner, statusColors, fontScale}, every
    # field itself optional). Absent = the shell's built-in defaults.
    # When present, enum values and status hex colours are validated
    # and unknown fields/keys are rejected as bad shapes.
    # Rich Design Elevation pass: theme.groupColors (optional,
    # partial allowed) maps a canonical nav group label
    # (NAV_GROUP_ORDER) to its identity colour; unknown group keys
    # are rejected with the same strictness as statusColors.
    theme = payload.get("theme")
    if theme is not None:
        if not isinstance(theme, dict):
            _fail("'theme' must be an object")
        for field in theme:
            if field not in _THEME_FIELDS:
                _fail(f"'theme.{field}' is not a known theme field "
                      f"(allowed: {list(_THEME_FIELDS)})")
        if "density" in theme and theme["density"] not in THEME_DENSITIES:
            _fail(f"'theme.density' must be one of {list(THEME_DENSITIES)}")
        if "corner" in theme and theme["corner"] not in THEME_CORNERS:
            _fail(f"'theme.corner' must be one of {list(THEME_CORNERS)}")
        if "fontScale" in theme \
                and theme["fontScale"] not in THEME_FONT_SCALES:
            _fail(f"'theme.fontScale' must be one of "
                  f"{list(THEME_FONT_SCALES)}")
        status_colors = theme.get("statusColors")
        if status_colors is not None:
            if not isinstance(status_colors, dict):
                _fail("'theme.statusColors' must be an object mapping "
                      "status name -> #RRGGBB hex colour")
            for name, value in status_colors.items():
                if name not in THEME_STATUS_COLOR_KEYS:
                    _fail(f"'theme.statusColors.{name}' is not a known "
                          f"status (allowed: "
                          f"{list(THEME_STATUS_COLOR_KEYS)})")
                if not (isinstance(value, str) and _COLOR_RE.match(value)):
                    _fail(f"'theme.statusColors.{name}' must be a "
                          "#RRGGBB hex colour")
        group_colors = theme.get("groupColors")
        if group_colors is not None:
            if not isinstance(group_colors, dict):
                _fail("'theme.groupColors' must be an object mapping "
                      "nav group label -> #RRGGBB hex colour")
            for label, value in group_colors.items():
                if label not in NAV_GROUP_ORDER:
                    _fail(f"'theme.groupColors.{label}' is not a known "
                          f"nav group (allowed: "
                          f"{list(NAV_GROUP_ORDER)})")
                if not (isinstance(value, str) and _COLOR_RE.match(value)):
                    _fail(f"'theme.groupColors.{label}' must be a "
                          "#RRGGBB hex colour")


def validate_permissions(payload) -> None:
    payload = _require_obj(payload)
    caps = payload.get("capabilities")
    if not isinstance(caps, dict) or not caps:
        _fail("Permissions config requires a non-empty 'capabilities' object")
    for capability, per_role in caps.items():
        # A capability NOT in CAPABILITY_DEFAULTS is accepted and stored:
        # sites may define new capabilities in configuration ahead of (or
        # without) code that enforces them. The distinction matters —
        # DEFINING a capability is configuration; the ENFORCEMENT POINTS
        # (which endpoint checks which capability) remain code-defined,
        # and role_allowed() resolves an undefined capability to its
        # published cells, defaulting to denied.
        if not isinstance(per_role, dict) or not per_role:
            _fail(f"capabilities.{capability} must be an object mapping role -> boolean")
        for role, allowed in per_role.items():
            if role not in ROLE_NAMES:
                _fail(f"capabilities.{capability} names unknown role '{role}'")
            if not isinstance(allowed, bool):
                _fail(f"capabilities.{capability}.{role} must be a boolean")


COHORT_COMPARATORS = ("<", "<=", ">", ">=", "==")


def validate_cohort(payload) -> None:
    """Population-health registry definition (Phase 3): a cohort is a set
    of condition codes and/or a lab rule (latest value vs threshold within
    a window). At least one criterion is required. `smallCellThreshold`
    (default 5) drives small-cell suppression in /pophealth responses."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    conditions = payload.get("conditionCodes")
    if conditions is not None:
        if not isinstance(conditions, list):
            _fail("'conditionCodes' must be a list of {system, code} objects")
        for i, c in enumerate(conditions):
            if not isinstance(c, dict):
                _fail(f"conditionCodes[{i}] must be an object with 'system' and 'code'")
            _require_str(c.get("code"), f"conditionCodes[{i}].code")
            if "system" in c and not isinstance(c["system"], str):
                _fail(f"conditionCodes[{i}].system must be a string")
    lab = payload.get("lab")
    if lab is not None:
        if not isinstance(lab, dict):
            _fail("'lab' must be an object")
        _require_str(lab.get("code"), "lab.code")
        if lab.get("comparator") not in COHORT_COMPARATORS:
            _fail(f"'lab.comparator' must be one of {list(COHORT_COMPARATORS)}")
        threshold = lab.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            _fail("'lab.threshold' must be a number")
        window = lab.get("windowDays")
        if window is not None and (isinstance(window, bool)
                                   or not isinstance(window, int) or window <= 0):
            _fail("'lab.windowDays' must be a positive integer")
    if not conditions and lab is None:
        _fail("Cohort config requires at least one of 'conditionCodes' or 'lab'")
    threshold_cells = payload.get("smallCellThreshold")
    if threshold_cells is not None and (isinstance(threshold_cells, bool)
                                        or not isinstance(threshold_cells, int)
                                        or threshold_cells < 1):
        _fail("'smallCellThreshold' must be a positive integer")
    bands = payload.get("ageBands")
    if bands is not None:
        if not isinstance(bands, list) or not bands:
            _fail("'ageBands' must be a non-empty list of [low, high, label] triples")
        for i, band in enumerate(bands):
            if not isinstance(band, list) or len(band) != 3:
                _fail(f"ageBands[{i}] must be a [low, high, label] triple")
            low, high, label = band
            if isinstance(low, bool) or not isinstance(low, int) or low < 0:
                _fail(f"ageBands[{i}][0] (low) must be a non-negative integer")
            if high is not None and (isinstance(high, bool)
                                     or not isinstance(high, int) or high < low):
                _fail(f"ageBands[{i}][1] (high) must be null or an integer >= low")
            if not isinstance(label, str) or not label:
                _fail(f"ageBands[{i}][2] (label) must be a non-empty string")


def validate_integration(payload) -> None:
    """Integration config (Phase 4B): external-system connection data —
    in v1 the DICOM viewer link target. Shape: an object whose known
    sections are objects; `viewer`, when present, must carry a string
    `baseUrl` (an http(s) URL the web app appends the study UID to).
    Unknown extra sections pass through: they are data for the owning
    integration, not structure.

    Phase 8 adds the HL7 interface-engine sections (used by the
    `hl7-endpoints` key): `facility` (MSH identity strings), `retry`
    ({maxAttempts, backoffSeconds}) and `endpoints` — a list of
    {name, host, port, messageTypes, enabled, receivingApplication?,
    receivingFacility?}. Those sections are validated when present so
    a malformed endpoint can never reach the dispatcher."""
    payload = _require_obj(payload)
    viewer = payload.get("viewer")
    if viewer is not None:
        if not isinstance(viewer, dict):
            _fail("'viewer' must be an object")
        base = viewer.get("baseUrl")
        if base is not None:
            if not isinstance(base, str) or not base.startswith(("http://", "https://")):
                _fail("'viewer.baseUrl' must be an http(s) URL string")
        if "name" in viewer and not isinstance(viewer["name"], str):
            _fail("'viewer.name' must be a string")
    facility = payload.get("facility")
    if facility is not None:
        if not isinstance(facility, dict):
            _fail("'facility' must be an object")
        for field in ("sendingApplication", "sendingFacility"):
            if field in facility and not isinstance(facility[field], str):
                _fail(f"'facility.{field}' must be a string")
    retry = payload.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            _fail("'retry' must be an object")
        if "maxAttempts" in retry:
            value = retry["maxAttempts"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _fail("'retry.maxAttempts' must be a positive integer")
        if "backoffSeconds" in retry:
            value = retry["backoffSeconds"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                _fail("'retry.backoffSeconds' must be a non-negative number")
    # Phase 17: the OIDC registration (key=oidc) and the session-security
    # policy (key=session-security) are FLAT payloads on this kind. Their
    # fields are validated when present; neither may carry a secret —
    # client secrets live in the identity provider / vault, never config.
    for field in ("issuer", "jwksUrl", "clientId", "audience", "rolesClaim"):
        if field in payload and not isinstance(payload[field], str):
            _fail(f"'{field}' must be a string")
    if "issuer" in payload and payload["issuer"] and \
            not str(payload["issuer"]).startswith(("http://", "https://")):
        _fail("'issuer' must be an http(s) URL string")
    if "jwksUrl" in payload and payload["jwksUrl"] and \
            not str(payload["jwksUrl"]).startswith(("http://", "https://")):
        _fail("'jwksUrl' must be an http(s) URL string")
    for field in ("clientSecret", "secret", "password"):
        if field in payload:
            _fail(f"'{field}' must never be stored in Studio config")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        _fail("'enabled' must be a boolean")
    for field in ("idleLockMinutes", "reentryGraceMinutes"):
        if field in payload:
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value <= 0:
                _fail(f"'{field}' must be a positive number")
    endpoints = payload.get("endpoints")
    if endpoints is not None:
        if not isinstance(endpoints, list):
            _fail("'endpoints' must be a list")
        seen_names: set[str] = set()
        for i, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, dict):
                _fail(f"endpoints[{i}] must be an object")
            name = _require_str(endpoint.get("name"), f"endpoints[{i}].name")
            if name in seen_names:
                _fail(f"Duplicate endpoint name '{name}'")
            seen_names.add(name)
            _require_str(endpoint.get("host"), f"endpoints[{i}].host")
            port = endpoint.get("port")
            if isinstance(port, bool) or not isinstance(port, int) \
                    or not (1 <= port <= 65535):
                _fail(f"endpoints[{i}].port must be an integer in 1..65535")
            types = endpoint.get("messageTypes")
            if not isinstance(types, list) or not types \
                    or not all(isinstance(t, str) and t for t in types):
                _fail(f"endpoints[{i}].messageTypes must be a non-empty list of strings")
            if "enabled" in endpoint and not isinstance(endpoint["enabled"], bool):
                _fail(f"endpoints[{i}].enabled must be a boolean")
            for field in ("receivingApplication", "receivingFacility"):
                if field in endpoint and not isinstance(endpoint[field], str):
                    _fail(f"endpoints[{i}].{field} must be a string")


def validate_orderset(payload) -> None:
    """Order set (Phase 5): a named bundle of order templates that CPOE
    expands into individual orders on placement. Shape: {name, orders:
    [{type, code, ...}]} — each item names an order TYPE (validated
    against the published order-types catalogue at placement time, not
    here: a set must remain publishable before/after catalogue edits)
    and a code. Extra item fields (display, performerRole, modality,
    dosage text, ...) pass through unvalidated: they are placement
    fields for the owning orders module, not structure."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    orders = payload.get("orders")
    if not isinstance(orders, list) or not orders:
        _fail("Order set config requires a non-empty 'orders' list")
    for i, item in enumerate(orders):
        if not isinstance(item, dict):
            _fail(f"orders[{i}] must be an object")
        _require_str(item.get("type"), f"orders[{i}].type")
        _require_str(item.get("code"), f"orders[{i}].code")
        if "display" in item and not isinstance(item["display"], str):
            _fail(f"orders[{i}].display must be a string")


def _validate_lab_rule(lab, field: str) -> None:
    """One lab criterion — the cohort `lab` shape (latest value vs
    threshold, optional window), shared by cohort and measure configs."""
    if not isinstance(lab, dict):
        _fail(f"'{field}' must be an object")
    _require_str(lab.get("code"), f"{field}.code")
    if lab.get("comparator") not in COHORT_COMPARATORS:
        _fail(f"'{field}.comparator' must be one of {list(COHORT_COMPARATORS)}")
    threshold = lab.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        _fail(f"'{field}.threshold' must be a number")
    window = lab.get("windowDays")
    if window is not None and (isinstance(window, bool)
                               or not isinstance(window, int) or window <= 0):
        _fail(f"'{field}.windowDays' must be a positive integer")


def _validate_code_list(values, field: str) -> None:
    if not isinstance(values, list):
        _fail(f"'{field}' must be a list of {{system, code}} objects")
    for i, c in enumerate(values):
        if not isinstance(c, dict):
            _fail(f"{field}[{i}] must be an object with 'system' and 'code'")
        _require_str(c.get("code"), f"{field}[{i}].code")
        if "system" in c and not isinstance(c["system"], str):
            _fail(f"{field}[{i}].system must be a string")


def _validate_measure_criteria(criteria, field: str) -> None:
    """One side (denominator or numerator) of a quality measure: a set
    of criteria in the SAME style as a cohort definition (condition
    codes and/or lab rules — evaluated by the same warehouse code in
    app.pophealth / app.quality), extended with `labs` (several lab
    rules, ALL must hold), `procedureCodes` and `ageRange`. At least
    one criterion is required; criteria combine as AND."""
    if not isinstance(criteria, dict):
        _fail(f"'{field}' must be an object of measure criteria")
    present = False
    if criteria.get("conditionCodes") is not None:
        _validate_code_list(criteria["conditionCodes"], f"{field}.conditionCodes")
        present = present or bool(criteria["conditionCodes"])
    if criteria.get("procedureCodes") is not None:
        _validate_code_list(criteria["procedureCodes"], f"{field}.procedureCodes")
        present = present or bool(criteria["procedureCodes"])
    if criteria.get("lab") is not None:
        _validate_lab_rule(criteria["lab"], f"{field}.lab")
        present = True
    if criteria.get("labs") is not None:
        labs = criteria["labs"]
        if not isinstance(labs, list) or not labs:
            _fail(f"'{field}.labs' must be a non-empty list of lab rules")
        for i, lab in enumerate(labs):
            _validate_lab_rule(lab, f"{field}.labs[{i}]")
        present = True
    age = criteria.get("ageRange")
    if age is not None:
        if not isinstance(age, list) or len(age) != 2:
            _fail(f"'{field}.ageRange' must be a [low, high] pair")
        low, high = age
        for bound in (low, high):
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                _fail(f"'{field}.ageRange' bounds must be non-negative integers")
        if high < low:
            _fail(f"'{field}.ageRange' high must be >= low")
        present = True
    if not present:
        _fail(f"'{field}' requires at least one criterion (conditionCodes, "
              "procedureCodes, lab, labs or ageRange)")


def validate_measure(payload) -> None:
    """Quality measure definition (Phase 9): {name, title?,
    description?, denominator, numerator, smallCellThreshold?}. The
    numerator/denominator are cohort-style criteria (see
    _validate_measure_criteria); the measure ENGINE (app.quality)
    evaluates them with the population-health evaluator over the
    warehouse — a measure is configuration, never code."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    for field in ("title", "description"):
        if field in payload and not isinstance(payload[field], str):
            _fail(f"'{field}' must be a string")
    if "denominator" not in payload:
        _fail("Measure config requires a 'denominator' criteria object")
    if "numerator" not in payload:
        _fail("Measure config requires a 'numerator' criteria object")
    _validate_measure_criteria(payload["denominator"], "denominator")
    _validate_measure_criteria(payload["numerator"], "numerator")
    threshold_cells = payload.get("smallCellThreshold")
    if threshold_cells is not None and (isinstance(threshold_cells, bool)
                                        or not isinstance(threshold_cells, int)
                                        or threshold_cells < 1):
        _fail("'smallCellThreshold' must be a positive integer")


# ---------------------------------------------------------------------------
# Phase 10 kinds: agent / rule / dashboard / regulatory_pack
# ---------------------------------------------------------------------------

# Agent tool registry (Track A): the ONLY tools an agent definition may
# allow-list. Tools are server code; a config naming anything else is
# rejected at draft time, exactly like the Agentic_App ADK pattern. The
# one tool is a caller-token API call back into this backend, restricted
# per-agent by `apiAllowlist` path prefixes.
AGENT_TOOL_NAMES = ("opencare_api_call",)

AGENT_MODALITIES = ("text", "image", "audio")


def validate_agent(payload) -> None:
    """Agent definition (Phase 10, Track A): a Studio-managed ADK agent.
    {description?, model, instructions, tools, apiAllowlist?,
    modalities?, temperature?, maxSteps?}. `model` is a LiteLLM model
    string (e.g. "ollama_chat/llama3.2:3b"); an EMPTY model is valid
    configuration — it means the agent has no model route and every run
    is honestly refused (503), never faked."""
    payload = _require_obj(payload)
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    # C-AI (AI-G1): optional riskTier stamps the agent's governance
    # tier (framework §3). Absent is valid (governed as T2 by
    # four_eyes.agent_tier_of); present it must be a real tier.
    if "riskTier" in payload and payload["riskTier"] not in ("T1", "T2", "T3"):
        _fail("'riskTier' must be one of 'T1', 'T2', 'T3' when present")
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        _fail("'model' must be a LiteLLM model string (or empty for no model route)")
    if isinstance(model, str) and any(s in model for s in ("sk-", "api_key", "secret")):
        _fail("'model' looks like a credential; model routes never carry secrets "
              "— provider keys live on the runtime, not in configuration")
    _require_str(payload.get("instructions"), "instructions")
    tools = payload.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list) or any(t not in AGENT_TOOL_NAMES for t in tools):
        _fail(f"'tools' must be a list drawn from {list(AGENT_TOOL_NAMES)}")
    allowlist = payload.get("apiAllowlist")
    if allowlist is not None:
        if not isinstance(allowlist, list) or any(
                not isinstance(p, str) or not p.startswith("/") for p in allowlist):
            _fail("'apiAllowlist' must be a list of API path prefixes starting with '/'")
    modalities = payload.get("modalities")
    if modalities is not None:
        if not isinstance(modalities, dict):
            _fail("'modalities' must be an object")
        for key, value in modalities.items():
            if key not in AGENT_MODALITIES:
                _fail(f"'modalities' keys must be drawn from {list(AGENT_MODALITIES)}")
            if not isinstance(value, bool):
                _fail(f"'modalities.{key}' must be a boolean")
    temperature = payload.get("temperature")
    if temperature is not None and (isinstance(temperature, bool)
                                    or not isinstance(temperature, (int, float))
                                    or not (0 <= temperature <= 2)):
        _fail("'temperature' must be a number in 0..2")
    max_steps = payload.get("maxSteps")
    if max_steps is not None and (isinstance(max_steps, bool)
                                  or not isinstance(max_steps, int)
                                  or not (1 <= max_steps <= 50)):
        _fail("'maxSteps' must be an integer in 1..50")


# Rule engine registries (Track B): events are emitted by server code at
# named domain moments; actions are server code too. A rule config only
# ever BINDS an event + a governed condition to an action — there is no
# expression language beyond the structured condition below, and no
# arbitrary code anywhere (the Track B clinical-safety rule).
RULE_EVENTS = ("lab.report.released", "inpatient.census.changed",
               "pharmacy.dur.screened", "continuum.boarding.changed",
               # Phase 13: the workforce credential scan emits this per
               # credential inside the configured alert window (or
               # already expired) that has not been alerted for its
               # current expiry date.
               "workforce.credential.expiring",
               # Phase 14: device ingestion (app.devices) emits these —
               # a storage sensor reading crossing its published range
               # (transition into excursion), and a vitals Observation
               # outside the published per-code thresholds.
               "device.temperature.excursion",
               "device.vitals.breach",
               # Phase 15: RFID reader ingestion (app.rfid) emits this
               # when a device-bound asset tag is sighted in a zone
               # other than the device registry's assigned zone.
               "rfid.zone.mismatch",
               # Phase 15: the radiology critical-result scan
               # (app.radiology) emits this for critical results still
               # unacknowledged past the published SLA.
               "radiology.critical.overdue",
               # Phase 15: the results-inbox SLA scan
               # (app.results_inbox) emits this for released critical
               # results acknowledged by nobody past the published
               # integration/inbox SLA.
               "inbox.critical.unacknowledged")
RULE_ACTIONS = ("open_critical_notification", "create_task", "audit_alert")
RULE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains")


def _validate_rule_condition(cond, field: str, depth: int = 0) -> None:
    if not isinstance(cond, dict):
        _fail(f"'{field}' must be a condition object")
    if depth > 1:
        _fail(f"'{field}' nests too deep (all/any may wrap leaf conditions once)")
    groups = [g for g in ("all", "any") if g in cond]
    if groups:
        if len(cond) != 1:
            _fail(f"'{field}' must be either a group (all/any) or a leaf condition")
        group = groups[0]
        leaves = cond[group]
        if not isinstance(leaves, list) or not leaves:
            _fail(f"'{field}.{group}' must be a non-empty list of conditions")
        for i, leaf in enumerate(leaves):
            _validate_rule_condition(leaf, f"{field}.{group}[{i}]", depth + 1)
        return
    _require_str(cond.get("signal"), f"{field}.signal")
    if cond.get("op") not in RULE_OPS:
        _fail(f"'{field}.op' must be one of {list(RULE_OPS)}")
    if "value" not in cond:
        _fail(f"'{field}.value' is required")
    value = cond["value"]
    if cond["op"] == "in" and not isinstance(value, list):
        _fail(f"'{field}.value' must be a list for the 'in' operator")
    if isinstance(value, (dict, list)) and cond["op"] != "in":
        _fail(f"'{field}.value' must be a scalar for operator '{cond['op']}'")


def validate_rule(payload) -> None:
    """Automation rule (Phase 10, Track B): {name, event, condition,
    action: {type, params?}, enabled?}. Conditions are structured data
    over the event's named signals — never code."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    if payload.get("event") not in RULE_EVENTS:
        _fail(f"'event' must be one of {list(RULE_EVENTS)}")
    _validate_rule_condition(payload.get("condition"), "condition")
    action = payload.get("action")
    if not isinstance(action, dict):
        _fail("'action' must be an object")
    if action.get("type") not in RULE_ACTIONS:
        _fail(f"'action.type' must be one of {list(RULE_ACTIONS)}")
    params = action.get("params")
    if params is not None and not isinstance(params, dict):
        _fail("'action.params' must be an object")
    if action.get("type") == "create_task":
        _require_str((params or {}).get("title"), "action.params.title")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        _fail("'enabled' must be a boolean")


DASHBOARD_DISPLAYS = ("total", "count", "value",
                      # Phase 13 (Track F visualisation): chart widget
                      # types rendered by the shared DashboardView from
                      # the SAME endpoint contract — the response's
                      # `items` array (line/bar/table) or one named
                      # numeric field (gauge/big-number). A chart widget
                      # carries only composition data (series fields,
                      # column titles, thresholds): still no queries.
                      "big-number", "line", "bar", "gauge", "table")


def _validate_widget_series(widget, i: int) -> None:
    series = widget.get("series")
    if not isinstance(series, list) or not series:
        _fail(f"widgets[{i}].series must be a non-empty list of "
              "{field, label?} for chart displays")
    for j, entry in enumerate(series):
        if not isinstance(entry, dict):
            _fail(f"widgets[{i}].series[{j}] must be an object")
        _require_str(entry.get("field"), f"widgets[{i}].series[{j}].field")
        if "label" in entry and not isinstance(entry["label"], str):
            _fail(f"widgets[{i}].series[{j}].label must be a string")


def validate_dashboard(payload) -> None:
    """Dashboard definition (Phase 10, Track B/E; chart widgets Phase
    13): {title, widgets: [{id, title, endpoint, display, field?}]}.
    A widget references an EXISTING API endpoint by path; the generic
    renderer fetches it and shows the response's `total`, the array
    length, or one named numeric field. Phase 13 adds chart displays:
    line/bar read the response's `items` array using the widget's
    `series` fields (+ `labelField` for the axis), gauge reads one
    numeric `field` against `max` and optional `thresholds`
    ({warn, critical} — at/above colours), table renders `items` with
    `columns` ({field, title}). No data definitions live here —
    dashboards are composition, not queries."""
    payload = _require_obj(payload)
    _require_str(payload.get("title"), "title")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    if "audience" in payload and not isinstance(payload["audience"], str):
        _fail("'audience' must be a string")
    _check_roles(payload.get("roles"), "roles")
    widgets = payload.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        _fail("Dashboard config requires a non-empty 'widgets' list")
    seen: set[str] = set()
    for i, widget in enumerate(widgets):
        if not isinstance(widget, dict):
            _fail(f"widgets[{i}] must be an object")
        widget_id = _require_str(widget.get("id"), f"widgets[{i}].id")
        if widget_id in seen:
            _fail(f"Duplicate widget id '{widget_id}'")
        seen.add(widget_id)
        _require_str(widget.get("title"), f"widgets[{i}].title")
        endpoint = _require_str(widget.get("endpoint"), f"widgets[{i}].endpoint")
        if not endpoint.startswith("/") or "://" in endpoint:
            _fail(f"widgets[{i}].endpoint must be an API path starting with '/'")
        display = widget.get("display")
        if display not in DASHBOARD_DISPLAYS:
            _fail(f"widgets[{i}].display must be one of {list(DASHBOARD_DISPLAYS)}")
        if display in ("value", "big-number", "gauge"):
            _require_str(widget.get("field"), f"widgets[{i}].field")
        if "field" in widget and not isinstance(widget["field"], str):
            _fail(f"widgets[{i}].field must be a string")
        if display in ("line", "bar"):
            _validate_widget_series(widget, i)
            _require_str(widget.get("labelField"), f"widgets[{i}].labelField")
        if display == "gauge":
            maximum = widget.get("max")
            if maximum is not None and (isinstance(maximum, bool)
                                        or not isinstance(maximum, (int, float))
                                        or maximum <= 0):
                _fail(f"widgets[{i}].max must be a positive number")
            thresholds = widget.get("thresholds")
            if thresholds is not None:
                if not isinstance(thresholds, dict):
                    _fail(f"widgets[{i}].thresholds must be an object")
                for name in ("warn", "critical"):
                    value = thresholds.get(name)
                    if value is not None and (isinstance(value, bool)
                                              or not isinstance(value, (int, float))):
                        _fail(f"widgets[{i}].thresholds.{name} must be a number")
        if display == "table":
            columns = widget.get("columns")
            if not isinstance(columns, list) or not columns:
                _fail(f"widgets[{i}].columns must be a non-empty list of "
                      "{field, title} for table displays")
            for j, column in enumerate(columns):
                if not isinstance(column, dict):
                    _fail(f"widgets[{i}].columns[{j}] must be an object")
                _require_str(column.get("field"), f"widgets[{i}].columns[{j}].field")
                _require_str(column.get("title"), f"widgets[{i}].columns[{j}].title")
        if "unit" in widget and not isinstance(widget["unit"], str):
            _fail(f"widgets[{i}].unit must be a string")


def validate_regulatory_pack(payload) -> None:
    """Regulatory pack (Phase 10, Track J): {body, jurisdiction,
    parameters?, requirements?, notes?}. The pack parameterises the
    workflows its regulation governs (timers, windows, thresholds) —
    the values are data consumed by Track J/K code, never code
    branches. `requirements` lists the register citations the pack
    covers, so a pack and the requirements register stay linked."""
    payload = _require_obj(payload)
    _require_str(payload.get("body"), "body")
    _require_str(payload.get("jurisdiction"), "jurisdiction")
    parameters = payload.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, dict):
            _fail("'parameters' must be an object")
        for key, value in parameters.items():
            # Phase 16 (WS-F2): a parameter may be a flat list of scalars
            # (e.g. the GCP pack's essentialDocuments, the Part 11 pack's
            # manifestFields) — nested objects and nulls stay refused.
            if isinstance(value, list):
                if any(isinstance(item, (dict, list)) or item is None for item in value):
                    _fail(f"'parameters.{key}' must be a scalar or a flat list of scalars")
            elif isinstance(value, dict) or value is None:
                _fail(f"'parameters.{key}' must be a scalar (string, number or boolean)")
    requirements = payload.get("requirements")
    if requirements is not None:
        if not isinstance(requirements, list) or any(
                not isinstance(r, str) or not r.strip() for r in requirements):
            _fail("'requirements' must be a list of citation strings")
    if "notes" in payload and not isinstance(payload["notes"], str):
        _fail("'notes' must be a string")


# ---------------------------------------------------------------------------
# Phase 13 kinds: report / mapping (Track F)
# ---------------------------------------------------------------------------

# Report sources are SERVER-DEFINED queries (app.reports.REPORT_QUERIES):
# a report config names one — it can never carry SQL or a query of its
# own. This tuple is the registry the validator checks against, exactly
# like RULE_EVENTS for the rules engine.
REPORT_SOURCES = ("census-by-unit", "claims-summary", "credential-expiry",
                  "safety-events", "payments-summary")
REPORT_FORMATS = ("pdf", "csv")
REPORT_PARAM_TYPES = ("string", "date", "number", "choice")


def validate_report(payload) -> None:
    """Report definition (Phase 13, Track F): {title, description?,
    source, parameters?, formats?}. `source` names a server-side
    report query from REPORT_SOURCES; `parameters` declares the run
    form ({name, type, required?, default?, choices?}); `formats`
    limits what a run may produce (default pdf + csv)."""
    payload = _require_obj(payload)
    _require_str(payload.get("title"), "title")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    if payload.get("source") not in REPORT_SOURCES:
        _fail(f"'source' must be one of {list(REPORT_SOURCES)} "
              "(server-defined report queries — a report never carries SQL)")
    formats = payload.get("formats")
    if formats is not None:
        if not isinstance(formats, list) or not formats or any(
                f not in REPORT_FORMATS for f in formats):
            _fail(f"'formats' must be a non-empty list drawn from {list(REPORT_FORMATS)}")
    parameters = payload.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, list):
            _fail("'parameters' must be a list")
        seen: set[str] = set()
        for i, param in enumerate(parameters):
            if not isinstance(param, dict):
                _fail(f"parameters[{i}] must be an object")
            name = _require_str(param.get("name"), f"parameters[{i}].name")
            if name in seen:
                _fail(f"Duplicate parameter name '{name}'")
            seen.add(name)
            if param.get("type") not in REPORT_PARAM_TYPES:
                _fail(f"parameters[{i}].type must be one of {list(REPORT_PARAM_TYPES)}")
            if "required" in param and not isinstance(param["required"], bool):
                _fail(f"parameters[{i}].required must be a boolean")
            if "label" in param and not isinstance(param["label"], str):
                _fail(f"parameters[{i}].label must be a string")
            if param.get("type") == "choice":
                choices = param.get("choices")
                if not isinstance(choices, list) or not choices or any(
                        not isinstance(c, str) or not c for c in choices):
                    _fail(f"parameters[{i}].choices must be a non-empty list "
                          "of strings for a choice parameter")


# Import entity types + the governed transform set (app.reports). A
# mapping profile may only bind these: the entity types are the ones
# with a server-side import schema, and transforms are named, reviewed
# functions — never expressions.
IMPORT_ENTITY_TYPES = ("patients", "appointments", "pharmacy-lots")
MAPPING_TRANSFORMS = ("trim", "upper", "lower", "title",
                      "date-mdY", "date-dMY", "code-map")


def validate_mapping(payload) -> None:
    """Import mapping profile (Phase 13, Track F): {entityType,
    description?, columns: [{source, target, transform?, codeMap?}]}.
    `source` is the CSV header name; `target` is a field of the
    entity's server-side import schema; `transform` (optional) is one
    governed transform; `codeMap` (with transform code-map) maps source
    values to target codes."""
    payload = _require_obj(payload)
    if payload.get("entityType") not in IMPORT_ENTITY_TYPES:
        _fail(f"'entityType' must be one of {list(IMPORT_ENTITY_TYPES)}")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        _fail("Mapping config requires a non-empty 'columns' list")
    seen_targets: set[str] = set()
    for i, column in enumerate(columns):
        if not isinstance(column, dict):
            _fail(f"columns[{i}] must be an object")
        _require_str(column.get("source"), f"columns[{i}].source")
        target = _require_str(column.get("target"), f"columns[{i}].target")
        if target in seen_targets:
            _fail(f"Duplicate mapping target '{target}'")
        seen_targets.add(target)
        transform = column.get("transform")
        if transform is not None and transform not in MAPPING_TRANSFORMS:
            _fail(f"columns[{i}].transform must be one of {list(MAPPING_TRANSFORMS)}")
        code_map = column.get("codeMap")
        if code_map is not None:
            if not isinstance(code_map, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in code_map.items()):
                _fail(f"columns[{i}].codeMap must be an object of string -> string")
        if transform == "code-map" and not code_map:
            _fail(f"columns[{i}] uses transform 'code-map' and requires a 'codeMap'")


# Pipeline step vocabulary + data-quality check types (app.pipelines).
# A pipeline definition binds steps from this governed vocabulary —
# steps are server code, parameterised by the definition, exactly like
# RULE_EVENTS / REPORT_SOURCES. There is no arbitrary code in a
# pipeline definition (the Track B clinical-safety rule, applied to
# the data platform).
PIPELINE_STEP_TYPES = ("extract-table", "terminology-normalise",
                       "dedup-scan", "load-warehouse", "replay-lake",
                       # Platform pass: Rust de-identification step
                       # (services/rust) — params validated below.
                       "deidentify")
PIPELINE_DQ_CHECKS = ("row-count-floor", "null-rate-ceiling",
                      "value-set-membership")
PIPELINE_EXTRACT_TABLES = ("patients", "appointments", "orders",
                           "lab-results", "observations")


def _validate_pipeline_step(step, i: int) -> None:
    if not isinstance(step, dict):
        _fail(f"steps[{i}] must be an object")
    step_type = step.get("type")
    if step_type not in PIPELINE_STEP_TYPES:
        _fail(f"steps[{i}].type must be one of {list(PIPELINE_STEP_TYPES)} "
              "(governed step vocabulary — a pipeline never carries code)")
    params = step.get("params")
    if params is not None and not isinstance(params, dict):
        _fail(f"steps[{i}].params must be an object")
    params = params or {}
    if step_type == "extract-table" and params.get("table") not in PIPELINE_EXTRACT_TABLES:
        _fail(f"steps[{i}].params.table must be one of "
              f"{list(PIPELINE_EXTRACT_TABLES)}")
    if step_type == "replay-lake":
        payload_type = params.get("payloadType", "hl7")
        if payload_type not in ("hl7", "device-reading", "fhir-observation"):
            _fail(f"steps[{i}].params.payloadType must be one of "
                  "['hl7', 'device-reading', 'fhir-observation']")
    if step_type == "deidentify":
        # Platform pass: the Rust de-identification binary
        # (services/rust) invoked as a subprocess by app.pipelines.
        # `command` names the binary (path or PATH-resolvable name);
        # the step fails closed at run time when it is absent.
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            _fail(f"steps[{i}].params.command must be a non-empty string "
                  "(path or PATH name of the de-identification binary)")
        config = params.get("config")
        if config is not None and not isinstance(config, str):
            _fail(f"steps[{i}].params.config must be a string path")
        record_key_field = params.get("recordKeyField")
        if record_key_field is not None and not isinstance(record_key_field, str):
            _fail(f"steps[{i}].params.recordKeyField must be a string")
        timeout = params.get("timeoutSeconds")
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, (int, float))
                                    or timeout <= 0 or timeout > 600):
            _fail(f"steps[{i}].params.timeoutSeconds must be a number "
                  "in (0, 600]")
    if "name" in step and not isinstance(step["name"], str):
        _fail(f"steps[{i}].name must be a string")
    retry = step.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            _fail(f"steps[{i}].retry must be an object")
        attempts = retry.get("maxAttempts")
        if attempts is not None and (isinstance(attempts, bool)
                                     or not isinstance(attempts, int)
                                     or not (1 <= attempts <= 5)):
            _fail(f"steps[{i}].retry.maxAttempts must be an integer in 1..5")
        backoff = retry.get("backoffSeconds")
        if backoff is not None and (isinstance(backoff, bool)
                                    or not isinstance(backoff, (int, float))
                                    or backoff < 0 or backoff > 60):
            _fail(f"steps[{i}].retry.backoffSeconds must be a number in 0..60")
    checks = step.get("checks")
    if checks is not None:
        if not isinstance(checks, list):
            _fail(f"steps[{i}].checks must be a list")
        for j, check in enumerate(checks):
            if not isinstance(check, dict):
                _fail(f"steps[{i}].checks[{j}] must be an object")
            if check.get("type") not in PIPELINE_DQ_CHECKS:
                _fail(f"steps[{i}].checks[{j}].type must be one of "
                      f"{list(PIPELINE_DQ_CHECKS)}")
            if check["type"] == "row-count-floor":
                value = check.get("value")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    _fail(f"steps[{i}].checks[{j}].value must be a "
                          "non-negative integer (row-count floor)")
            if check["type"] == "null-rate-ceiling":
                _require_str(check.get("field"), f"steps[{i}].checks[{j}].field")
                value = check.get("value")
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not (0 <= value <= 1):
                    _fail(f"steps[{i}].checks[{j}].value must be a number "
                          "in 0..1 (null-rate ceiling)")
            if check["type"] == "value-set-membership":
                _require_str(check.get("field"), f"steps[{i}].checks[{j}].field")
                values = check.get("values")
                if not isinstance(values, list) or not values or any(
                        not isinstance(v, str) for v in values):
                    _fail(f"steps[{i}].checks[{j}].values must be a non-empty "
                          "list of strings (the allowed value set)")


def validate_pipeline(payload) -> None:
    """Pipeline definition (Phase 14, Track I): {name, description?,
    source?, steps: [{type, params?, retry?, checks?}], schedule?,
    enabled?}. Steps come from the governed PIPELINE_STEP_TYPES
    vocabulary; `schedule` is a cron string recorded as configuration
    (runs are operator-triggered in this build — no in-process
    scheduler daemon); `enabled: false` refuses runs with 409."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    if "source" in payload and not isinstance(payload["source"], str):
        _fail("'source' must be a string")
    if "schedule" in payload and not isinstance(payload["schedule"], str):
        _fail("'schedule' must be a cron string")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        _fail("'enabled' must be a boolean")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail("Pipeline config requires a non-empty 'steps' list")
    if len(steps) > 12:
        _fail("Pipeline config allows at most 12 steps")
    for i, step in enumerate(steps):
        _validate_pipeline_step(step, i)


def validate_protocol(payload) -> None:
    # Platform pass: the protocol validator lives in app.protocols (the
    # module owns the kind's governance). Imported lazily because
    # app.protocols imports this module — a top-level import would cycle.
    from .protocols import validate_protocol_payload
    validate_protocol_payload(payload)


def validate_report_definition(payload) -> None:
    # Platform pass: validator lives in app.report_engine; lazy import
    # for the same import-cycle reason as validate_protocol above.
    # (Session-aware dataset validation additionally runs inside the
    # report engine's own governance endpoints.)
    from .report_engine import validate_report_definition as _validate
    _validate(payload)


def validate_indicator(payload) -> None:
    # Platform pass: validator lives in app.indicators; lazy import.
    from .indicators import validate_indicator as _validate
    _validate(payload)


def validate_dbplatform(payload) -> None:
    # Platform pass: connection-profile validator (kind "dbplatform")
    # lives in app.dbplatform; lazy import. Refuses any
    # credential-shaped field — profiles reference vault key NAMES.
    from .dbplatform import validate_profile_payload as _validate
    _validate(payload)


def validate_dbplatform_schema(payload) -> None:
    # Platform pass: deployment schema-extension validator (kind
    # "dbplatform-schema") lives in app.dbplatform; lazy import.
    from .dbplatform import validate_schema_extension_payload as _validate
    _validate(payload)


def validate_dbplatform_transform(payload) -> None:
    # Platform pass: transformer/export profile validator (kind
    # "dbplatform-transform") lives in app.dbplatform; lazy import.
    from .dbplatform import validate_transform_profile_payload as _validate
    _validate(payload)


def validate_ai_provider(payload) -> None:
    # Platform pass WS10: AI provider validator (kind "ai-provider")
    # lives in app.ai_providers; lazy import. Refuses any
    # credential-shaped field — providers reference env-var NAMES.
    from .ai_providers import validate_ai_provider_payload as _validate
    _validate(payload)


def validate_endpoint(payload) -> None:
    # Platform pass WS10: endpoint DECLARATION validator (kind
    # "endpoint") lives in app.studio_ops; lazy import. Declarations
    # are born disabled and create no live route.
    from .studio_ops import validate_endpoint_declaration as _validate
    _validate(payload)


def validate_tool(payload) -> None:
    # Platform pass WS10: tool DECLARATION validator (kind "tool")
    # lives in app.studio_ops; lazy import. Executable tool kinds do
    # not exist here (no code execution in the clinical runtime).
    from .studio_ops import validate_tool_declaration as _validate
    _validate(payload)


def validate_agentflow(payload) -> None:
    # Phase 16: supervised agent-workflow validator (kind "agentflow")
    # lives in app.agent_workflows; lazy import. Step types are ONLY
    # agent | review — no step type that writes clinical data exists.
    from .agent_workflows import validate_agentflow_payload as _validate
    _validate(payload)


def validate_knowledge(payload) -> None:
    # Phase 16: governed knowledge-document validator (kind
    # "knowledge") lives in app.rag; lazy import.
    from .rag import validate_knowledge_payload as _validate
    _validate(payload)


def validate_mcp_server(payload) -> None:
    # Phase 16: MCP server registration validator (kind "mcp_server")
    # lives in app.mcp_a2a; lazy import. Refuses any credential-shaped
    # field or credential-embedded URL — servers reference auth held
    # outside config, never secrets in config.
    from .mcp_a2a import validate_mcp_server_payload as _validate
    _validate(payload)


def validate_analytics_view(payload) -> None:
    # Phase 16: governed analytics-view validator (kind
    # "analytics_view") lives in app.analytics; lazy import.
    # Identifiers come only from the dataset allowlist — a view never
    # carries SQL.
    from .analytics import validate_analytics_definition as _validate
    _validate(payload)


VALIDATORS = {
    "form": validate_form,
    "workflow": validate_workflow,
    "terminology": validate_terminology,
    "navigation": validate_navigation,
    "branding": validate_branding,
    "permissions": validate_permissions,
    "cohort": validate_cohort,
    "integration": validate_integration,
    "orderset": validate_orderset,
    "measure": validate_measure,
    "agent": validate_agent,
    "rule": validate_rule,
    "dashboard": validate_dashboard,
    "regulatory_pack": validate_regulatory_pack,
    "report": validate_report,
    "mapping": validate_mapping,
    "pipeline": validate_pipeline,
    "protocol": validate_protocol,
    "report-definition": validate_report_definition,
    "indicator": validate_indicator,
    "dbplatform": validate_dbplatform,
    "dbplatform-schema": validate_dbplatform_schema,
    "dbplatform-transform": validate_dbplatform_transform,
    "ai-provider": validate_ai_provider,
    "endpoint": validate_endpoint,
    "tool": validate_tool,
    "agentflow": validate_agentflow,
    "knowledge": validate_knowledge,
    "mcp_server": validate_mcp_server,
    "analytics_view": validate_analytics_view,
    "inventory": validate_inventory,
}


def validate_payload(kind: str, payload) -> None:
    VALIDATORS[kind](payload)


# ---------------------------------------------------------------------------
# Structural diff (version comparison for the Studio UI / API consumers)
# ---------------------------------------------------------------------------

def diff_payloads(old, new, path: str = "") -> list[dict]:
    """Recursive JSON diff: entries {path, change: added|removed|changed,
    from?, to?}. Lists are compared element-wise by index."""
    out: list[dict] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            p = f"{path}.{key}" if path else key
            if key not in old:
                out.append({"path": p, "change": "added", "to": new[key]})
            elif key not in new:
                out.append({"path": p, "change": "removed", "from": old[key]})
            else:
                out.extend(diff_payloads(old[key], new[key], p))
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(old):
                out.append({"path": p, "change": "added", "to": new[i]})
            elif i >= len(new):
                out.append({"path": p, "change": "removed", "from": old[i]})
            else:
                out.extend(diff_payloads(old[i], new[i], p))
    elif old != new:
        out.append({"path": path, "change": "changed", "from": old, "to": new})
    return out
