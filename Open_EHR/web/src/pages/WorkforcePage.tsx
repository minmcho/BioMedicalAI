import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  EMPTY_PLACEMENT,
  displayForCode,
  formatPlacementView,
  getOrgTree,
  type OrgTree,
  type PlacementCodes,
} from "../api/org";
import type {
  AccountCreated,
  Role,
  StaffCredential,
  StaffMember,
  UserAccount,
  WorkforceConfig,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { MasterDetail } from "../components/MasterDetail";
import { ModuleHeader } from "../components/ModuleHeader";
import { checkPhone, isEmail } from "../components/PatientForms";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { PlacementCascade } from "../components/PlacementCascade";
import { OnboardingPanel } from "./OnboardingPanel";

/** Placement line for a staff row: displays resolved from the org
 *  tree ("Unit · Department · Location · Region"), falling back to
 *  the flat codes the staff record carries; null when unplaced. */
function staffPlacementLabel(tree: OrgTree | null, m: StaffMember): string | null {
  const parts = [m.unit, m.department, m.location, m.region]
    .filter((c): c is string => Boolean(c))
    .map((c) => displayForCode(tree, c) ?? c);
  return parts.length > 0 ? parts.join(" · ") : null;
}

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Wave A (WF-B1): the computed verification block the backend adds
 *  to every credential row (app.people._verification_block) and the
 *  governed `psv` section of the published workforce config. Both
 *  ride payloads the page already loads; the declared web types
 *  predate WF-B1, so they are modelled locally here. */
interface VerificationBlock {
  state: "verified" | "verification-expired" | "unverified" | "exclusion-hit";
  source?: string | null;
  method?: string | null;
  verified_by?: string | null;
  verified_on?: string | null;
  outcome?: string | null;
  evidence?: string | null;
  exclusions?: Record<string, string>;
}

type CredentialRow = StaffCredential & {
  verification?: VerificationBlock;
};

interface PsvSection {
  sources?: Array<{ code: string; display?: string }>;
  exclusionKinds?: Array<{ code: string; display?: string }>;
  reverificationDays?: number;
  requireExclusionKinds?: string[];
}

const VERIFICATION_METHODS = [
  { code: "direct-source-query", display: "Direct source query" },
  { code: "source-document-review", display: "Source document review" },
  { code: "source-attestation-call", display: "Source attestation call" },
];

const VERIFICATION_OUTCOMES = [
  { code: "verified", display: "Verified" },
  { code: "unable-to-verify", display: "Unable to verify" },
  { code: "discrepancy", display: "Discrepancy" },
];

const EXCLUSION_OUTCOMES = [
  { code: "clear", display: "Clear" },
  { code: "hit", display: "Hit" },
  { code: "not-run", display: "Not run" },
];

/** The named verification badge for a credential row: the state is
 *  always text, never colour alone. */
function VerificationBadge({
  verification,
}: {
  verification?: VerificationBlock;
}) {
  const state = verification?.state ?? "unverified";
  if (state === "verified") {
    return (
      <span className="badge">
        Verified
        {verification?.source ? ` · ${verification.source}` : ""}
        {verification?.verified_on ? ` · ${verification.verified_on}` : ""}
      </span>
    );
  }
  if (state === "verification-expired") {
    return <span className="badge badge-warn">PSV expired — re-verify</span>;
  }
  if (state === "exclusion-hit") {
    const hits = Object.entries(verification?.exclusions ?? {})
      .filter(([, outcome]) => outcome === "hit")
      .map(([kind]) => kind);
    return (
      <span className="badge badge-warn">
        Exclusion hit{hits.length > 0 ? `: ${hits.join(", ")}` : ""}
      </span>
    );
  }
  return <span className="badge badge-warn">Unverified</span>;
}

/** Workforce (Phase 13, Track E): the staff directory, the credential
 *  expiry worklist, today's roster and who is on call now. Expiry
 *  windows, shift templates and the alert rule are published Studio
 *  config server-side; the credential scan runs the rules engine over
 *  the alert window and raises renewal Tasks (idempotently). */
export function WorkforcePage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["workforce.work"]?.[role] === true;
  const canManageAccounts = permissions["accounts.manage"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editingStaffId, setEditingStaffId] = useState<string | null>(null);
  // Depth pass (Workstream 1): stacked sections became shared Tabs.
  const [tab, setTab] = useState<string>("directory");

  // Placement filters (Organisation pass): cascading selects over
  // GET /org-hierarchy; each set level becomes a query param on the
  // staff fetch (region/location/department/unit).
  const [placementFilter, setPlacementFilter] =
    useState<PlacementCodes>(EMPTY_PLACEMENT);
  const orgState = useAsync(() => getOrgTree(client), [client]);
  const staff = useAsync(
    () =>
      client.listStaff({
        region: placementFilter.region || undefined,
        location: placementFilter.location || undefined,
        department: placementFilter.department || undefined,
        unit: placementFilter.unit || undefined,
      }),
    [
      client,
      tick,
      placementFilter.region,
      placementFilter.location,
      placementFilter.department,
      placementFilter.unit,
    ],
  );
  const configState = useAsync(() => client.workforceConfig(), [client]);
  const worklist = useAsync(() => client.credentialWorklist(), [client, tick]);
  const roster = useAsync(() => client.listRoster(todayIso()), [client, tick]);
  const onCall = useAsync(() => client.onCallNow(), [client, tick]);
  // WF-B1: the full credential list carries each row's computed
  // verification state. Fetched defensively: a client without the
  // method leaves the tab on its worklist rows (which carry the
  // same verification block) rather than erroring the page.
  const allCredentials = useAsync(async (): Promise<CredentialRow[] | null> => {
    if (typeof client.listCredentials !== "function") return null;
    try {
      return (await client.listCredentials()) as unknown as CredentialRow[];
    } catch {
      return null;
    }
  }, [client, tick]);
  const [verifyCredId, setVerifyCredId] = useState<string | null>(null);

  async function runScan() {
    setBusy(true);
    setNotice(null);
    setActionError(null);
    try {
      const out = await client.scanCredentials();
      setNotice(
        `Credential scan complete: ${out.scanned} credentials scanned, ` +
          `${out.alerted} renewal alert(s) raised (alert window ${out.alert_window_days} days).`,
      );
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Credential scan failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  // UX pass (spec P-1): loading/error states keep the page title.
  if (staff.loading || worklist.loading || roster.loading || onCall.loading) {
    return (
      <div>
        <ModuleHeader title="Workforce" purpose="Staff directory, credentials, rosters, on-call and onboarding." />
        <Loading label="Loading workforce…" />
      </div>
    );
  }
  if (staff.error || worklist.error) {
    return (
      <div>
        <ModuleHeader title="Workforce" purpose="Staff directory, credentials, rosters, on-call and onboarding." />
        <ErrorView error={staff.error ?? worklist.error} />
      </div>
    );
  }

  const wl = worklist.data;
  // WF-B1: the governed PSV lists come from the published workforce
  // config the page already loads — never hard-coded here.
  const psv = (
    configState.data as unknown as { psv?: PsvSection } | null
  )?.psv;
  const psvSources = psv?.sources ?? [];
  const psvKinds = psv?.exclusionKinds ?? [];
  const verifyTarget =
    allCredentials.data?.find((c) => c.credential_id === verifyCredId) ??
    (wl?.items as CredentialRow[] | undefined)?.find(
      (c) => c.credential_id === verifyCredId,
    ) ??
    null;
  return (
    <div>
      <ModuleHeader icon="id-badge" group="People & Teams" title="Workforce" purpose="Staff directory, credentials, rosters, on-call and onboarding." />
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}

      <Tabs
        tabs={[
          { key: "directory", label: "Directory" },
          { key: "credentials", label: "Credentials & licences" },
          { key: "rosters", label: "Rosters" },
          { key: "oncall", label: "On-call" },
          { key: "onboarding", label: "Onboarding" },
          ...(canManageAccounts ? [{ key: "accounts", label: "Accounts" }] : []),
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Workforce sections"
        panels={{
          directory: (
<section aria-label="Staff directory">
        <h3>Staff directory</h3>
        <div className="inline-form" aria-label="Staff placement filters">
          <h4>Filter by placement</h4>
          <PlacementCascade
            tree={orgState.data}
            treeError={orgState.error}
            value={placementFilter}
            onChange={setPlacementFilter}
            labelPrefix="Staff filter"
          />
        </div>
        {/* Phase 15 (workstream F): the directory is the shared
            MasterDetail — selecting a staff row shows the full
            record in the detail pane; the staff edit form lives
            there too (no button nested inside the row button). */}
        {staff.data && staff.data.length > 0 ? (
          <MasterDetail<StaffMember>
            items={staff.data}
            loading={staff.loading}
            getKey={(m) => m.staff_id}
            listLabel="Staff directory"
            emptyText="The staff directory is empty."
            detailEmptyText="Select a staff member to see the record."
            renderListItem={(m) => (
              <>
                <span className="patient-name">{m.full_name}</span>
                <span className="patient-meta">
                  {m.job_role}
                  {m.unit ? ` · ${m.unit}` : ""}
                  {` · ${m.status}`}
                </span>
                <span className="patient-meta">
                  Placement: {staffPlacementLabel(orgState.data, m) ?? "Not placed"}
                </span>
              </>
            )}
            renderDetail={(m) => (
              <div>
                <h4>
                  {m.full_name} ({m.staff_id})
                </h4>
                <p>Role: {m.job_role}</p>
                <p>Unit: {m.unit ?? "—"}</p>
                <p>
                  Placement: {staffPlacementLabel(orgState.data, m) ?? "Not placed"}
                </p>
                <p>Service line: {m.service_line ?? "—"}</p>
                <p>Status: {m.status}</p>
                <p>Credentials: {m.credential_count ?? "—"}</p>
                {m.email && <p>Email: {m.email}</p>}
                {m.phone && <p>Phone: {m.phone}</p>}
                {canWork && editingStaffId !== m.staff_id && (
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => setEditingStaffId(m.staff_id)}
                  >
                    Edit
                  </button>
                )}
                {canWork && editingStaffId === m.staff_id && (
                  <StaffEditForm
                    client={client}
                    member={m}
                    config={configState.data}
                    onSaved={() => {
                      setEditingStaffId(null);
                      setNotice("Staff record updated.");
                      setTick((t) => t + 1);
                    }}
                    onCancel={() => setEditingStaffId(null)}
                  />
                )}
              </div>
            )}
          />
        ) : (
          <Empty
            label={
              placementFilter.region ||
              placementFilter.location ||
              placementFilter.department ||
              placementFilter.unit
                ? "No staff match the selected placement."
                : "The staff directory is empty."
            }
          />
        )}
      </section>
          ),
          credentials: (
<section aria-label="Credential expiry worklist">
        <h3>Credential expiry worklist</h3>
        {wl && (
          <p className="hint">
            Window {wl.worklist_window_days} days (config); {wl.expired} expired,{" "}
            {wl.total} in the worklist. Items marked “alert window” are inside{" "}
            {wl.alert_window_days} days — the scan raises a renewal task for each.
          </p>
        )}
        {canWork && (
          <p>
            <button type="button" onClick={runScan} disabled={busy}>
              Run credential expiry scan
            </button>
          </p>
        )}
        {wl && wl.items.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Staff</th>
                <th scope="col">Credential</th>
                <th scope="col">Expires</th>
                <th scope="col">Days left</th>
                <th scope="col">Status</th>
                <th scope="col">Verification</th>
                {canWork && <th scope="col">PSV</th>}
              </tr>
            </thead>
            <tbody>
              {wl.items.map((c) => (
                <tr key={c.credential_id}>
                  <td>{c.staff_name ?? c.staff_id}</td>
                  <td>{c.type}</td>
                  <td>{c.expires_on}</td>
                  <td className="num">{c.days_to_expiry ?? "—"}</td>
                  <td>
                    {c.status}
                    {c.alert_window ? " · alert window" : ""}
                  </td>
                  <td>
                    <VerificationBadge
                      verification={(c as CredentialRow).verification}
                    />
                  </td>
                  {canWork && (
                    <td>
                      <button
                        type="button"
                        className="linklike"
                        onClick={() => setVerifyCredId(c.credential_id)}
                      >
                        Record verification
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty label="No credentials expiring inside the worklist window." />
        )}

        {/* WF-B1: every credential with its computed PSV state —
            a credential is self-asserted until a primary-source
            verification is recorded for it. */}
        <h4>All credentials — primary-source verification</h4>
        {allCredentials.data && allCredentials.data.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Staff</th>
                <th scope="col">Credential</th>
                <th scope="col">Number</th>
                <th scope="col">Expires</th>
                <th scope="col">Status</th>
                <th scope="col">Verification</th>
                {canWork && <th scope="col">PSV</th>}
              </tr>
            </thead>
            <tbody>
              {allCredentials.data.map((c) => (
                <tr key={c.credential_id}>
                  <td>{c.staff_name ?? c.staff_id}</td>
                  <td>{c.type}</td>
                  <td>{c.number ?? "—"}</td>
                  <td>{c.expires_on}</td>
                  <td>{c.status}</td>
                  <td>
                    <VerificationBadge verification={c.verification} />
                  </td>
                  {canWork && (
                    <td>
                      <button
                        type="button"
                        className="linklike"
                        onClick={() => setVerifyCredId(c.credential_id)}
                      >
                        Record verification
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          allCredentials.data && (
            <Empty label="No credentials on file." />
          )
        )}
        {canWork && verifyTarget && (
          <RecordVerificationForm
            client={client}
            credential={verifyTarget}
            sources={psvSources}
            onSaved={() => {
              setVerifyCredId(null);
              setNotice(
                `Verification recorded for ${verifyTarget.credential_id}.`,
              );
              setTick((t) => t + 1);
            }}
            onCancel={() => setVerifyCredId(null)}
          />
        )}
        {canWork && (
          <ExclusionCheckForm
            client={client}
            staffList={staff.data ?? []}
            kinds={psvKinds}
            onSaved={(staffId) => {
              setNotice(`Exclusion check recorded for ${staffId}.`);
              setTick((t) => t + 1);
            }}
          />
        )}
      </section>
          ),
          rosters: (
<section aria-label="Roster today">
        <h3>Roster — today</h3>
        {roster.data && roster.data.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Staff</th>
                <th scope="col">Unit</th>
                <th scope="col">Shift</th>
                <th scope="col">Starts</th>
                <th scope="col">Ends</th>
              </tr>
            </thead>
            <tbody>
              {roster.data.map((s) => (
                <tr key={s.shift_id}>
                  <td>{s.staff_name ?? s.staff_id}</td>
                  <td>{s.unit}</td>
                  <td>{s.template}</td>
                  <td>{s.starts_at}</td>
                  <td>{s.ends_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty label="No shifts rostered for today." />
        )}
      </section>
          ),
          oncall: (
<section aria-label="On call now">
        <h3>On call now</h3>
        {onCall.data && onCall.data.services.length > 0 ? (
          <div className="dashboard-grid">
            {onCall.data.services.map((s) => (
              <div key={s.service_line} className="dashboard-widget">
                <div className="dashboard-widget-title">{s.service_line}</div>
                <div>
                  {s.primary ? (
                    <>
                      <strong>{s.primary.staff_name}</strong>
                      {s.backup && (
                        <div className="hint">Backup: {s.backup.staff_name}</div>
                      )}
                    </>
                  ) : (
                    <StatusBadge tone="warn">uncovered</StatusBadge>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Empty label="No on-call schedule published." />
        )}
        {onCall.data && (
          <p className="hint">
            {onCall.data.covered} of {onCall.data.total} service lines covered now.
          </p>
        )}
      </section>
          ),
          onboarding: <OnboardingPanel client={client} role={role} />,
          accounts: canManageAccounts ? (
            <AccountsSection
              client={client}
              config={configState.data}
              staffList={staff.data ?? []}
            />
          ) : (
            <Empty label="Accounts are managed by administrators." />
          ),
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Front Door pass: staff edit + user accounts
// ---------------------------------------------------------------------------

const STAFF_STATUSES = ["active", "leave", "inactive"] as const;
const SIGN_IN_ROLES: Role[] = ["admin", "configurator", "clinician", "viewer", "patient"];

function contactErrors(email: string, phone: string): Record<string, string> {
  const errors: Record<string, string> = {};
  if (email.trim() && !isEmail(email.trim())) {
    errors.email = "That does not look like an email address (name@example.org).";
  }
  if (phone.trim()) {
    const msg = checkPhone(phone.trim());
    if (msg) errors.phone = msg;
  }
  return errors;
}

/** A config-sourced select. When its source list is empty the
 *  control is disabled and says why — never a blank mystery list. */
function ConfigSelect({
  label,
  value,
  options,
  emptyReason,
  allowNone,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ code: string; display?: string }>;
  emptyReason: string;
  allowNone?: string;
  onChange: (value: string) => void;
}) {
  const disabled = options.length === 0;
  return (
    <label className="field">
      {label}
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {allowNone !== undefined && <option value="">{allowNone}</option>}
        {options.map((o) => (
          <option key={o.code} value={o.code}>
            {o.display ?? o.code}
          </option>
        ))}
      </select>
      {disabled && <span className="field-error">{emptyReason}</span>}
    </label>
  );
}

/** WF-B1: record a primary-source verification for one credential.
 *  The source select is populated FROM the published workforce
 *  config's psv section; when that list is unavailable the select is
 *  disabled with the reason shown (ConfigSelect) — never a dead
 *  control. Client validation mirrors the server's: evidence is
 *  required for a "verified" outcome and the date cannot be future;
 *  server refusals surface as a form error. */
function RecordVerificationForm({
  client,
  credential,
  sources,
  onSaved,
  onCancel,
}: {
  client: ApiClient;
  credential: CredentialRow;
  sources: Array<{ code: string; display?: string }>;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    source: sources[0]?.code ?? "",
    method: "direct-source-query",
    outcome: "verified",
    verified_on: todayIso(),
    evidence: "",
    note: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function save() {
    const errs: Record<string, string> = {};
    if (!form.source) {
      errs.source =
        "Choose the primary source the credential was verified against.";
    }
    if (!form.verified_on) {
      errs.verified_on = "The verification date is required.";
    } else if (form.verified_on > todayIso()) {
      errs.verified_on = "The verification date cannot be in the future.";
    }
    if (form.outcome === "verified" && !form.evidence.trim()) {
      errs.evidence =
        "Evidence (document id / confirmation number) is required " +
        "for a verified outcome.";
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    try {
      await client.request(
        `/people/credentials/${credential.credential_id}/verifications`,
        {
          method: "POST",
          body: JSON.stringify({
            source_code: form.source,
            method: form.method,
            outcome: form.outcome,
            verified_on: form.verified_on,
            evidence: form.evidence.trim() || undefined,
            note: form.note.trim() || undefined,
          }),
        },
      );
      onSaved();
    } catch (err) {
      setFormError(`Recording verification failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label={`Record verification for ${credential.credential_id}`}
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h4>
        Record verification — {credential.type} ({credential.credential_id})
        for {credential.staff_name ?? credential.staff_id}
      </h4>
      <ConfigSelect
        label="PSV source *"
        value={form.source}
        options={sources}
        emptyReason="No PSV sources are available: the published workforce config could not be loaded, or its psv section names no sources."
        onChange={(v) => patch({ source: v })}
      />
      {errors.source && (
        <span className="field-error" role="alert">{errors.source}</span>
      )}
      <ConfigSelect
        label="Method *"
        value={form.method}
        options={VERIFICATION_METHODS}
        emptyReason="No verification methods are available."
        onChange={(v) => patch({ method: v })}
      />
      <ConfigSelect
        label="Outcome *"
        value={form.outcome}
        options={VERIFICATION_OUTCOMES}
        emptyReason="No verification outcomes are available."
        onChange={(v) => patch({ outcome: v })}
      />
      <label className="field">
        Verified on *
        <input
          aria-label="Verification date"
          type="date"
          value={form.verified_on}
          onChange={(e) => patch({ verified_on: e.target.value })}
        />
        {errors.verified_on && (
          <span className="field-error" role="alert">{errors.verified_on}</span>
        )}
      </label>
      <label className="field">
        Evidence {form.outcome === "verified" ? "*" : ""}
        <input
          aria-label="Verification evidence"
          value={form.evidence}
          onChange={(e) => patch({ evidence: e.target.value })}
          placeholder="Document id / confirmation number"
        />
        {errors.evidence && (
          <span className="field-error" role="alert">{errors.evidence}</span>
        )}
      </label>
      <label className="field">
        Note
        <input
          aria-label="Verification note"
          value={form.note}
          onChange={(e) => patch({ note: e.target.value })}
        />
      </label>
      <button type="submit" disabled={busy}>
        Record verification
      </button>
      <button type="button" className="linklike" onClick={onCancel}>
        Cancel
      </button>
      {formError && (
        <p className="error-note" role="alert">
          {formError}
        </p>
      )}
    </form>
  );
}

/** WF-B1: record an exclusion check for one staff member. The kind
 *  select is populated FROM the published workforce config's psv
 *  section; a "hit" is recorded as data — its consequence surfaces
 *  in the credential's verification state and in privilege checks. */
function ExclusionCheckForm({
  client,
  staffList,
  kinds,
  onSaved,
}: {
  client: ApiClient;
  staffList: StaffMember[];
  kinds: Array<{ code: string; display?: string }>;
  onSaved: (staffId: string) => void;
}) {
  const [form, setForm] = useState({
    staff_id: "",
    kind: "",
    outcome: "clear",
    checked_on: todayIso(),
    evidence: "",
    note: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function save() {
    const errs: Record<string, string> = {};
    if (!form.staff_id) errs.staff_id = "Choose the staff member checked.";
    if (!form.kind) {
      errs.kind = "Choose the exclusion registry that was checked.";
    }
    if (!form.checked_on) {
      errs.checked_on = "The check date is required.";
    } else if (form.checked_on > todayIso()) {
      errs.checked_on = "The check date cannot be in the future.";
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    try {
      await client.request(`/people/staff/${form.staff_id}/exclusion-checks`, {
        method: "POST",
        body: JSON.stringify({
          kind: form.kind,
          outcome: form.outcome,
          checked_on: form.checked_on,
          evidence: form.evidence.trim() || undefined,
          note: form.note.trim() || undefined,
        }),
      });
      onSaved(form.staff_id);
      setForm({
        staff_id: "", kind: "", outcome: "clear",
        checked_on: todayIso(), evidence: "", note: "",
      });
    } catch (err) {
      setFormError(`Recording exclusion check failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label="Record exclusion check"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h4>Record exclusion check</h4>
      <ConfigSelect
        label="Staff member *"
        value={form.staff_id}
        options={staffList.map((s) => ({
          code: s.staff_id,
          display: `${s.full_name} (${s.staff_id})`,
        }))}
        emptyReason="The staff directory is empty — add the person there first."
        allowNone="(select staff member)"
        onChange={(v) => patch({ staff_id: v })}
      />
      {errors.staff_id && (
        <span className="field-error" role="alert">{errors.staff_id}</span>
      )}
      <ConfigSelect
        label="Exclusion kind *"
        value={form.kind}
        options={kinds}
        emptyReason="No exclusion kinds are available: the published workforce config could not be loaded, or its psv section names no exclusion kinds."
        allowNone="(select kind)"
        onChange={(v) => patch({ kind: v })}
      />
      {errors.kind && (
        <span className="field-error" role="alert">{errors.kind}</span>
      )}
      <ConfigSelect
        label="Outcome *"
        value={form.outcome}
        options={EXCLUSION_OUTCOMES}
        emptyReason="No exclusion outcomes are available."
        onChange={(v) => patch({ outcome: v })}
      />
      <label className="field">
        Checked on *
        <input
          aria-label="Exclusion check date"
          type="date"
          value={form.checked_on}
          onChange={(e) => patch({ checked_on: e.target.value })}
        />
        {errors.checked_on && (
          <span className="field-error" role="alert">{errors.checked_on}</span>
        )}
      </label>
      <label className="field">
        Evidence
        <input
          aria-label="Exclusion check evidence"
          value={form.evidence}
          onChange={(e) => patch({ evidence: e.target.value })}
          placeholder="Query reference / confirmation number"
        />
      </label>
      <label className="field">
        Note
        <input
          aria-label="Exclusion check note"
          value={form.note}
          onChange={(e) => patch({ note: e.target.value })}
        />
      </label>
      <button type="submit" disabled={busy}>
        Record exclusion check
      </button>
      {formError && (
        <p className="error-note" role="alert">
          {formError}
        </p>
      )}
    </form>
  );
}

function StaffEditForm({
  client,
  member,
  config,
  onSaved,
  onCancel,
}: {
  client: ApiClient;
  member: StaffMember;
  config: WorkforceConfig | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    full_name: member.full_name,
    job_role: member.job_role,
    unit: member.unit ?? "",
    service_line: member.service_line ?? "",
    status: member.status,
    email: member.email ?? "",
    phone: member.phone ?? "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function save() {
    const errs = contactErrors(form.email, form.phone);
    if (!form.full_name.trim()) errs.full_name = "Full name is required.";
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    try {
      await client.updateStaff(member.staff_id, {
        full_name: form.full_name.trim(),
        job_role: form.job_role,
        unit: form.unit || null,
        service_line: form.service_line || null,
        status: form.status,
        email: form.email.trim() || null,
        phone: form.phone.trim() || null,
      });
      onSaved();
    } catch (err) {
      setFormError(`Save failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label={`Edit staff ${member.staff_id}`}
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h4>Edit {member.full_name} ({member.staff_id})</h4>
      <label className="field">
        Full name *
        <input
          aria-label="Staff full name"
          value={form.full_name}
          onChange={(e) => patch({ full_name: e.target.value })}
        />
        {errors.full_name && (
          <span className="field-error" role="alert">{errors.full_name}</span>
        )}
      </label>
      <ConfigSelect
        label="Job role *"
        value={form.job_role}
        options={(config?.concepts ?? []).map((c) => ({
          code: c.code,
          display: c.display,
        }))}
        emptyReason="No job roles in the published workforce config."
        onChange={(v) => patch({ job_role: v })}
      />
      <ConfigSelect
        label="Unit"
        value={form.unit}
        options={config?.units ?? []}
        emptyReason="No units in the published workforce config."
        allowNone="(no unit)"
        onChange={(v) => patch({ unit: v })}
      />
      <ConfigSelect
        label="Service line"
        value={form.service_line}
        options={config?.serviceLines ?? []}
        emptyReason="No service lines in the published workforce config."
        allowNone="(none)"
        onChange={(v) => patch({ service_line: v })}
      />
      <label className="field">
        Status *
        <select
          aria-label="Staff status"
          value={form.status}
          onChange={(e) => patch({ status: e.target.value })}
        >
          {STAFF_STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        Email
        <input
          aria-label="Staff email"
          type="email"
          value={form.email}
          onChange={(e) => patch({ email: e.target.value })}
        />
        {errors.email && (
          <span className="field-error" role="alert">{errors.email}</span>
        )}
      </label>
      <label className="field">
        Phone
        <input
          aria-label="Staff phone"
          type="tel"
          value={form.phone}
          onChange={(e) => patch({ phone: e.target.value })}
        />
        {errors.phone && (
          <span className="field-error" role="alert">{errors.phone}</span>
        )}
      </label>
      <button type="submit" disabled={busy}>
        Save staff record
      </button>
      <button type="button" className="linklike" onClick={onCancel}>
        Cancel
      </button>
      {formError && (
        <p className="error-note" role="alert">
          {formError}
        </p>
      )}
    </form>
  );
}

/** User accounts (admin only, accounts.manage). Accounts govern
 *  identity, role, unit and status — passwords are NOT stored here:
 *  production sign-in credentials live in the identity provider
 *  (OIDC/Keycloak). In dev mode a one-time sign-in token is issued at
 *  creation and shown exactly once. Lifecycle state is labelled in
 *  text (Active / Disabled), never colour alone. */
function AccountsSection({
  client,
  config,
  staffList,
}: {
  client: ApiClient;
  config: WorkforceConfig | null;
  staffList: StaffMember[];
}) {
  const [tick, setTick] = useState(0);
  const accounts = useAsync(() => client.listAccounts(), [client, tick]);
  const [form, setForm] = useState({
    username: "",
    full_name: "",
    role: "viewer" as Role,
    unit: "",
    service_line: "",
    email: "",
    phone: "",
    staff_id: "",
    patient_ref: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [created, setCreated] = useState<AccountCreated | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function create() {
    const errs = contactErrors(form.email, form.phone);
    if (!form.username.trim()) {
      errs.username = "Username is required.";
    } else if (!/^[A-Za-z0-9._-]+$/.test(form.username.trim())) {
      errs.username =
        "One word: letters, digits, dot, underscore or dash — no spaces.";
    }
    if (!form.full_name.trim()) errs.full_name = "Full name is required.";
    if (form.role === "patient" && !form.patient_ref.trim()) {
      errs.patient_ref =
        "A patient account needs its chart reference (e.g. Patient/pat-1).";
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    try {
      const out = await client.createAccount({
        username: form.username.trim(),
        full_name: form.full_name.trim(),
        role: form.role,
        unit: form.unit || null,
        service_line: form.service_line || null,
        email: form.email.trim() || null,
        phone: form.phone.trim() || null,
        staff_id: form.staff_id || null,
        patient_ref: form.patient_ref.trim() || null,
      });
      setCreated(out);
      setForm({
        username: "", full_name: "", role: "viewer", unit: "",
        service_line: "", email: "", phone: "", staff_id: "",
        patient_ref: "",
      });
      setTick((t) => t + 1);
    } catch (err) {
      setFormError(`Account creation failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function setStatus(account: UserAccount) {
    setBusy(true);
    setFormError(null);
    try {
      if (account.status === "active") {
        await client.disableAccount(account.account_id);
      } else {
        await client.enableAccount(account.account_id);
      }
      setTick((t) => t + 1);
    } catch (err) {
      setFormError(`Status change failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="User accounts">
      <h3>User accounts</h3>
      <p className="hint">
        Accounts govern identity, sign-in role, unit assignment and
        status — and every create, update, disable and enable is
        audit-chained. Passwords are not stored here: production
        sign-in credentials live in the identity provider
        (OIDC/Keycloak). Disabling an account stops its sign-in
        immediately.
      </p>
      {accounts.loading && <Loading label="Loading accounts…" />}
      {accounts.error != null && <ErrorView error={accounts.error} />}
      {accounts.data && accounts.data.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Username</th>
              <th scope="col">Name</th>
              <th scope="col">Role</th>
              <th scope="col">Unit</th>
              <th scope="col">Placement</th>
              <th scope="col">Staff link</th>
              <th scope="col">Status</th>
              <th scope="col">Lifecycle</th>
            </tr>
          </thead>
          <tbody>
            {accounts.data.map((a) => (
              <tr key={a.account_id}>
                <td>{a.username}</td>
                <td>{a.full_name}</td>
                <td>{a.role}</td>
                <td>{a.unit ?? "—"}</td>
                <td>{formatPlacementView(a.placement) || "Not placed"}</td>
                <td>{a.staff_id ?? "—"}</td>
                <td>
                  {a.status === "active" ? (
                    <span className="badge">Active</span>
                  ) : (
                    <span className="badge badge-warn">Disabled</span>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    className="linklike"
                    disabled={busy}
                    onClick={() => void setStatus(a)}
                  >
                    {a.status === "active" ? "Disable" : "Enable"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        accounts.data && <Empty label="No accounts yet." />
      )}

      <form
      noValidate
      className="inline-form"
      aria-label="Create account"
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <h4>Register a new account</h4>
        <label className="field">
          Username *
          <input
            aria-label="Account username"
            value={form.username}
            onChange={(e) => patch({ username: e.target.value })}
            placeholder="e.g. j.osei"
          />
          {errors.username && (
            <span className="field-error" role="alert">{errors.username}</span>
          )}
        </label>
        <label className="field">
          Full name *
          <input
            aria-label="Account full name"
            value={form.full_name}
            onChange={(e) => patch({ full_name: e.target.value })}
          />
          {errors.full_name && (
            <span className="field-error" role="alert">{errors.full_name}</span>
          )}
        </label>
        <label className="field">
          Sign-in role *
          <select
            aria-label="Account sign-in role"
            value={form.role}
            onChange={(e) => patch({ role: e.target.value as Role })}
          >
            {SIGN_IN_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <ConfigSelect
          label="Unit"
          value={form.unit}
          options={config?.units ?? []}
          emptyReason="No units in the published workforce config."
          allowNone="(no unit)"
          onChange={(v) => patch({ unit: v })}
        />
        <ConfigSelect
          label="Service line"
          value={form.service_line}
          options={config?.serviceLines ?? []}
          emptyReason="No service lines in the published workforce config."
          allowNone="(none)"
          onChange={(v) => patch({ service_line: v })}
        />
        <label className="field">
          Email
          <input
            aria-label="Account email"
            type="email"
            value={form.email}
            onChange={(e) => patch({ email: e.target.value })}
          />
          {errors.email && (
            <span className="field-error" role="alert">{errors.email}</span>
          )}
        </label>
        <label className="field">
          Phone
          <input
            aria-label="Account phone"
            type="tel"
            value={form.phone}
            onChange={(e) => patch({ phone: e.target.value })}
          />
          {errors.phone && (
            <span className="field-error" role="alert">{errors.phone}</span>
          )}
        </label>
        <ConfigSelect
          label="Linked staff entry"
          value={form.staff_id}
          options={staffList.map((s) => ({
            code: s.staff_id,
            display: `${s.full_name} (${s.staff_id})`,
          }))}
          emptyReason="The staff directory is empty — add the person there first."
          allowNone="(no link)"
          onChange={(v) => patch({ staff_id: v })}
        />
        {form.role === "patient" && (
          <label className="field">
            Patient chart reference *
            <input
              aria-label="Account patient reference"
              value={form.patient_ref}
              onChange={(e) => patch({ patient_ref: e.target.value })}
              placeholder="Patient/pat-1"
            />
            {errors.patient_ref && (
              <span className="field-error" role="alert">
                {errors.patient_ref}
              </span>
            )}
          </label>
        )}
        <button type="submit" disabled={busy}>
          Create account
        </button>
        {formError && (
          <p className="error-note" role="alert">
            {formError}
          </p>
        )}
      </form>
      {created && (
        <div className="state state-empty" role="status">
          <p>
            Account <strong>{created.username}</strong> created — status{" "}
            {created.status}.
          </p>
          {created.dev_token ? (
            <p>
              Dev-mode sign-in token (shown <strong>once</strong> — store it
              now): <code>{created.dev_token}</code>
            </p>
          ) : (
            <p className="hint">
              No dev token issued: sign-in is through the identity provider.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
