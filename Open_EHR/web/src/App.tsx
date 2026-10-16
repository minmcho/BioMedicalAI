import { useEffect, useState } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { canAccess, homePageFor, navItemsFor, type PageKey } from "./auth/session";
import { BillingPage } from "./pages/BillingPage";
import { ChartPage } from "./pages/ChartPage";
import { EdBoardPage } from "./pages/EdBoardPage";
import { InboxPage } from "./pages/InboxPage";
import { LabPage } from "./pages/LabPage";
import { LoginPage } from "./pages/LoginPage";
import { PatientsPage } from "./pages/PatientsPage";
import { PharmacyPage } from "./pages/PharmacyPage";
import { PopHealthPage } from "./pages/PopHealthPage";
import { PortalPage } from "./pages/PortalPage";
import { RadiologyPage } from "./pages/RadiologyPage";
import { SchedulingPage } from "./pages/SchedulingPage";
import { StudioPage } from "./pages/StudioPage";
import { SurgeryBoardPage } from "./pages/SurgeryBoardPage";

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
        {page === "lab" && <LabPage client={client} role={session.role} />}
        {page === "pophealth" && <PopHealthPage client={client} role={session.role} />}
        {page === "billing" && <BillingPage client={client} role={session.role} />}
        {page === "pharmacy" && <PharmacyPage client={client} role={session.role} />}
        {page === "ed" && <EdBoardPage client={client} role={session.role} />}
        {page === "radiology" && <RadiologyPage client={client} role={session.role} />}
        {page === "surgery" && <SurgeryBoardPage client={client} role={session.role} />}
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
