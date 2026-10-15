import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { API_BASE, ApiClient } from "../api/client";
import { loadSession, saveSession, type Session } from "./session";

interface AuthContextValue {
  session: Session | null;
  client: ApiClient;
  signIn: (session: Session) => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => loadSession());

  const value = useMemo<AuthContextValue>(() => {
    const token = session?.token ?? null;
    const client = new ApiClient(API_BASE, () => token);
    return {
      session,
      client,
      signIn: (s: Session) => {
        saveSession(s);
        setSession(s);
      },
      signOut: () => {
        saveSession(null);
        setSession(null);
      },
    };
  }, [session]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
