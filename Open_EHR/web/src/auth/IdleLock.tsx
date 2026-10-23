import { useCallback, useEffect, useRef, useState } from "react";
import {
  evaluateIdleLock,
  type IdlePhase,
  type SessionSecurityConfig,
} from "./sessionSecurity";
import { ModuleIcon } from "../components/ModuleIcon";

// Phase 17 (BD-54 / RTM CMP-33) — the web-shell enforcement of the
// published session-security policy. The hook tracks real user activity
// (pointer / keyboard / touch); while locked it stops tracking, so the
// grace clock runs from the lock moment. Locking is a UI state only:
// the session object (and the server session behind the token) is left
// untouched, which is what makes tap-and-go re-entry inside the grace
// window possible. Past the grace window the shell signs out.

const ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart"] as const;

export interface IdleLockState {
  phase: IdlePhase;
  locked: boolean;
  /** Tap-and-go re-entry: resume after the caller has re-verified the
   *  server session (App calls GET /security/session first). */
  reenter: () => void;
}

export function useIdleLock(
  config: SessionSecurityConfig,
  active: boolean,
  onExpired: () => void,
): IdleLockState {
  const [lastActivityAt, setLastActivityAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());
  const [dismissedLockAt, setDismissedLockAt] = useState<number | null>(null);
  const expiredFiredRef = useRef<number | null>(null);

  const evaluation = evaluateIdleLock(config, lastActivityAt, now);
  const locked =
    active &&
    evaluation.phase !== "active" &&
    evaluation.lockedAt !== dismissedLockAt;
  const phase: IdlePhase = locked ? evaluation.phase : "active";

  // Activity tracking: only while signed in, enabled and unlocked.
  useEffect(() => {
    if (!active || !config.enabled || locked) return;
    setLastActivityAt((prev) => prev ?? Date.now());
    const mark = () => setLastActivityAt(Date.now());
    for (const event of ACTIVITY_EVENTS) {
      window.addEventListener(event, mark, { passive: true });
    }
    return () => {
      for (const event of ACTIVITY_EVENTS) {
        window.removeEventListener(event, mark);
      }
    };
  }, [active, config.enabled, locked]);

  // 1s tick drives lock/grace transitions without activity.
  useEffect(() => {
    if (!active || !config.enabled) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active, config.enabled]);

  // Grace expiry fires the sign-out exactly once per lock episode.
  useEffect(() => {
    if (
      locked &&
      evaluation.phase === "locked-expired" &&
      evaluation.lockedAt !== null &&
      expiredFiredRef.current !== evaluation.lockedAt
    ) {
      expiredFiredRef.current = evaluation.lockedAt;
      onExpired();
    }
  }, [locked, evaluation.phase, evaluation.lockedAt, onExpired]);

  const reenter = useCallback(() => {
    if (evaluation.lockedAt !== null) {
      setDismissedLockAt(evaluation.lockedAt);
    }
    setLastActivityAt(Date.now());
    setNow(Date.now());
  }, [evaluation.lockedAt]);

  return { phase, locked, reenter };
}

/** The lock screen. In the re-entry window it offers tap-and-go resume
 *  (the caller re-verifies the server session first); after the grace
 *  window it offers only a fresh sign-in. */
export function SessionLockOverlay({
  phase,
  username,
  onReenter,
  onSignOut,
}: {
  phase: IdlePhase;
  username: string;
  onReenter: () => void;
  onSignOut: () => void;
}) {
  if (phase === "active") return null;
  const canReenter = phase === "locked-reentry";
  return (
    <div className="session-lock-overlay" role="alertdialog" aria-label="Session locked">
      <div className="card session-lock-card">
        {/* Rich Design Elevation (§8.17): shield glyph from the
            shared icon map above the heading. */}
        <span className="session-lock-icon" aria-hidden="true">
          <ModuleIcon name="shield" size={32} />
        </span>
        <h2>Session locked</h2>
        <p>
          {username ? <>{username}, your </> : "Your "}session was locked after a
          period of inactivity.
          {canReenter
            ? " Your work is still signed in — tap and go to re-enter."
            : " The re-entry window has passed, so a fresh sign-in is required."}
        </p>
        {canReenter ? (
          <button type="button" className="primary" onClick={onReenter}>
            Tap and go — re-enter
          </button>
        ) : (
          <button type="button" className="primary" onClick={onSignOut}>
            Sign in again
          </button>
        )}
      </div>
    </div>
  );
}
