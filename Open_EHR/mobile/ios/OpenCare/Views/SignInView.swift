import SwiftUI

/// Dev-mode sign-in: pick a role, get that role's seeded backend token.
/// There is no password in dev mode — the backend's AUTH_MODE=dev maps
/// bearer tokens to users. Production uses OIDC (see README).
struct SignInView: View {
    @EnvironmentObject private var session: Session
    @State private var selected: DevRole = .clinician

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Role", selection: $selected) {
                        ForEach(DevRole.allCases) { role in
                            Text(role.displayName).tag(role)
                        }
                    }
                    .pickerStyle(.inline)
                } header: {
                    Text("Development sign-in")
                } footer: {
                    Text("Uses the backend's seeded dev tokens "
                         + "(backend/app/security.py). The patient role is "
                         + "linked to Patient/pat-1 and sees only that record.")
                }
                Section {
                    Button("Sign in as \(selected.displayName)") {
                        session.signIn(as: selected)
                    }
                }
                Section("Backend") {
                    LabeledContent("API base URL", value: Config.apiBaseURL.absoluteString)
                }
            }
            .navigationTitle("OpenCare")
        }
    }
}

#Preview {
    // Preview fixture — UI preview only, never used at runtime.
    SignInView().environmentObject(Session())
}
