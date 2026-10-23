import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  EMPTY_PLACEMENT,
  getOrgTree,
  type PlacementCodes,
} from "../api/org";
import type { Role } from "../api/types";
import * as teamsApi from "../api/teams";
import type { OnboardingDetail, OnboardingListItem } from "../api/teams";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { MasterDetail } from "../components/MasterDetail";
import { PlacementCascade } from "../components/PlacementCascade";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Stepper } from "../components/Stepper";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function stageTone(stage?: string): StatusTone {
  const s = (stage ?? "").toLowerCase();
  if (s.includes("complete") || s.includes("active") || s.includes("done")) return "ok";
  if (s.includes("terminat") || s.includes("fail")) return "bad";
  if (s.includes("block") || s.includes("pending")) return "warn";
  return "info";
}

function onboardingId(item: OnboardingListItem): string {
  return item.record?.onboarding_id ?? "";
}

function onboardingStage(item: OnboardingListItem): string {
  return item.record?.stage ?? "unknown";
}

/** Onboarding workflow (Workforce, Teams & Panels pass): HR record →
 *  credentialing / privileging → linked login account → orientation.
 *  Rendered as the Workforce page's Onboarding tab. Work actions
 *  follow workforce.onboard and are enforced again server-side;
 *  blockers are always listed in text, the Advance button is
 *  disabled WITH its reason while blockers exist, and a one-time
 *  dev token from a provisioning advance is shown exactly once with
 *  a copy-now warning. Server {detail} errors surface inline. */
