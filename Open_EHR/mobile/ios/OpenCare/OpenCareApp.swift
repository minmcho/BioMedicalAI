import SwiftUI

@main
struct OpenCareApp: App {
    @StateObject private var session = Session()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
        }
    }
}

/// Switches between sign-in and the signed-in tab shell. Tabs are
/// role-aware because the backend enforces the same rules server-side:
/// the inbox is staff-only and the portal is patient-only; hiding them
/// here is presentation, the API enforces the real boundary.
struct RootView: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        if session.isSignedIn {
            TabView {
                NavigationStack { PatientListView() }
                    .tabItem { Label("Patients", systemImage: "person.3") }
                NavigationStack { ScheduleView() }
                    .tabItem { Label("Schedule", systemImage: "calendar") }
                if let role = session.role, role.canUseInbox {
                    NavigationStack { ResultsInboxView() }
                        .tabItem { Label("Inbox", systemImage: "tray.full") }
                }
                if session.role == .patient {
                    NavigationStack { PortalResultsView() }
                        .tabItem { Label("My Results", systemImage: "doc.text") }
                }
                NavigationStack { SignOutView() }
                    .tabItem { Label("Account", systemImage: "person.circle") }
            }
        } else {
            SignInView()
        }
    }
}

struct SignOutView: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        List {
            Section("Signed in") {
                LabeledContent("Role", value: session.role?.displayName ?? "—")
                LabeledContent("User", value: session.username)
                LabeledContent("API", value: Config.apiBaseURL.absoluteString)
            }
            Section {
                Button("Sign out", role: .destructive) { session.signOut() }
            }
        }
        .navigationTitle("Account")
    }
}
