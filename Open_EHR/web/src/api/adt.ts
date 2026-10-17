// Appointment (ADT) status state machine. The single source of truth
// is the Studio workflow config (workflow/appointment-adt): its
// built-in default payload lives in backend/app/configstore.py, the
// server enforces the published config, and SchedulingPage derives its
// buttons from the published payload when it can be fetched. The map
// below is the offline fallback, identical to the server's default.

export const APPOINTMENT_TRANSITIONS: Record<string, string[]> = {
  proposed: ["booked", "cancelled"],
  pending: ["booked", "cancelled"],
  booked: ["arrived", "cancelled"],
  arrived: ["fulfilled", "cancelled"],
  "checked-in": ["fulfilled", "cancelled"],
  fulfilled: [],
  cancelled: [],
  noshow: [],
};

/** Build a transition map from a published workflow payload
 *  ({transitions: [{from, to}]}), or null when the payload carries no
 *  usable transitions (caller keeps the built-in map). */
export function machineFromWorkflowPayload(
  payload: unknown,
): Record<string, string[]> | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const transitions = (payload as { transitions?: unknown }).transitions;
  if (!Array.isArray(transitions)) return null;
  const machine: Record<string, string[]> = {};
  for (const t of transitions) {
    if (!t || typeof t !== "object") continue;
    const { from, to } = t as { from?: unknown; to?: unknown };
    if (typeof from !== "string" || typeof to !== "string") continue;
    (machine[from] ??= []).push(to);
  }
  return Object.keys(machine).length > 0 ? machine : null;
}

export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "fulfilled",
  "cancelled",
  "noshow",
]);

export function isTerminal(status: string | undefined): boolean {
  return status !== undefined && TERMINAL_STATUSES.has(status);
}

export function allowedTransitions(
  status: string | undefined,
  machine: Record<string, string[]> = APPOINTMENT_TRANSITIONS,
): string[] {
  if (!status) return [];
  return machine[status] ?? [];
}

export function canTransition(
  from: string | undefined,
  to: string,
  machine: Record<string, string[]> = APPOINTMENT_TRANSITIONS,
): boolean {
  return allowedTransitions(from, machine).includes(to);
}

export const TRANSITION_LABELS: Record<string, string> = {
  booked: "Book",
  arrived: "Arrive",
  fulfilled: "Fulfil",
  cancelled: "Cancel",
  noshow: "No-show",
};
