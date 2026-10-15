import { useEffect, useState } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { canAccess, homePageFor, navItemsFor, type PageKey } from "./auth/session";
import { ChartPage } from "./pages/ChartPage";
import { InboxPage } from "./pages/InboxPage";
import { LoginPage } from "./pages/LoginPage";
import { PatientsPage } from "./pages/PatientsPage";
import { PortalPage } from "./pages/PortalPage";
import { SchedulingPage } from "./pages/SchedulingPage";
import { StudioPage } from "./pages/StudioPage";

function Shell() {
  const { session, client, signOut } = useAuth();
  const [page, setPage] = useState<PageKey>("patients");
  const [chartPatientId, setChartPatientId] = useState<string | null>(null);

  // Land each role on its home page and never sit on a page the role
  // cannot access (route guarding).
  useEffect(() => {
    if (session && !canAccess(session.role, page)) {
      setPage(homePageFor(session.role));
    }
  }, [session, page]);

  if (!session) return <LoginPage />;

  const nav = navItemsFor(session.role);

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1 className="brand">OpenCare</h1>
        <p className="user-line">
          {session.username}
          <br />
          <span className="badge">{session.role}</span>
        </p>
        <nav aria-label="Primary">
          {nav.map((item) => (
            <button
              key={item.key}
              type="button"
              className={page === item.key || (item.key === "patients" && page === "chart") ? "nav active" : "nav"}
              onClick={() => setPage(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <button type="button" className="nav signout" onClick={signOut}>
          Sign out
        </button>
      </aside>

      <main className="content">
        {page === "patients" && (
          <PatientsPage
            client={client}
            onOpenChart={(id) => {
              setChartPatientId(id);
              setPage("chart");
            }}
          />
        )}
        {page === "chart" &&
          (chartPatientId ? (
            <>
              <button type="button" className="linklike" onClick={() => setPage("patients")}>
                ← Back to patients
              </button>
              <ChartPage client={client} patientId={chartPatientId} />
            </>
          ) : (
            <PatientsPage
              client={client}
              onOpenChart={(id) => {
                setChartPatientId(id);
                setPage("chart");
              }}
            />
          ))}
        {page === "inbox" && <InboxPage client={client} />}
        {page === "scheduling" && <SchedulingPage client={client} />}
        {page === "studio" && <StudioPage client={client} />}
        {page === "portal" && <PortalPage client={client} />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
