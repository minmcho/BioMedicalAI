import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { formatPlacementView } from "../api/org";
import type { Role } from "../api/types";
import * as teamsApi from "../api/teams";
import type {
  Assignment,
  CoverageRecord,
  PcpPanelSummary,
  StaffLite,
  TeamPanel,
  TeamSummary,
} from "../api/teams";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { MasterDetail } from "../components/MasterDetail";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function stateTone(state?: string | null): StatusTone {
  const s = (state ?? "").toLowerCase();
  if (s === "active" || s === "accepted" || s === "complete" || s === "completed") return "ok";
  if (s === "requested" || s === "pending" || s === "proposed") return "warn";
  if (s === "declined" || s === "cancelled" || s === "ended" || s === "terminated") return "bad";
  return "neutral";
}

function roleBadge(role?: string | null) {
  const r = (role ?? "").toLowerCase();
  if (r === "primary") return <StatusBadge tone="ok">Primary</StatusBadge>;
  if (r === "consulting") return <StatusBadge tone="info">Consulting</StatusBadge>;
  return <StatusBadge tone="neutral">{role ?? "—"}</StatusBadge>;
}

function capacityInfo(
  pcp: PcpPanelSummary,
  maxWeighted?: number,
): { label: string; tone: StatusTone } {
  const raw =
    (typeof pcp.capacity_status === "string" && pcp.capacity_status) ||
    (typeof pcp.capacity === "string" && pcp.capacity) ||
    "";
  const norm = raw.toLowerCase();
  if (norm.includes("over")) return { label: "Over capacity", tone: "bad" };
  if (norm.includes("under")) return { label: "Under capacity", tone: "info" };
  if (norm.includes("within") || norm.includes("at")) {
    return { label: "Within capacity", tone: "ok" };
  }
  const weighted = pcp.weighted_size ?? pcp.panel_size ?? 0;
  const cap =
    typeof pcp.capacity === "number" ? pcp.capacity : maxWeighted;
  if (cap != null) {
    if (weighted > cap) return { label: "Over capacity", tone: "bad" };
    if (weighted < cap * 0.5) return { label: "Under capacity", tone: "info" };
    return { label: "Within capacity", tone: "ok" };
  }
  return { label: "Within capacity", tone: "ok" };
}

/** Staff list via the request surface (so page tests stub one method). */
async function fetchAllStaff(client: ApiClient): Promise<StaffLite[]> {
  const out = await client.request<{ items: StaffLite[] }>("/people/staff");
  return out.items ?? [];
}

async function fetchUnits(
  client: ApiClient,
): Promise<Array<{ code: string; display?: string }>> {
  const cfg = await client.request<{
    units?: Array<{ code: string; display?: string }>;
  }>("/people/config");
  return cfg.units ?? [];
}

/** Teams, panels & empanelment (Workforce, Teams & Panels pass).
 *  Four tabs over /teams: the team workspace panel, the house-wide
 *  assignment queue, the PCP panel manager and the caller's own
 *  teams. Work actions follow teams.work and are enforced again
 *  server-side; every workflow state is text + colour (StatusBadge),
 *  never colour alone, and server {detail} errors surface inline. */
