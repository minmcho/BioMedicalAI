// Appointment (ADT) status state machine — mirrors
// APPOINTMENT_TRANSITIONS in backend/app/fhir.py and
// contracts/fhir-resources.md. fulfilled / cancelled / noshow are terminal.

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

export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "fulfilled",
  "cancelled",
  "noshow",
]);

export function isTerminal(status: string | undefined): boolean {
  return status !== undefined && TERMINAL_STATUSES.has(status);
}

export function allowedTransitions(status: string | undefined): string[] {
  if (!status) return [];
  return APPOINTMENT_TRANSITIONS[status] ?? [];
}

export function canTransition(from: string | undefined, to: string): boolean {
  return allowedTransitions(from).includes(to);
}

export const TRANSITION_LABELS: Record<string, string> = {
  booked: "Book",
  arrived: "Arrive",
  fulfilled: "Fulfil",
  cancelled: "Cancel",
  noshow: "No-show",
};
