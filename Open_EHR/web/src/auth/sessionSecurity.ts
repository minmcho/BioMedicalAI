// Phase 17 (BD-54 / RTM CMP-33) — session idle-lock + tap-and-go re-entry.
//
// The policy is published Studio config (backend kind=integration,
// key=session-security), consumed by the shell through runtimeConfig.
// This module is the pure policy logic so it is unit-testable without
// timers: given the config, the last-activity timestamp and "now", it
// decides whether the shell is active, locked-but-re-enterable (inside
// the grace window — the server session/token was NOT destroyed by the
// UI lock), or locked-and-expired (grace over: full sign-in required).

export interface SessionSecurityConfig {
  enabled: boolean;
  idleLockMinutes: number;
  reentryGraceMinutes: number;
}

/** Config-off default: no published policy (or a fetch failure) means
 *  the shell behaves exactly as it did before Phase 17. */
export const DEFAULT_SESSION_SECURITY: SessionSecurityConfig = {
  enabled: false,
  idleLockMinutes: 15,
  reentryGraceMinutes: 5,
};

function positiveNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : fallback;
}

export function parseSessionSecurity(payload: unknown): SessionSecurityConfig {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return DEFAULT_SESSION_SECURITY;
  }
  const p = payload as Record<string, unknown>;
  if (p.enabled !== true) return DEFAULT_SESSION_SECURITY;
  return {
    enabled: true,
    idleLockMinutes: positiveNumber(
      p.idleLockMinutes,
      DEFAULT_SESSION_SECURITY.idleLockMinutes,
    ),
    reentryGraceMinutes: positiveNumber(
      p.reentryGraceMinutes,
      DEFAULT_SESSION_SECURITY.reentryGraceMinutes,
    ),
  };
}

export type IdlePhase = "active" | "locked-reentry" | "locked-expired";

export interface IdleEvaluation {
  phase: IdlePhase;
  /** When the lock engaged (ms epoch), or null while active. The grace
   *  window is measured from this moment, not from last activity. */
  lockedAt: number | null;
}

/**
 * Evaluate the lock state.
 *
 * - disabled config, or no activity recorded yet -> active.
 * - idle for less than idleLockMinutes -> active.
 * - idle past idleLockMinutes: the lock engaged at
 *   lastActivityAt + idleLockMinutes; inside reentryGraceMinutes of
 *   that moment the phase is locked-reentry (tap-and-go may resume the
 *   untouched server session), afterwards locked-expired.
 */
export function evaluateIdleLock(
  config: SessionSecurityConfig,
  lastActivityAt: number | null,
  now: number,
): IdleEvaluation {
  if (!config.enabled || lastActivityAt === null) {
    return { phase: "active", lockedAt: null };
  }
  const idleMs = config.idleLockMinutes * 60_000;
  if (now - lastActivityAt < idleMs) {
    return { phase: "active", lockedAt: null };
  }
  const lockedAt = lastActivityAt + idleMs;
  const graceMs = config.reentryGraceMinutes * 60_000;
  return {
    phase: now - lockedAt <= graceMs ? "locked-reentry" : "locked-expired",
    lockedAt,
  };
}