export function TeamsPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["teams.work"]?.[role] === true;
  const [tab, setTab] = useState<string>("panel");

  return (
    <div>
      <ModuleHeader icon="people" group="People & Teams" title="Teams" purpose="Care teams, patient assignments, empanelment and your own teams." />
      <Tabs
        tabs={[
          { key: "panel", label: "Team panel" },
          { key: "assignments", label: "Assignments" },
          { key: "panels", label: "Panels" },
          { key: "my-teams", label: "My teams" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Teams sections"
        panels={{
          panel: <TeamPanelTab client={client} canWork={canWork} />,
          assignments: <AssignmentsTab client={client} canWork={canWork} />,
          panels: <PanelsTab client={client} canWork={canWork} />,
          "my-teams": <MyTeamsTab client={client} />,
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Team panel tab
// ---------------------------------------------------------------------------

function TeamPanelTab({ client, canWork }: { client: ApiClient; canWork: boolean }) {
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);

  const teamsState = useAsync(() => teamsApi.listTeams(client), [client, tick]);
  const configState = useAsync(() => teamsApi.getTeamsConfig(client), [client]);
  const staffState = useAsync(() => fetchAllStaff(client), [client]);
  const unitsState = useAsync(() => fetchUnits(client), [client]);

  const teams = teamsState.data?.items ?? [];
  const effectiveTeamId =
    selectedTeamId && teams.some((t) => t.team_id === selectedTeamId)
      ? selectedTeamId
      : (teams[0]?.team_id ?? null);

  if (teamsState.loading) return <Loading label="Loading teams…" />;
  if (teamsState.error) return <ErrorView error={teamsState.error} />;

  return (
    <section aria-label="Team panel">
      <h3>Team panel</h3>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}
      {canWork && (
        <CreateTeamForm
          client={client}
          teamTypes={configState.data?.teams.teamTypes ?? []}
          units={unitsState.data ?? []}
          onCreated={() => {
            setNotice("Team created.");
            setTick((t) => t + 1);
          }}
          onError={setActionError}
        />
      )}
      {teams.length === 0 ? (
        <Empty label="No teams yet. Create the first team above." />
      ) : (
        <MasterDetail<TeamSummary>
          items={teams}
          loading={teamsState.loading}
          getKey={(t) => t.team_id}
          listLabel="Teams"
          emptyText="No teams yet."
          detailEmptyText="Select a team to see its panel."
          selectedKey={effectiveTeamId}
          onSelect={setSelectedTeamId}
          renderListItem={(t) => (
            <>
              <span className="patient-name">{t.name}</span>
              <span className="patient-meta">
                {t.team_type}
                {t.unit ? ` · ${t.unit}` : ""}
                {` · ${t.member_count ?? 0} members · ${t.active_patient_count ?? 0} patients`}
              </span>
            </>
          )}
          renderDetail={(t) => (
            <TeamPanelDetail
              key={`${t.team_id}-${tick}`}
              client={client}
              team={t}
              canWork={canWork}
              staff={staffState.data ?? []}
              staffReason={
                staffState.error
                  ? `Staff list unavailable: ${errorText(staffState.error)}`
                  : staffState.loading
                    ? "Loading staff…"
                    : undefined
              }
              teamRoles={configState.data?.teams.teamRoles ?? []}
              allTeams={teams}
              onChanged={(msg) => {
                setNotice(msg);
                setTick((v) => v + 1);
              }}
              onError={setActionError}
            />
          )}
        />
      )}
    </section>
  );
}

function CreateTeamForm({
  client,
  teamTypes,
  units,
  onCreated,
  onError,
}: {
  client: ApiClient;
  teamTypes: Array<{ code: string; display?: string }>;
  units: Array<{ code: string; display?: string }>;
  onCreated: () => void;
  onError: (msg: string | null) => void;
}) {
  const [form, setForm] = useState({ name: "", team_type: "", unit: "", service_line: "", description: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function submit() {
    const errs: Record<string, string> = {};
    if (!form.name.trim()) errs.name = "Team name is required.";
    if (!form.team_type) errs.team_type = "Team type is required.";
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    onError(null);
    try {
      await teamsApi.createTeam(client, {
        name: form.name.trim(),
        team_type: form.team_type,
        ...(form.unit ? { unit: form.unit } : {}),
        ...(form.service_line.trim() ? { service_line: form.service_line.trim() } : {}),
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
      });
      setForm({ name: "", team_type: "", unit: "", service_line: "", description: "" });
      onCreated();
    } catch (err) {
      onError(`Create team failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label="Create team"
      onSubmit={(e) => { e.preventDefault(); void submit(); }}
    >
      <h4>Create team</h4>
      <label className="field">
        Team name *
        <input aria-label="Team name" value={form.name} onChange={(e) => patch({ name: e.target.value })} />
        {errors.name && <span className="field-error" role="alert">{errors.name}</span>}
      </label>
      <label className="field">
        Team type *
        <select
          aria-label="Team type"
          value={form.team_type}
          disabled={teamTypes.length === 0}
          onChange={(e) => patch({ team_type: e.target.value })}
        >
          <option value="">Select a team type</option>
          {teamTypes.map((t) => (
            <option key={t.code} value={t.code}>{t.display ?? t.code}</option>
          ))}
        </select>
        {teamTypes.length === 0 && (
          <span className="field-error">No team types in the published teams config.</span>
        )}
        {errors.team_type && <span className="field-error" role="alert">{errors.team_type}</span>}
      </label>
      <label className="field">
        Unit
        <select
          aria-label="Team unit"
          value={form.unit}
          disabled={units.length === 0}
          onChange={(e) => patch({ unit: e.target.value })}
        >
          <option value="">(no unit)</option>
          {units.map((u) => (
            <option key={u.code} value={u.code}>{u.display ?? u.code}</option>
          ))}
        </select>
        {units.length === 0 && <span className="field-error">No units in the published workforce config.</span>}
      </label>
      <label className="field">
        Service line
        <input aria-label="Team service line" value={form.service_line} onChange={(e) => patch({ service_line: e.target.value })} />
      </label>
      <label className="field">
        Description
        <input aria-label="Team description" value={form.description} onChange={(e) => patch({ description: e.target.value })} />
      </label>
      <button type="submit" disabled={busy}>Create team</button>
    </form>
  );
}

function TeamPanelDetail({
  client,
  team,
  canWork,
  staff,
  staffReason,
  teamRoles,
  allTeams,
  onChanged,
  onError,
}: {
  client: ApiClient;
  team: TeamSummary;
  canWork: boolean;
  staff: StaffLite[];
  staffReason?: string;
  teamRoles: Array<{ code: string; display?: string }>;
  allTeams: TeamSummary[];
  onChanged: (msg: string) => void;
  onError: (msg: string | null) => void;
}) {
  const [localTick, setLocalTick] = useState(0);
  const panelState = useAsync(
    () => teamsApi.getTeamPanel(client, team.team_id),
    [client, team.team_id, localTick],
  );
  const [busy, setBusy] = useState(false);
  const [declineReasons, setDeclineReasons] = useState<Record<string, string>>({});
  const [memberForm, setMemberForm] = useState({ staff_id: "", role_in_team: "" });
  const [memberErrors, setMemberErrors] = useState<Record<string, string>>({});
  const [coverageForm, setCoverageForm] = useState({
    covered_staff_id: "", covering_staff_id: "", covering_team_id: "",
    starts_at: "", ends_at: "", reason: "",
  });
  const [coverageErrors, setCoverageErrors] = useState<Record<string, string>>({});

  const refresh = (msg: string) => {
    setLocalTick((t) => t + 1);
    onChanged(msg);
  };

  async function accept(id: string) {
    setBusy(true);
    onError(null);
    try {
      await teamsApi.acceptAssignment(client, id);
      refresh("Assignment accepted.");
    } catch (err) {
      onError(`Accept failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function decline(id: string) {
    const reason = (declineReasons[id] ?? "").trim();
    if (!reason) {
      onError("Declining an assignment needs a reason — enter one next to the request first.");
      return;
    }
    setBusy(true);
    onError(null);
    try {
      await teamsApi.declineAssignment(client, id, reason);
      refresh("Assignment declined.");
    } catch (err) {
      onError(`Decline failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function assignUnassigned(u: TeamPanel["unassigned"][number]) {
    if (!u.patient_ref) {
      onError("That unassigned row has no patient reference, so it cannot be assigned.");
      return;
    }
    setBusy(true);
    onError(null);
    try {
      await teamsApi.createAssignment(client, {
        patient_ref: u.patient_ref,
        ...(u.encounter_id ? { encounter_id: u.encounter_id } : {}),
        team_id: team.team_id,
        role: "primary",
      });
      refresh(`Assignment requested for ${u.patient_name ?? u.patient_ref}.`);
    } catch (err) {
      onError(`Assign failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function addMember() {
    const errs: Record<string, string> = {};
    if (!memberForm.staff_id) errs.staff_id = "Choose a staff member.";
    if (!memberForm.role_in_team) errs.role_in_team = "Choose a role in the team.";
    setMemberErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    onError(null);
    try {
      await teamsApi.addTeamMember(client, team.team_id, {
        staff_id: memberForm.staff_id,
        role_in_team: memberForm.role_in_team,
      });
      setMemberForm({ staff_id: "", role_in_team: "" });
      refresh("Team member added.");
    } catch (err) {
      onError(`Add member failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function endMember(membershipId: string) {
    setBusy(true);
    onError(null);
    try {
      await teamsApi.endTeamMember(client, team.team_id, membershipId);
      refresh("Team membership ended.");
    } catch (err) {
      onError(`End membership failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function createCoverage() {
    const errs: Record<string, string> = {};
    if (!coverageForm.covered_staff_id) errs.covered_staff_id = "Choose who is covered.";
    if (!coverageForm.covering_staff_id) errs.covering_staff_id = "Choose who covers.";
    setCoverageErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    onError(null);
    try {
      await teamsApi.createCoverage(client, {
        covered_staff_id: coverageForm.covered_staff_id,
        covered_team_id: team.team_id,
        covering_staff_id: coverageForm.covering_staff_id,
        ...(coverageForm.covering_team_id ? { covering_team_id: coverageForm.covering_team_id } : {}),
        ...(coverageForm.starts_at ? { starts_at: coverageForm.starts_at } : {}),
        ...(coverageForm.ends_at ? { ends_at: coverageForm.ends_at } : {}),
        ...(coverageForm.reason.trim() ? { reason: coverageForm.reason.trim() } : {}),
      });
      setCoverageForm({ covered_staff_id: "", covering_staff_id: "", covering_team_id: "", starts_at: "", ends_at: "", reason: "" });
      refresh("Coverage arranged.");
    } catch (err) {
      onError(`Create coverage failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function cancelCoverage(id: string) {
    setBusy(true);
    onError(null);
    try {
      await teamsApi.cancelCoverage(client, id);
      refresh("Coverage cancelled.");
    } catch (err) {
      onError(`Cancel coverage failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  if (panelState.loading) return <Loading label="Loading team panel…" />;
  if (panelState.error) return <ErrorView error={panelState.error} />;
  const panel = panelState.data;
  if (!panel) return <Empty label="No panel data for this team." />;

  return (
    <div>
      <h4>
        {panel.team?.name ?? team.name}{" "}
        <StatusBadge tone={stateTone(panel.team?.status ?? team.status)}>
          {panel.team?.status ?? team.status ?? "active"}
        </StatusBadge>
      </h4>
      <p className="hint">
        {panel.team?.team_type ?? team.team_type}
        {(panel.team?.unit ?? team.unit) ? ` · ${panel.team?.unit ?? team.unit}` : ""}
      </p>
      {(panel.team?.placement !== undefined || team.placement !== undefined) && (
        <p>
          Placement:{" "}
          {formatPlacementView(panel.team?.placement ?? team.placement) || "Not placed"}
        </p>
      )}

      <h5>Members</h5>
      {panel.members && panel.members.length > 0 ? (
        <ul aria-label="Team members">
          {panel.members.map((m) => (
            <li key={m.staff_id}>
              {m.staff_name ?? m.staff_id} — {m.role_in_team ?? "member"}{" "}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No members on this team yet." />
      )}
      {canWork && (
        <form
          noValidate
          className="inline-form"
          aria-label="Add team member"
          onSubmit={(e) => { e.preventDefault(); void addMember(); }}
        >
          <label className="field">
            Staff member *
            <select
              aria-label="Member staff"
              value={memberForm.staff_id}
              disabled={staff.length === 0}
              onChange={(e) => setMemberForm((f) => ({ ...f, staff_id: e.target.value }))}
            >
              <option value="">Select staff</option>
              {staff.map((s) => (
                <option key={s.staff_id} value={s.staff_id}>
                  {s.full_name} ({s.staff_id})
                </option>
              ))}
            </select>
            {staff.length === 0 && (
              <span className="field-error">{staffReason ?? "The staff directory is empty."}</span>
            )}
            {memberErrors.staff_id && <span className="field-error" role="alert">{memberErrors.staff_id}</span>}
          </label>
          <label className="field">
            Role in team *
            <select
              aria-label="Role in team"
              value={memberForm.role_in_team}
              disabled={teamRoles.length === 0}
              onChange={(e) => setMemberForm((f) => ({ ...f, role_in_team: e.target.value }))}
            >
              <option value="">Select a role</option>
              {teamRoles.map((r) => (
                <option key={r.code} value={r.code}>{r.display ?? r.code}</option>
              ))}
            </select>
            {teamRoles.length === 0 && (
              <span className="field-error">No team roles in the published teams config.</span>
            )}
            {memberErrors.role_in_team && <span className="field-error" role="alert">{memberErrors.role_in_team}</span>}
          </label>
          <button type="submit" disabled={busy}>Add member</button>
        </form>
      )}
      {/* Membership end actions ride the team detail members (with ids). */}
      <TeamMembershipEndList client={client} team={team} canWork={canWork} busy={busy} onEnd={endMember} />

      <h5>Patients</h5>
      {panel.patients && panel.patients.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Patient</th>
              <th scope="col">Role</th>
              <th scope="col">Bed</th>
              <th scope="col">Attending</th>
              <th scope="col">Primary nurse</th>
              <th scope="col">Coverage</th>
              <th scope="col">State</th>
            </tr>
          </thead>
          <tbody>
            {panel.patients.map((p, i) => (
              <tr key={p.assignment_id ?? `${p.patient_ref}-${i}`}>
                <td>{p.patient_name ?? p.patient_ref ?? "—"}</td>
                <td>{roleBadge(p.role)}</td>
                <td>{p.bed ?? "—"}</td>
                <td>{p.attending_name ?? "—"}</td>
                <td>{p.primary_nurse ?? "—"}</td>
                <td>{p.via_coverage ? <StatusBadge tone="warn">Coverage</StatusBadge> : "Own team"}</td>
                <td><StatusBadge tone={stateTone(p.state)}>{p.state ?? "active"}</StatusBadge></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty label="No patients assigned to this team." />
      )}

      <h5>Pending inbound</h5>
      {panel.pending_inbound && panel.pending_inbound.length > 0 ? (
        <ul aria-label="Pending inbound assignments">
          {panel.pending_inbound.map((p) => (
            <li key={p.assignment_id}>
              <strong>{p.patient_name ?? p.patient_ref}</strong> — {roleBadge(p.role)}{" "}
              {p.handoff ? <>· Handoff: {p.handoff} </> : null}
              {p.requested_by ? <>· Requested by {p.requested_by} </> : null}
              {canWork && (
                <>
                  <label className="field">
                    Decline reason
                    <input
                      aria-label={`Decline reason for ${p.patient_name ?? p.patient_ref}`}
                      value={declineReasons[p.assignment_id] ?? ""}
                      onChange={(e) =>
                        setDeclineReasons((r) => ({ ...r, [p.assignment_id]: e.target.value }))
                      }
                    />
                  </label>
                  <button type="button" disabled={busy} onClick={() => void accept(p.assignment_id)}>
                    Accept
                  </button>{" "}
                  <button type="button" className="linklike" disabled={busy} onClick={() => void decline(p.assignment_id)}>
                    Decline
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No pending inbound assignments." />
      )}

      <h5>Unassigned in this unit</h5>
      {panel.unassigned && panel.unassigned.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Patient</th>
              <th scope="col">Bed</th>
              <th scope="col">Unit</th>
              {canWork && <th scope="col">Action</th>}
            </tr>
          </thead>
          <tbody>
            {panel.unassigned.map((u, i) => (
              <tr key={u.encounter_id ?? `${u.patient_ref}-${i}`}>
                <td>{u.patient_name ?? u.patient_ref ?? "—"}</td>
                <td>{u.bed ?? "—"}</td>
                <td>{u.unit ?? "—"}</td>
                {canWork && (
                  <td>
                    <button type="button" disabled={busy} onClick={() => void assignUnassigned(u)}>
                      Assign to this team
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty label="No unassigned patients in this team's unit." />
      )}

      <h5>Coverage</h5>
      <p>Covering for others (coverage in):</p>
      {panel.coverage_in && panel.coverage_in.length > 0 ? (
        <ul aria-label="Coverage in">
          {panel.coverage_in.map((c) => (
            <li key={c.coverage_id}>
              Covering {c.covered_staff_id ?? c.covered_team_id ?? "—"} · {c.starts_at ?? ""} – {c.ends_at ?? ""}{" "}
              <StatusBadge tone={stateTone(c.status)}>{c.status ?? "active"}</StatusBadge>
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="This team is not covering for anyone." />
      )}
      <p>Covered by others (coverage out):</p>
      {panel.coverage_out && panel.coverage_out.length > 0 ? (
        <ul aria-label="Coverage out">
          {panel.coverage_out.map((c: CoverageRecord) => (
            <li key={c.coverage_id}>
              {c.covered_staff_id ?? "—"} covered by {c.covering_staff_id ?? c.covering_team_id ?? "—"} ·{" "}
              {c.starts_at ?? ""} – {c.ends_at ?? ""} {c.reason ? `· ${c.reason}` : ""}{" "}
              <StatusBadge tone={stateTone(c.status)}>{c.status ?? "active"}</StatusBadge>{" "}
              {canWork && (
                <button type="button" className="linklike" disabled={busy} onClick={() => void cancelCoverage(c.coverage_id)}>
                  Cancel coverage
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No one is covering this team." />
      )}
      {canWork && (
        <form
          noValidate
          className="inline-form"
          aria-label="Create coverage"
          onSubmit={(e) => { e.preventDefault(); void createCoverage(); }}
        >
          <h6>Arrange coverage</h6>
          <label className="field">
            Who is covered *
            <select
              aria-label="Covered staff"
              value={coverageForm.covered_staff_id}
              disabled={staff.length === 0}
              onChange={(e) => setCoverageForm((f) => ({ ...f, covered_staff_id: e.target.value }))}
            >
              <option value="">Select staff</option>
              {staff.map((s) => (
                <option key={s.staff_id} value={s.staff_id}>{s.full_name} ({s.staff_id})</option>
              ))}
            </select>
            {staff.length === 0 && <span className="field-error">{staffReason ?? "The staff directory is empty."}</span>}
            {coverageErrors.covered_staff_id && <span className="field-error" role="alert">{coverageErrors.covered_staff_id}</span>}
          </label>
          <label className="field">
            Who covers *
            <select
              aria-label="Covering staff"
              value={coverageForm.covering_staff_id}
              disabled={staff.length === 0}
              onChange={(e) => setCoverageForm((f) => ({ ...f, covering_staff_id: e.target.value }))}
            >
              <option value="">Select staff</option>
              {staff.map((s) => (
                <option key={s.staff_id} value={s.staff_id}>{s.full_name} ({s.staff_id})</option>
              ))}
            </select>
            {coverageErrors.covering_staff_id && <span className="field-error" role="alert">{coverageErrors.covering_staff_id}</span>}
          </label>
          <label className="field">
            Covering team
            <select
              aria-label="Covering team"
              value={coverageForm.covering_team_id}
              onChange={(e) => setCoverageForm((f) => ({ ...f, covering_team_id: e.target.value }))}
            >
              <option value="">(same team)</option>
              {allTeams.map((t) => (
                <option key={t.team_id} value={t.team_id}>{t.name}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Starts at
            <input aria-label="Coverage starts at" type="datetime-local" value={coverageForm.starts_at} onChange={(e) => setCoverageForm((f) => ({ ...f, starts_at: e.target.value }))} />
          </label>
          <label className="field">
            Ends at
            <input aria-label="Coverage ends at" type="datetime-local" value={coverageForm.ends_at} onChange={(e) => setCoverageForm((f) => ({ ...f, ends_at: e.target.value }))} />
          </label>
          <label className="field">
            Reason
            <input aria-label="Coverage reason" value={coverageForm.reason} onChange={(e) => setCoverageForm((f) => ({ ...f, reason: e.target.value }))} />
          </label>
          <button type="submit" disabled={busy}>Arrange coverage</button>
        </form>
      )}

      {panel.todays_assignments && panel.todays_assignments.length > 0 && (
        <>
          <h5>Today&apos;s assignments</h5>
          <ul aria-label="Todays assignments">
            {panel.todays_assignments.map((a, i) => (
              <li key={i}>{String(a.patient_name ?? a.patient_ref ?? "Assignment")} — {String(a.role ?? "")}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** End-membership buttons need membership_ids, which live on the
 *  team detail (GET /teams/{id}), not the panel payload — fetched
 *  separately and rendered only when the caller can work. */
function TeamMembershipEndList({
  client,
  team,
  canWork,
  busy,
  onEnd,
}: {
  client: ApiClient;
  team: TeamSummary;
  canWork: boolean;
  busy: boolean;
  onEnd: (membershipId: string) => Promise<void>;
}) {
  const detail = useAsync(
    () => (canWork ? teamsApi.getTeam(client, team.team_id) : Promise.resolve(null)),
    [client, team.team_id, canWork],
  );
  if (!canWork || detail.loading || detail.error || !detail.data) return null;
  const members = detail.data.members ?? [];
  if (members.length === 0) return null;
  return (
    <ul aria-label="End membership">
      {members.map((m) => (
        <li key={m.membership_id}>
          {m.staff_name ?? m.staff_id} ({m.role_in_team ?? "member"}){" "}
          <button type="button" className="linklike" disabled={busy} onClick={() => void onEnd(m.membership_id)}>
            End membership
          </button>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Assignments tab
// ---------------------------------------------------------------------------

function AssignmentsTab({ client, canWork }: { client: ApiClient; canWork: boolean }) {
  const [tick, setTick] = useState(0);
  const [unitFilter, setUnitFilter] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [declineReasons, setDeclineReasons] = useState<Record<string, string>>({});
  const [form, setForm] = useState({ patient_ref: "", encounter_id: "", team_id: "", role: "primary", note: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});

  const unitsState = useAsync(() => fetchUnits(client), [client]);
  const teamsState = useAsync(() => teamsApi.listTeams(client), [client]);
  const unassignedState = useAsync(
    () => teamsApi.listUnassigned(client, unitFilter ? { unit: unitFilter } : {}),
    [client, unitFilter, tick],
  );
  const requestedState = useAsync(
    () => teamsApi.listAssignments(client, { state: "requested" }),
    [client, tick],
  );

  async function create() {
    const errs: Record<string, string> = {};
    if (!form.patient_ref.trim()) errs.patient_ref = "Patient reference is required (e.g. Patient/pat-1).";
    if (!form.team_id) errs.team_id = "Choose a team.";
    if (!form.role) errs.role = "Choose a role.";
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setActionError(null);
    try {
      await teamsApi.createAssignment(client, {
        patient_ref: form.patient_ref.trim(),
        ...(form.encounter_id.trim() ? { encounter_id: form.encounter_id.trim() } : {}),
        team_id: form.team_id,
        role: form.role,
        ...(form.note.trim() ? { note: form.note.trim() } : {}),
      });
      setNotice("Assignment requested.");
      setForm({ patient_ref: "", encounter_id: "", team_id: "", role: "primary", note: "" });
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Create assignment failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function accept(id: string) {
    setBusy(true);
    setActionError(null);
    try {
      await teamsApi.acceptAssignment(client, id);
      setNotice("Assignment accepted.");
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Accept failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function decline(id: string) {
    const reason = (declineReasons[id] ?? "").trim();
    if (!reason) {
      setActionError("Declining an assignment needs a reason — enter one next to the request first.");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await teamsApi.declineAssignment(client, id, reason);
      setNotice("Assignment declined.");
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Decline failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const teams = teamsState.data?.items ?? [];
  const units = unitsState.data ?? [];

  return (
    <section aria-label="Assignments">
      <h3>Assignments</h3>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}

      <label className="field">
        Unit filter
        <select
          aria-label="Unit filter"
          value={unitFilter}
          disabled={units.length === 0}
          onChange={(e) => setUnitFilter(e.target.value)}
        >
          <option value="">All units</option>
          {units.map((u) => (
            <option key={u.code} value={u.code}>{u.display ?? u.code}</option>
          ))}
        </select>
        {units.length === 0 && !unitsState.loading && (
          <span className="field-error">No units in the published workforce config — showing all units.</span>
        )}
      </label>

      <h4>Unassigned patients (house-wide)</h4>
      {unassignedState.loading && <Loading label="Loading unassigned queue…" />}
      {unassignedState.error != null && <ErrorView error={unassignedState.error} />}
      {unassignedState.data && unassignedState.data.items.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Patient</th>
              <th scope="col">Bed</th>
              <th scope="col">Unit</th>
              <th scope="col">Use in form</th>
            </tr>
          </thead>
          <tbody>
            {unassignedState.data.items.map((u, i) => (
              <tr key={u.encounter_id ?? `${u.patient_ref}-${i}`}>
                <td>{u.patient_name ?? u.patient_ref ?? "—"}</td>
                <td>{u.bed ?? "—"}</td>
                <td>{u.unit ?? "—"}</td>
                <td>
                  <button
                    type="button"
                    className="linklike"
                    onClick={() =>
                      setForm((f) => ({
                        ...f,
                        patient_ref: u.patient_ref ?? "",
                        encounter_id: u.encounter_id ?? "",
                      }))
                    }
                  >
                    Use
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        unassignedState.data && <Empty label="No unassigned patients." />
      )}

      {canWork && (
        <form
          noValidate
          className="inline-form"
          aria-label="Create assignment"
          onSubmit={(e) => { e.preventDefault(); void create(); }}
        >
          <h4>Request an assignment</h4>
          <label className="field">
            Patient reference *
            <input
              aria-label="Assignment patient reference"
              value={form.patient_ref}
              placeholder="Patient/pat-1"
              onChange={(e) => setForm((f) => ({ ...f, patient_ref: e.target.value }))}
            />
            {errors.patient_ref && <span className="field-error" role="alert">{errors.patient_ref}</span>}
          </label>
          <label className="field">
            Encounter ID
            <input
              aria-label="Assignment encounter ID"
              value={form.encounter_id}
              onChange={(e) => setForm((f) => ({ ...f, encounter_id: e.target.value }))}
            />
          </label>
          <label className="field">
            Team *
            <select
              aria-label="Assignment team"
              value={form.team_id}
              disabled={teams.length === 0}
              onChange={(e) => setForm((f) => ({ ...f, team_id: e.target.value }))}
            >
              <option value="">Select a team</option>
              {teams.map((t) => (
                <option key={t.team_id} value={t.team_id}>{t.name}</option>
              ))}
            </select>
            {teams.length === 0 && <span className="field-error">No teams exist yet — create one in the Team panel tab.</span>}
            {errors.team_id && <span className="field-error" role="alert">{errors.team_id}</span>}
          </label>
          <label className="field">
            Role *
            <select
              aria-label="Assignment role"
              value={form.role}
              onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
            >
              <option value="primary">Primary</option>
              <option value="consulting">Consulting</option>
            </select>
            {errors.role && <span className="field-error" role="alert">{errors.role}</span>}
          </label>
          <label className="field">
            Note
            <input aria-label="Assignment note" value={form.note} onChange={(e) => setForm((f) => ({ ...f, note: e.target.value }))} />
          </label>
          <button type="submit" disabled={busy}>Request assignment</button>
        </form>
      )}

      <h4>Pending requests</h4>
      {requestedState.loading && <Loading label="Loading pending requests…" />}
      {requestedState.error != null && <ErrorView error={requestedState.error} />}
      {requestedState.data && requestedState.data.items.length > 0 ? (
        <ul aria-label="Pending assignment requests">
          {requestedState.data.items.map((a: Assignment) => {
            const id = teamsApi.assignmentId(a);
            return (
              <li key={id || a.patient_ref}>
                <strong>{a.patient_name ?? a.patient_ref}</strong> — {roleBadge(a.role)}{" "}
                <StatusBadge tone={stateTone(a.state ?? a.status)}>{a.state ?? a.status ?? "requested"}</StatusBadge>{" "}
                {a.team_name ? <>· Team {a.team_name} </> : a.team_id ? <>· Team {a.team_id} </> : null}
                {canWork && id && (
                  <>
                    <label className="field">
                      Decline reason
                      <input
                        aria-label={`Decline reason for ${a.patient_name ?? a.patient_ref}`}
                        value={declineReasons[id] ?? ""}
                        onChange={(e) => setDeclineReasons((r) => ({ ...r, [id]: e.target.value }))}
                      />
                    </label>
                    <button type="button" disabled={busy} onClick={() => void accept(id)}>Accept</button>{" "}
                    <button type="button" className="linklike" disabled={busy} onClick={() => void decline(id)}>Decline</button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        requestedState.data && <Empty label="No pending assignment requests." />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Panels tab
// ---------------------------------------------------------------------------

function PanelsTab({ client, canWork }: { client: ApiClient; canWork: boolean }) {
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedPcp, setSelectedPcp] = useState<string | null>(null);
  const [form, setForm] = useState({ patient_ref: "", pcp_staff_id: "", team_id: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});

  const panelsState = useAsync(() => teamsApi.getPanels(client), [client, tick]);
  const teamsState = useAsync(() => teamsApi.listTeams(client), [client]);
  const configState = useAsync(() => teamsApi.getTeamsConfig(client), [client]);
  const physiciansState = useAsync(
    () => teamsApi.listStaffByJobRole(client, "physician"),
    [client],
  );
  const detailState = useAsync(
    () => (selectedPcp ? teamsApi.getPanelDetail(client, selectedPcp) : Promise.resolve(null)),
    [client, selectedPcp],
  );

  async function empanel() {
    const errs: Record<string, string> = {};
    if (!form.patient_ref.trim()) errs.patient_ref = "Patient reference is required (e.g. Patient/pat-1).";
    if (!form.pcp_staff_id) errs.pcp_staff_id = "Choose a PCP.";
    if (!form.team_id) errs.team_id = "Choose a primary-care team.";
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setActionError(null);
    try {
      await teamsApi.createEmpanelment(client, {
        patient_ref: form.patient_ref.trim(),
        pcp_staff_id: form.pcp_staff_id,
        team_id: form.team_id,
      });
      setNotice("Patient empanelled.");
      setForm({ patient_ref: "", pcp_staff_id: "", team_id: "" });
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Empanel failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  if (panelsState.loading) return <Loading label="Loading panels…" />;
  if (panelsState.error) return <ErrorView error={panelsState.error} />;
  const data = panelsState.data;
  if (!data) return <Empty label="No panel data." />;

  const cov = data.coverage ?? {};
  const maxWeighted = configState.data?.teams.panelThresholds?.maxWeighted;
  const teams = teamsState.data?.items ?? [];
  const teamName = (id?: string | null) =>
    teams.find((t) => t.team_id === id)?.name ?? id ?? "—";
  const primaryCareTeams = teams.filter((t) =>
    (t.team_type ?? "").toLowerCase().includes("primary"),
  );
  const physicians = physiciansState.data ?? [];

  return (
    <section aria-label="Panels">
      <h3>Panels</h3>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}

      <div className="dashboard-grid" aria-label="Panel coverage">
        <div className="dashboard-widget">
          <div className="dashboard-widget-title">Empanelled</div>
          <div>{cov.empanelled ?? 0}</div>
        </div>
        <div className="dashboard-widget">
          <div className="dashboard-widget-title">Patients total</div>
          <div>{cov.patients_total ?? 0}</div>
        </div>
        <div className="dashboard-widget">
          <div className="dashboard-widget-title">Unempanelled</div>
          <div>{cov.unempanelled ?? 0}</div>
        </div>
        <div className="dashboard-widget">
          <div className="dashboard-widget-title">Coverage</div>
          <div>{cov.coverage_pct != null ? `${cov.coverage_pct}%` : "—"}</div>
        </div>
      </div>
      <p className="hint">
        Coverage: {cov.empanelled ?? 0} of {cov.patients_total ?? 0} patients empanelled
        {cov.coverage_pct != null ? ` (${cov.coverage_pct}%)` : ""}.
      </p>

      <h4>PCP panels</h4>
      {data.pcps && data.pcps.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">PCP</th>
              <th scope="col">Team</th>
              <th scope="col">Panel size</th>
              <th scope="col">Weighted size</th>
              <th scope="col">Capacity</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {data.pcps.map((p) => {
              const cap = capacityInfo(p, maxWeighted);
              return (
                <tr key={p.pcp_staff_id}>
                  <td>{p.pcp_name ?? p.pcp_staff_id}</td>
                  <td>{p.team_name ?? teamName(p.team_id)}</td>
                  <td className="num">{p.panel_size ?? 0}</td>
                  <td className="num">{p.weighted_size ?? 0}</td>
                  <td><StatusBadge tone={cap.tone}>{cap.label}</StatusBadge></td>
                  <td>
                    <button type="button" className="linklike" onClick={() => setSelectedPcp(p.pcp_staff_id)}>
                      View panel
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <Empty label="No PCP panels yet." />
      )}

      {selectedPcp && (
        <div>
          <h4>Panel detail — {selectedPcp}</h4>
          {detailState.loading && <Loading label="Loading panel detail…" />}
          {detailState.error != null && <ErrorView error={detailState.error} />}
          {detailState.data && (
            <>
              {detailState.data.patients && detailState.data.patients.length > 0 ? (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Patient</th>
                      <th scope="col">Weight</th>
                      <th scope="col">Basis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detailState.data.patients.map((pt, i) => (
                      <tr key={pt.patient_ref ?? i}>
                        <td>{pt.patient_name ?? pt.patient_ref ?? "—"}</td>
                        <td className="num">{pt.weight ?? "—"}</td>
                        <td>{pt.basis ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty label="No patients on this panel." />
              )}
              <h5>Care gaps</h5>
              {detailState.data.care_gaps && detailState.data.care_gaps.length > 0 ? (
                <ul aria-label="Care gaps">
                  {detailState.data.care_gaps.map((g, i) => (
                    <li key={i}>{typeof g === "string" ? g : String(g.description ?? g.gap ?? JSON.stringify(g))}</li>
                  ))}
                </ul>
              ) : (
                <Empty label="No open care gaps on this panel." />
              )}
            </>
          )}
        </div>
      )}

      <h4>Unempanelled patients</h4>
      {data.unempanelled && data.unempanelled.length > 0 ? (
        <ul aria-label="Unempanelled patients">
          {data.unempanelled.map((u, i) => (
            <li key={i}>
              {typeof u === "string"
                ? u
                : String(u.patient_name ?? u.patient_ref ?? JSON.stringify(u))}
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="Every patient is empanelled." />
      )}

      {canWork && (
        <form
          noValidate
          className="inline-form"
          aria-label="Empanel patient"
          onSubmit={(e) => { e.preventDefault(); void empanel(); }}
        >
          <h4>Empanel a patient</h4>
          <label className="field">
            Patient reference *
            <input
              aria-label="Empanel patient reference"
              value={form.patient_ref}
              placeholder="Patient/pat-1"
              onChange={(e) => setForm((f) => ({ ...f, patient_ref: e.target.value }))}
            />
            {errors.patient_ref && <span className="field-error" role="alert">{errors.patient_ref}</span>}
          </label>
          <label className="field">
            PCP *
            <select
              aria-label="Empanel PCP"
              value={form.pcp_staff_id}
              disabled={physicians.length === 0}
              onChange={(e) => setForm((f) => ({ ...f, pcp_staff_id: e.target.value }))}
            >
              <option value="">Select a PCP</option>
              {physicians.map((s) => (
                <option key={s.staff_id} value={s.staff_id}>{s.full_name} ({s.staff_id})</option>
              ))}
            </select>
            {physicians.length === 0 && (
              <span className="field-error">
                {physiciansState.error
                  ? `Physician list unavailable: ${errorText(physiciansState.error)}`
                  : "No physicians in the staff directory."}
              </span>
            )}
            {errors.pcp_staff_id && <span className="field-error" role="alert">{errors.pcp_staff_id}</span>}
          </label>
          <label className="field">
            Primary-care team *
            <select
              aria-label="Empanel team"
              value={form.team_id}
              disabled={primaryCareTeams.length === 0}
              onChange={(e) => setForm((f) => ({ ...f, team_id: e.target.value }))}
            >
              <option value="">Select a team</option>
              {primaryCareTeams.map((t) => (
                <option key={t.team_id} value={t.team_id}>{t.name}</option>
              ))}
            </select>
            {primaryCareTeams.length === 0 && (
              <span className="field-error">No primary-care teams exist yet — create one in the Team panel tab.</span>
            )}
            {errors.team_id && <span className="field-error" role="alert">{errors.team_id}</span>}
          </label>
          <button type="submit" disabled={busy}>Empanel patient</button>
        </form>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// My teams tab
// ---------------------------------------------------------------------------

function MyTeamsTab({ client }: { client: ApiClient }) {
  const state = useAsync(() => teamsApi.getMyTeams(client), [client]);
  if (state.loading) return <Loading label="Loading your teams…" />;
  if (state.error) return <ErrorView error={state.error} />;
  const data = state.data;
  if (!data || !data.staff_id) {
    return (
      <section aria-label="My teams">
        <h3>My teams</h3>
        <Empty label="You are not linked to a staff record, so you have no teams yet. Ask an administrator to link your account to the staff directory." />
      </section>
    );
  }
  return (
    <section aria-label="My teams">
      <h3>My teams</h3>
      <p className="hint">Staff record: {data.staff_id}</p>
      {data.teams && data.teams.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col">Type</th>
              <th scope="col">Your role</th>
              <th scope="col">Unit</th>
            </tr>
          </thead>
          <tbody>
            {data.teams.map((t, i) => (
              <tr key={t.team_id ?? i}>
                <td>{t.name ?? t.team_id ?? "—"}</td>
                <td>{t.team_type ?? "—"}</td>
                <td>{t.role_in_team ?? "—"}</td>
                <td>{t.unit ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty label="You are not a member of any team yet." />
      )}
      {data.panel ? (
        <p>
          My panel: {data.panel.panel_size ?? 0} patients
          {data.panel.weighted_size != null ? ` · weighted ${data.panel.weighted_size}` : ""}.
        </p>
      ) : (
        <p className="hint">You do not hold a PCP panel.</p>
      )}
      <h4>Coverage I am giving</h4>
      {data.coverage_giving && data.coverage_giving.length > 0 ? (
        <ul aria-label="Coverage giving">
          {data.coverage_giving.map((c) => (
            <li key={c.coverage_id}>
              Covering {c.covered_staff_id ?? c.covered_team_id ?? "—"} · {c.starts_at ?? ""} – {c.ends_at ?? ""}{" "}
              <StatusBadge tone={stateTone(c.status)}>{c.status ?? "active"}</StatusBadge>
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="You are not covering for anyone." />
      )}
      <h4>Coverage I am receiving</h4>
      {data.coverage_receiving && data.coverage_receiving.length > 0 ? (
        <ul aria-label="Coverage receiving">
          {data.coverage_receiving.map((c) => (
            <li key={c.coverage_id}>
              Covered by {c.covering_staff_id ?? c.covering_team_id ?? "—"} · {c.starts_at ?? ""} – {c.ends_at ?? ""}{" "}
              <StatusBadge tone={stateTone(c.status)}>{c.status ?? "active"}</StatusBadge>
            </li>
          ))}
        </ul>
      ) : (
        <Empty label="No one is covering you." />
      )}
    </section>
  );
}