export function OnboardingPanel({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canOnboard = permissions["workforce.onboard"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const listState = useAsync(() => teamsApi.listOnboarding(client), [client, tick]);
  const configState = useAsync(() => teamsApi.getTeamsConfig(client), [client]);

  const items = listState.data?.items ?? [];
  const effectiveId =
    selectedId && items.some((i) => onboardingId(i) === selectedId)
      ? selectedId
      : (items.length > 0 ? onboardingId(items[0]) : null);

  // Only the FIRST load blanks the panel: a refetch after an action
  // (advance / complete / terminate) must keep the detail mounted, or
  // the one-time dev token held in its state would be discarded
  // before the user can copy it.
  if (listState.loading && !listState.data) return <Loading label="Loading onboarding…" />;
  if (listState.error && !listState.data) return <ErrorView error={listState.error} />;

  const jobRoles = configState.data?.jobCatalogue.roles ?? [];

  return (
    <section aria-label="Onboarding">
      <h3>Onboarding</h3>
      <p className="hint">
        Joining workflow: HR record, credentialing and privileging, linked
        login account, then orientation. A record cannot advance while
        blockers remain — they are listed on its detail.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}
      {canOnboard && (
        <CreateOnboardingForm
          client={client}
          jobRoles={jobRoles}
          configError={configState.error}
          onCreated={() => {
            setNotice("Onboarding started.");
            setTick((t) => t + 1);
          }}
          onError={setActionError}
        />
      )}
      {items.length === 0 ? (
        <Empty label="No onboarding records yet." />
      ) : (
        <MasterDetail<OnboardingListItem>
          items={items}
          getKey={(i) => onboardingId(i)}
          listLabel="Onboarding records"
          emptyText="No onboarding records yet."
          detailEmptyText="Select an onboarding record to see its detail."
          selectedKey={effectiveId}
          onSelect={setSelectedId}
          renderListItem={(i) => (
            <>
              <span className="patient-name">{i.staff_name ?? i.record?.staff_id ?? onboardingId(i)}</span>
              <span className="patient-meta">
                {i.job_role ?? ""} · {(i.blockers_count ?? 0)} blocker(s)
              </span>{" "}
              <StatusBadge tone={stageTone(onboardingStage(i))}>{onboardingStage(i)}</StatusBadge>
            </>
          )}
          renderDetail={(i) => (
            <OnboardingDetailView
              key={onboardingId(i)}
              client={client}
              onboardingId={onboardingId(i)}
              canOnboard={canOnboard}
              onChanged={(msg) => {
                setNotice(msg);
                setTick((t) => t + 1);
              }}
              onError={setActionError}
            />
          )}
        />
      )}
    </section>
  );
}

function CreateOnboardingForm({
  client,
  jobRoles,
  configError,
  onCreated,
  onError,
}: {
  client: ApiClient;
  jobRoles: teamsApi.JobCatalogueRole[];
  configError: unknown;
  onCreated: () => void;
  onError: (msg: string | null) => void;
}) {
  const [form, setForm] = useState({
    full_name: "", job_role: "", employee_id: "", service_line: "", email: "",
  });
  const [placement, setPlacement] = useState<PlacementCodes>(EMPTY_PLACEMENT);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  // Placement (Organisation pass): region → location → department →
  // unit from GET /org-hierarchy; all four are required by the
  // server (a 422 names any missing or broken level).
  const orgState = useAsync(() => getOrgTree(client), [client]);
  const placementComplete = Boolean(
    placement.region && placement.location && placement.department && placement.unit,
  );

  const selectedRole = jobRoles.find((r) => r.jobRole === form.job_role);

  async function submit() {
    const errs: Record<string, string> = {};
    if (!form.full_name.trim()) errs.full_name = "Full name is required.";
    if (!form.job_role) errs.job_role = "Job role is required.";
    if (!placementComplete) {
      errs.placement =
        "Placement is required: choose a region, location, department and unit.";
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    onError(null);
    try {
      await teamsApi.createOnboarding(client, {
        full_name: form.full_name.trim(),
        job_role: form.job_role,
        region: placement.region,
        location: placement.location,
        department: placement.department,
        unit: placement.unit,
        ...(form.employee_id.trim() ? { employee_id: form.employee_id.trim() } : {}),
        ...(form.service_line.trim() ? { service_line: form.service_line.trim() } : {}),
        ...(form.email.trim() ? { email: form.email.trim() } : {}),
      });
      setForm({ full_name: "", job_role: "", employee_id: "", service_line: "", email: "" });
      setPlacement(EMPTY_PLACEMENT);
      onCreated();
    } catch (err) {
      onError(`Start onboarding failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label="Start onboarding"
      onSubmit={(e) => { e.preventDefault(); void submit(); }}
    >
      <h4>Start onboarding</h4>
      <label className="field">
        Full name *
        <input aria-label="Onboarding full name" value={form.full_name} onChange={(e) => patch({ full_name: e.target.value })} />
        {errors.full_name && <span className="field-error" role="alert">{errors.full_name}</span>}
      </label>
      <label className="field">
        Job role *
        <select
          aria-label="Onboarding job role"
          value={form.job_role}
          disabled={jobRoles.length === 0}
          onChange={(e) => patch({ job_role: e.target.value })}
        >
          <option value="">Select a job role</option>
          {jobRoles.map((r) => (
            <option key={r.jobRole} value={r.jobRole}>{r.display ?? r.jobRole}</option>
          ))}
        </select>
        {jobRoles.length === 0 && (
          <span className="field-error">
            {configError
              ? `Job catalogue unavailable: ${errorText(configError)}`
              : "No roles in the published job catalogue."}
          </span>
        )}
        {errors.job_role && <span className="field-error" role="alert">{errors.job_role}</span>}
      </label>
      {selectedRole && selectedRole.requiredCredentials && selectedRole.requiredCredentials.length > 0 && (
        <p className="hint">
          Required credentials for {selectedRole.display ?? selectedRole.jobRole}:{" "}
          {selectedRole.requiredCredentials.join(", ")}.
        </p>
      )}
      <label className="field">
        Employee ID
        <input aria-label="Onboarding employee ID" value={form.employee_id} onChange={(e) => patch({ employee_id: e.target.value })} />
      </label>
      <PlacementCascade
        tree={orgState.data}
        treeError={orgState.error}
        value={placement}
        onChange={setPlacement}
        labelPrefix="Onboarding"
        required
      />
      {errors.placement && <span className="field-error" role="alert">{errors.placement}</span>}
      <label className="field">
        Service line
        <input aria-label="Onboarding service line" value={form.service_line} onChange={(e) => patch({ service_line: e.target.value })} />
      </label>
      <label className="field">
        Email
        <input aria-label="Onboarding email" type="email" value={form.email} onChange={(e) => patch({ email: e.target.value })} />
      </label>
      <button type="submit" disabled={busy || !placementComplete}>Start onboarding</button>
      {!placementComplete && (
        <span className="field-error">
          Select a region, location, department and unit first — placement
          is required to start onboarding.
        </span>
      )}
    </form>
  );
}

function OnboardingDetailView({
  client,
  onboardingId,
  canOnboard,
  onChanged,
  onError,
}: {
  client: ApiClient;
  onboardingId: string;
  canOnboard: boolean;
  onChanged: (msg: string) => void;
  onError: (msg: string | null) => void;
}) {
  const [localTick, setLocalTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [devToken, setDevToken] = useState<string | null>(null);
  const [credForm, setCredForm] = useState({ type: "", number: "", issued_on: "", expires_on: "" });
  const [credErrors, setCredErrors] = useState<Record<string, string>>({});
  const [terminateReason, setTerminateReason] = useState("");
  const [terminateError, setTerminateError] = useState<string | null>(null);

  const detailState = useAsync(
    () => teamsApi.getOnboarding(client, onboardingId),
    [client, onboardingId, localTick],
  );

  const refresh = (msg: string) => {
    setLocalTick((t) => t + 1);
    onChanged(msg);
  };

  async function advance() {
    setBusy(true);
    onError(null);
    setDevToken(null);
    try {
      const out: OnboardingDetail = await teamsApi.advanceOnboarding(client, onboardingId);
      if (out.dev_token) setDevToken(out.dev_token);
      refresh(`Onboarding advanced to ${out.record?.stage ?? "the next stage"}.`);
    } catch (err) {
      onError(`Advance failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function addCredential() {
    const errs: Record<string, string> = {};
    if (!credForm.type.trim()) errs.type = "Credential type is required.";
    if (!credForm.number.trim()) errs.number = "Credential number is required.";
    setCredErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    onError(null);
    try {
      await teamsApi.addOnboardingCredential(client, onboardingId, {
        type: credForm.type.trim(),
        number: credForm.number.trim(),
        ...(credForm.issued_on ? { issued_on: credForm.issued_on } : {}),
        ...(credForm.expires_on ? { expires_on: credForm.expires_on } : {}),
      });
      setCredForm({ type: "", number: "", issued_on: "", expires_on: "" });
      refresh("Credential recorded.");
    } catch (err) {
      onError(`Add credential failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function completeItem(itemCode: string) {
    setBusy(true);
    onError(null);
    try {
      await teamsApi.completeOrientationItem(client, onboardingId, itemCode);
      refresh("Orientation item completed.");
    } catch (err) {
      onError(`Complete orientation item failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function terminate() {
    if (!terminateReason.trim()) {
      setTerminateError("Terminating onboarding needs a reason.");
      return;
    }
    setTerminateError(null);
    setBusy(true);
    onError(null);
    try {
      await teamsApi.terminateOnboarding(client, onboardingId, terminateReason.trim());
      setTerminateReason("");
      refresh("Onboarding terminated.");
    } catch (err) {
      onError(`Terminate failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  if (detailState.loading) return <Loading label="Loading onboarding detail…" />;
  if (detailState.error) return <ErrorView error={detailState.error} />;
  const d = detailState.data;
  if (!d) return <Empty label="No onboarding detail." />;

  const stages = d.stages ?? [];
  const currentStage = d.record?.stage ?? "";
  const currentIdx = Math.max(0, stages.indexOf(currentStage));
  const blockers = d.blockers ?? [];
  const hasBlockers = blockers.length > 0;

  return (
    <div>
      <h4>
        {d.staff?.full_name ?? d.record?.staff_id ?? onboardingId}{" "}
        <StatusBadge tone={stageTone(currentStage)}>{currentStage || "unknown"}</StatusBadge>
      </h4>
      <p>
        Role: {d.staff?.job_role ?? "—"} · Unit: {d.staff?.unit ?? "—"} · Service line:{" "}
        {d.staff?.service_line ?? "—"} · Staff status: {d.staff?.status ?? "—"}
      </p>
      {stages.length > 0 && <Stepper steps={stages} current={currentIdx} />}

      <h5>Blockers</h5>
      {hasBlockers ? (
        <ul aria-label="Onboarding blockers">
          {blockers.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      ) : (
        <Empty label="No blockers — this record can advance." />
      )}

      {canOnboard && (
        <p>
          <button type="button" disabled={busy || hasBlockers} onClick={() => void advance()}>
            Advance stage
          </button>{" "}
          {hasBlockers && (
            <span className="field-error">
              Cannot advance: {blockers.length} blocker(s) remain — resolve them above first.
              The server will refuse the advance until they clear.
            </span>
          )}
        </p>
      )}
      {devToken && (
        <div className="state state-empty" role="status">
          <p>
            Account provisioned. Dev-mode sign-in token (shown once — copy it
            now, it cannot be retrieved again): <code>{devToken}</code>
          </p>
        </div>
      )}

      <h5>Credentials</h5>
      {d.credentials && d.credentials.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th scope="col">Type</th>
              <th scope="col">Number</th>
              <th scope="col">Expires</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {d.credentials.map((c, i) => (
              <tr key={`${c.type}-${c.number}-${i}`}>
                <td>{c.type ?? "—"}</td>
                <td>{c.number ?? "—"}</td>
                <td>{c.expires_on ?? "—"}</td>
                <td>{c.status ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty label="No credentials recorded yet." />
      )}
      {canOnboard && (
        <form
          noValidate
          className="inline-form"
          aria-label="Add credential"
          onSubmit={(e) => { e.preventDefault(); void addCredential(); }}
        >
          <h6>Add credential</h6>
          <label className="field">
            Credential type *
            <input aria-label="Credential type" value={credForm.type} onChange={(e) => setCredForm((f) => ({ ...f, type: e.target.value }))} />
            {credErrors.type && <span className="field-error" role="alert">{credErrors.type}</span>}
          </label>
          <label className="field">
            Credential number *
            <input aria-label="Credential number" value={credForm.number} onChange={(e) => setCredForm((f) => ({ ...f, number: e.target.value }))} />
            {credErrors.number && <span className="field-error" role="alert">{credErrors.number}</span>}
          </label>
          <label className="field">
            Issued on
            <input aria-label="Credential issued on" type="date" value={credForm.issued_on} onChange={(e) => setCredForm((f) => ({ ...f, issued_on: e.target.value }))} />
          </label>
          <label className="field">
            Expires on
            <input aria-label="Credential expires on" type="date" value={credForm.expires_on} onChange={(e) => setCredForm((f) => ({ ...f, expires_on: e.target.value }))} />
          </label>
          <button type="submit" disabled={busy}>Add credential</button>
        </form>
      )}

      <h5>Privileges</h5>
      {d.privileges && d.privileges.length > 0 ? (
        <ul aria-label="Privileges">
          {d.privileges.map((p, i) => (
            <li key={`${p.privilege_code}-${i}`}>
              {p.privilege_code ?? "—"}{p.service_line ? ` · ${p.service_line}` : ""}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No privileges granted yet." />
      )}

      <h5>Account</h5>
      {d.account ? (
        <p>
          Username: {d.account.username ?? "—"} · Status: {d.account.status ?? "—"} · Role:{" "}
          {d.account.role ?? "—"}
        </p>
      ) : (
        <Empty label="No login account provisioned yet — it is created at the provisioning stage." />
      )}

      <h5>Orientation</h5>
      {d.orientation && d.orientation.items && d.orientation.items.length > 0 ? (
        <ul aria-label="Orientation checklist">
          {d.orientation.items.map((item) => (
            <li key={item.linkId}>
              <span>{item.text ?? item.linkId}</span>
              {item.required ? " (required)" : ""} —{" "}
              <StatusBadge tone={item.completed ? "ok" : "warn"}>
                {item.completed ? "Completed" : "Pending"}
              </StatusBadge>{" "}
              {canOnboard && !item.completed && (
                <button type="button" className="linklike" disabled={busy} onClick={() => void completeItem(item.linkId)}>
                  Complete
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No orientation checklist for this record." />
      )}

      {canOnboard && (
        <form
          noValidate
          className="inline-form"
          aria-label="Terminate onboarding"
          onSubmit={(e) => { e.preventDefault(); void terminate(); }}
        >
          <h5>Terminate onboarding</h5>
          <label className="field">
            Reason *
            <input aria-label="Terminate reason" value={terminateReason} onChange={(e) => setTerminateReason(e.target.value)} />
            {terminateError && <span className="field-error" role="alert">{terminateError}</span>}
          </label>
          <button type="submit" disabled={busy}>Terminate onboarding</button>
        </form>
      )}
    </div>
  );
}
