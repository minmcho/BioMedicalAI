// Teams, panels & onboarding client (Workforce, Teams & Panels pass).
// Source of truth is the backend routers under /teams and /onboarding
// (built concurrently against this contract). Types live here, not in
// api/types.ts, per the domain-client pattern (see api/inbox.ts,
// api/support.ts). Every function takes the ApiClient first and calls
// client.request(...); errors are FastAPI {detail} surfaced by
// ApiClient as ApiError — callers show err.detail inline.

import type { ApiClient } from "./client";
import type { PlacementView } from "./types";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface TeamConcept {
  code: string;
  display?: string;
  [key: string]: unknown;
}

export interface JobCatalogueRole {
  jobRole: string;
  display?: string;
  clinical?: boolean;
  requiredCredentials?: string[];
  defaultAccountRole?: string;
  privilegeTemplate?: string;
  [key: string]: unknown;
}

export interface TeamsConfig {
  teams: {
    teamTypes: TeamConcept[];
    teamRoles: TeamConcept[];
    panelThresholds: { maxWeighted?: number; minWeighted?: number; [key: string]: unknown };
    panelWeights: Record<string, unknown>;
    [key: string]: unknown;
  };
  privileges: Record<string, unknown>;
  jobCatalogue: { roles: JobCatalogueRole[]; [key: string]: unknown };
  [key: string]: unknown;
}

export function getTeamsConfig(client: ApiClient): Promise<TeamsConfig> {
  return client.request<TeamsConfig>("/teams/config");
}

// ---------------------------------------------------------------------------
// Teams CRUD + membership
// ---------------------------------------------------------------------------

export interface TeamSummary {
  team_id: string;
  name: string;
  team_type: string;
  unit?: string | null;
  /** Resolved org placement (Organisation pass), derived from the
   *  team's unit; null when the unit does not resolve. */
  placement?: PlacementView | null;
  service_line?: string | null;
  status?: string;
  member_count?: number;
  active_patient_count?: number;
  [key: string]: unknown;
}

export interface TeamListResponse {
  items: TeamSummary[];
  total: number;
}

export interface TeamMember {
  membership_id: string;
  staff_id: string;
  staff_name?: string;
  role_in_team?: string;
  status?: string;
  [key: string]: unknown;
}

export interface TeamDetail extends TeamSummary {
  description?: string | null;
  members: TeamMember[];
  [key: string]: unknown;
}

export function listTeams(client: ApiClient): Promise<TeamListResponse> {
  return client.request<TeamListResponse>("/teams");
}

