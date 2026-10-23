import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionLockOverlay, useIdleLock } from "./IdleLock";
import {
  DEFAULT_SESSION_SECURITY,
  evaluateIdleLock,
  parseSessionSecurity,
  type SessionSecurityConfig,
} from "./sessionSecurity";

// Phase 17 (BD-54 / CMP-33): idle-lock + tap-and-go re-entry from the
// published session-security config, enforced in the web shell.

afterEach(cleanup);

const ENABLED: SessionSecurityConfig = {
  enabled: true,
  idleLockMinutes: 15,
  reentryGraceMinutes: 5,
};
const MIN = 60_000;

describe("parseSessionSecurity", () => {
  it("config-off: absent, malformed or disabled payloads never lock", () => {
    expect(parseSessionSecurity(null)).toEqual(DEFAULT_SESSION_SECURITY);
    expect(parseSessionSecurity(undefined).enabled).toBe(false);
    expect(parseSessionSecurity("nope").enabled).toBe(false);
    expect(parseSessionSecurity({ enabled: false, idleLockMinutes: 1 }).enabled).toBe(false);
    expect(parseSessionSecurity({ idleLockMinutes: 1 }).enabled).toBe(false);
  });

  it("an enabled payload carries its timers, with sane fallbacks", () => {
    expect(
      parseSessionSecurity({ enabled: true, idleLockMinutes: 10, reentryGraceMinutes: 2 }),
    ).toEqual({ enabled: true, idleLockMinutes: 10, reentryGraceMinutes: 2 });
    expect(
      parseSessionSecurity({ enabled: true, idleLockMinutes: -3, reentryGraceMinutes: "x" }),
    ).toEqual({ enabled: true, idleLockMinutes: 15, reentryGraceMinutes: 5 });
  });
});

describe("evaluateIdleLock", () => {
  it("config-off never locks, however long the idle time", () => {
    const out = evaluateIdleLock(DEFAULT_SESSION_SECURITY, 0, 10_000 * MIN);
    expect(out.phase).toBe("active");
    expect(out.lockedAt).toBeNull();
  });

  it("active before the idle limit, locked-reentry inside grace, expired after", () => {
    const t0 = 1_000_000;
    expect(evaluateIdleLock(ENABLED, t0, t0 + 14 * MIN).phase).toBe("active");
    const locked = evaluateIdleLock(ENABLED, t0, t0 + 16 * MIN);
    expect(locked.phase).toBe("locked-reentry");
    expect(locked.lockedAt).toBe(t0 + 15 * MIN);
    expect(evaluateIdleLock(ENABLED, t0, t0 + 20 * MIN).phase).toBe("locked-reentry");
    expect(evaluateIdleLock(ENABLED, t0, t0 + 21 * MIN).phase).toBe("locked-expired");
  });

  it("no activity recorded yet is active", () => {
    expect(evaluateIdleLock(ENABLED, null, Date.now()).phase).toBe("active");
  });
});

function Harness({
  config,
  onExpired,
}: {
  config: SessionSecurityConfig;
  onExpired: () => void;
}) {
  const idle = useIdleLock(config, true, onExpired);
  return (
    <div>
      <span data-testid="phase">{idle.phase}</span>
      <span data-testid="locked">{String(idle.locked)}</span>
      <button type="button" onClick={idle.reenter}>
        reenter-hook
      </button>
    </div>
  );
}

describe("useIdleLock", () => {
  it("locks after the idle limit and tap-and-go re-entry unlocks inside grace", () => {
    vi.useFakeTimers();
    try {
      const onExpired = vi.fn();
      render(<Harness config={{ enabled: true, idleLockMinutes: 1, reentryGraceMinutes: 5 }} onExpired={onExpired} />);
      expect(screen.getByTestId("locked").textContent).toBe("false");
      act(() => {
        vi.advanceTimersByTime(61_000);
      });
      expect(screen.getByTestId("locked").textContent).toBe("true");
      expect(screen.getByTestId("phase").textContent).toBe("locked-reentry");
      expect(onExpired).not.toHaveBeenCalled();
      // Tap-and-go: re-entry resumes without destroying the session.
      fireEvent.click(screen.getByText("reenter-hook"));
      expect(screen.getByTestId("locked").textContent).toBe("false");
      expect(onExpired).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("past the grace window the session is expired (sign-out fires once)", () => {
    vi.useFakeTimers();
    try {
      const onExpired = vi.fn();
      render(<Harness config={{ enabled: true, idleLockMinutes: 1, reentryGraceMinutes: 1 }} onExpired={onExpired} />);
      act(() => {
        vi.advanceTimersByTime(121_000);
      });
      expect(screen.getByTestId("phase").textContent).toBe("locked-expired");
      expect(onExpired).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("config-off: the hook never locks", () => {
    vi.useFakeTimers();
    try {
      const onExpired = vi.fn();
      render(<Harness config={DEFAULT_SESSION_SECURITY} onExpired={onExpired} />);
      act(() => {
        vi.advanceTimersByTime(60 * MIN);
      });
      expect(screen.getByTestId("locked").textContent).toBe("false");
      expect(onExpired).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("SessionLockOverlay", () => {
  it("re-entry window offers tap-and-go; expired offers fresh sign-in only", () => {
    const onReenter = vi.fn();
    const onSignOut = vi.fn();
    const { rerender } = render(
      <SessionLockOverlay phase="locked-reentry" username="dev-clinician" onReenter={onReenter} onSignOut={onSignOut} />,
    );
    fireEvent.click(screen.getByText("Tap and go — re-enter"));
    expect(onReenter).toHaveBeenCalledTimes(1);
    rerender(
      <SessionLockOverlay phase="locked-expired" username="dev-clinician" onReenter={onReenter} onSignOut={onSignOut} />,
    );
    expect(screen.queryByText("Tap and go — re-enter")).toBeNull();
    fireEvent.click(screen.getByText("Sign in again"));
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("renders nothing while active", () => {
    const { container } = render(
      <SessionLockOverlay phase="active" username="x" onReenter={() => {}} onSignOut={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