export function createTeam(
  client: ApiClient,
  input: {
    name: string;
    team_type: string;
    unit?: string;
    service_line?: string;
    description?: string;
  },
): Promise<TeamDetail> {
  return client.request<TeamDetail>("/teams", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getTeam(client: ApiClient, teamId: string): Promise<TeamDetail> {
  return client.request<TeamDetail>(`/teams/${encodeURIComponent(teamId)}`);
}

export function addTeamMember(
  client: ApiClient,
  teamId: string,
  input: { staff_id: string; role_in_team: string },
): Promise<TeamMember> {
  return client.request<TeamMember>(
    `/teams/${encodeURIComponent(teamId)}/members`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export function endTeamMember(
  client: ApiClient,
  teamId: string,
  membershipId: string,
  reason?: string,
): Promise<unknown> {
  return client.request(
    `/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(membershipId)}/end`,
    {
      method: "POST",
      body: JSON.stringify(reason ? { reason } : {}),
    },
  );
}

// ---------------------------------------------------------------------------
// Team panel (workspace for one team)
// ---------------------------------------------------------------------------

export interface PanelPatient {
  assignment_id?: string;
  patient_ref?: string;
  patient_name?: string;
  role?: string;
  state?: string;
  encounter_id?: string | null;
  bed?: string | null;
  attending_name?: string | null;
  primary_nurse?: string | null;
  via_coverage?: boolean;
  [key: string]: unknown;
}

export interface PendingInbound {
  assignment_id: string;
  patient_ref?: string;
  patient_name?: string;
  role?: string;
  handoff?: string | null;
  requested_by?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface UnassignedPatient {
  encounter_id?: string;
  patient_ref?: string;
  patient_name?: string;
  bed?: string | null;
  unit?: string | null;
  [key: string]: unknown;
}

export interface CoverageRecord {
  coverage_id: string;
  covered_staff_id?: string;
  covered_team_id?: string;
  covering_staff_id?: string;
  covering_team_id?: string;
  starts_at?: string | null;
  ends_at?: string | null;
  reason?: string | null;
  status?: string;
  [key: string]: unknown;
}

export interface TeamPanel {
  team: TeamSummary;
  members: Array<{
    staff_id: string;
    staff_name?: string;
    role_in_team?: string;
    [key: string]: unknown;
  }>;
  patients: PanelPatient[];
  pending_inbound: PendingInbound[];
  unassigned: UnassignedPatient[];
  coverage_in: CoverageRecord[];
  coverage_out: CoverageRecord[];
  todays_assignments: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export function getTeamPanel(client: ApiClient, teamId: string): Promise<TeamPanel> {
  return client.request<TeamPanel>(`/teams/${encodeURIComponent(teamId)}/panel`);
}

// ---------------------------------------------------------------------------
// Assignments
// ---------------------------------------------------------------------------

export interface Assignment {
  assignment_id?: string;
  id?: string;
  patient_ref?: string;
  patient_name?: string;
  encounter_id?: string | null;
  team_id?: string;
  team_name?: string;
  role?: string;
  state?: string;
  status?: string;
  note?: string | null;
  requested_by?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export function assignmentId(a: Assignment): string {
  return a.assignment_id ?? a.id ?? "";
}

export function createAssignment(
  client: ApiClient,
  input: {
    patient_ref: string;
    encounter_id?: string;
    team_id: string;
    role: string;
    note?: string;
  },
): Promise<Assignment> {
  return client.request<Assignment>("/teams/assignments", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function acceptAssignment(client: ApiClient, id: string): Promise<Assignment> {
  return client.request<Assignment>(
    `/teams/assignments/${encodeURIComponent(id)}/accept`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function declineAssignment(
  client: ApiClient,
  id: string,
  reason: string,
): Promise<Assignment> {
  return client.request<Assignment>(
    `/teams/assignments/${encodeURIComponent(id)}/decline`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}

export function endAssignment(
  client: ApiClient,
  id: string,
  reason?: string,
): Promise<Assignment> {
  return client.request<Assignment>(
    `/teams/assignments/${encodeURIComponent(id)}/end`,
    { method: "POST", body: JSON.stringify(reason ? { reason } : {}) },
  );
}

export function listAssignments(
  client: ApiClient,
  params: { state?: string; team_id?: string } = {},
): Promise<{ items: Assignment[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.state) qs.set("state", params.state);
  if (params.team_id) qs.set("team_id", params.team_id);
  const query = qs.toString();
  return client.request(`/teams/assignments${query ? `?${query}` : ""}`);
}

export function listUnassigned(
  client: ApiClient,
  params: { unit?: string; service_line?: string } = {},
): Promise<{ items: UnassignedPatient[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.unit) qs.set("unit", params.unit);
  if (params.service_line) qs.set("service_line", params.service_line);
  const query = qs.toString();
  return client.request(`/teams/unassigned${query ? `?${query}` : ""}`);
}

// ---------------------------------------------------------------------------
// Panels / empanelment
// ---------------------------------------------------------------------------

export interface PcpPanelSummary {
  pcp_staff_id: string;
  pcp_name?: string;
  team_id?: string | null;
  team_name?: string | null;
  panel_size?: number;
  weighted_size?: number;
  capacity?: number | string | null;
  capacity_status?: string | null;
  [key: string]: unknown;
}

export interface PanelsOverview {
  pcps: PcpPanelSummary[];
  teams: Array<Record<string, unknown>>;
  coverage: {
    patients_total?: number;
    empanelled?: number;
    unempanelled?: number;
    coverage_pct?: number;
    [key: string]: unknown;
  };
  unempanelled: Array<string | Record<string, unknown>>;
  [key: string]: unknown;
}

export function getPanels(client: ApiClient): Promise<PanelsOverview> {
  return client.request<PanelsOverview>("/teams/panels");
}

export interface PcpPanelDetail {
  pcp_staff_id?: string;
  pcp_name?: string;
  team_id?: string | null;
  panel_size?: number;
  weighted_size?: number;
  capacity?: number | string | null;
  patients: Array<{
    patient_ref?: string;
    patient_name?: string;
    weight?: number;
    basis?: string;
    [key: string]: unknown;
  }>;
  care_gaps: Array<Record<string, unknown> | string>;
  [key: string]: unknown;
}

export function getPanelDetail(client: ApiClient, staffId: string): Promise<PcpPanelDetail> {
  return client.request<PcpPanelDetail>(
    `/teams/panels/${encodeURIComponent(staffId)}`,
  );
}

export function createEmpanelment(
  client: ApiClient,
  input: { patient_ref: string; pcp_staff_id: string; team_id: string },
): Promise<unknown> {
  return client.request("/teams/empanelment", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function endEmpanelment(
  client: ApiClient,
  id: string,
  reason: string,
): Promise<unknown> {
  return client.request(`/teams/empanelment/${encodeURIComponent(id)}/end`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// ---------------------------------------------------------------------------
// Coverage
// ---------------------------------------------------------------------------

export function createCoverage(
  client: ApiClient,
  input: {
    covered_staff_id?: string;
    covered_team_id?: string;
    covering_staff_id?: string;
    covering_team_id?: string;
    starts_at?: string;
    ends_at?: string;
    reason?: string;
    [key: string]: unknown;
  },
): Promise<CoverageRecord> {
  return client.request<CoverageRecord>("/teams/coverage", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function cancelCoverage(
  client: ApiClient,
  id: string,
  reason?: string,
): Promise<unknown> {
  return client.request(`/teams/coverage/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  });
}

// ---------------------------------------------------------------------------
// My teams
// ---------------------------------------------------------------------------

export interface MyTeamsResponse {
  staff_id: string | null;
  teams: Array<{
    team_id?: string;
    name?: string;
    team_type?: string;
    role_in_team?: string;
    unit?: string | null;
    [key: string]: unknown;
  }>;
  panel: {
    panel_size?: number;
    weighted_size?: number;
    [key: string]: unknown;
  } | null;
  coverage_giving: CoverageRecord[];
  coverage_receiving: CoverageRecord[];
  [key: string]: unknown;
}

export function getMyTeams(client: ApiClient): Promise<MyTeamsResponse> {
  return client.request<MyTeamsResponse>("/teams/my-teams");
}

// ---------------------------------------------------------------------------
// Staff helper (PCP picker + member picker)
// ---------------------------------------------------------------------------

export interface StaffLite {
  staff_id: string;
  full_name: string;
  job_role: string;
  unit?: string | null;
  service_line?: string | null;
  status?: string;
  [key: string]: unknown;
}

/** GET /people/staff?job_role=... — server-filtered staff list. */
export async function listStaffByJobRole(
  client: ApiClient,
  jobRole: string,
): Promise<StaffLite[]> {
  const out = await client.request<{ items: StaffLite[] }>(
    `/people/staff?job_role=${encodeURIComponent(jobRole)}`,
  );
  return out.items ?? [];
}

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

export interface OnboardingRecord {
  onboarding_id: string;
  staff_id?: string | null;
  employee_id?: string | null;
  stage?: string;
  [key: string]: unknown;
}

export interface OnboardingListItem {
  record: OnboardingRecord;
  staff_name?: string;
  job_role?: string;
  blockers_count?: number;
  [key: string]: unknown;
}

export interface OnboardingDetail {
  record: OnboardingRecord;
  staff: {
    staff_id?: string;
    full_name?: string;
    job_role?: string;
    unit?: string | null;
    service_line?: string | null;
    status?: string;
    [key: string]: unknown;
  } | null;
  credentials: Array<{
    type?: string;
    number?: string;
    expires_on?: string | null;
    status?: string;
    [key: string]: unknown;
  }>;
  privileges: Array<{
    privilege_code?: string;
    service_line?: string | null;
    [key: string]: unknown;
  }>;
  account: { username?: string; status?: string; role?: string } | null;
  orientation: {
    form_key?: string;
    items: Array<{
      linkId: string;
      text?: string;
      required?: boolean;
      completed?: boolean;
      [key: string]: unknown;
    }>;
    [key: string]: unknown;
  } | null;
  blockers: string[];
  stages: string[];
  dev_token?: string | null;
  [key: string]: unknown;
}

export function listOnboarding(
  client: ApiClient,
  params: { stage?: string; job_role?: string } = {},
): Promise<{ items: OnboardingListItem[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.stage) qs.set("stage", params.stage);
  if (params.job_role) qs.set("job_role", params.job_role);
  const query = qs.toString();
  return client.request(`/onboarding${query ? `?${query}` : ""}`);
}

export function getOnboarding(
  client: ApiClient,
  id: string,
): Promise<OnboardingDetail> {
  return client.request<OnboardingDetail>(
    `/onboarding/${encodeURIComponent(id)}`,
  );
}

export function createOnboarding(
  client: ApiClient,
  input: Record<string, unknown>,
): Promise<OnboardingDetail> {
  return client.request<OnboardingDetail>("/onboarding", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function advanceOnboarding(
  client: ApiClient,
  id: string,
): Promise<OnboardingDetail> {
  return client.request<OnboardingDetail>(
    `/onboarding/${encodeURIComponent(id)}/advance`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function addOnboardingCredential(
  client: ApiClient,
  id: string,
  input: {
    type: string;
    number: string;
    issued_on?: string;
    expires_on?: string;
  },
): Promise<unknown> {
  return client.request(`/onboarding/${encodeURIComponent(id)}/credentials`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function completeOrientationItem(
  client: ApiClient,
  id: string,
  itemCode: string,
  note?: string,
): Promise<unknown> {
  return client.request(
    `/onboarding/${encodeURIComponent(id)}/orientation/${encodeURIComponent(itemCode)}/complete`,
    { method: "POST", body: JSON.stringify(note ? { note } : {}) },
  );
}

export function terminateOnboarding(
  client: ApiClient,
  id: string,
  reason: string,
): Promise<unknown> {
  return client.request(`/onboarding/${encodeURIComponent(id)}/terminate`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}
