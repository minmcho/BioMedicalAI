import Combine
import Foundation

/// Observable auth state. Dev-mode sign-in stores the role's seeded dev
/// token (see Config.swift / DevRole) in the Keychain; production replaces
/// this with an OIDC login against the same backend (AUTH_MODE=oidc).
@MainActor
final class Session: ObservableObject {
    @Published private(set) var role: DevRole?
    @Published private(set) var token: String?

    let api: APIClient

    var isSignedIn: Bool { token != nil && role != nil }
    var username: String { role.map { "dev-\($0.rawValue)" } ?? "" }

    init() {
        api = APIClient()
        if let savedToken = KeychainStore.load(key: "token"),
           let savedRole = KeychainStore.load(key: "role"),
           let role = DevRole(rawValue: savedRole) {
            self.token = savedToken
            self.role = role
        }
        api.tokenProvider = { [weak self] in self?.token }
    }

    func signIn(as role: DevRole) {
        self.role = role
        self.token = role.devToken
        KeychainStore.save(key: "token", value: role.devToken)
        KeychainStore.save(key: "role", value: role.rawValue)
    }

    func signOut() {
        role = nil
        token = nil
        KeychainStore.delete(key: "token")
        KeychainStore.delete(key: "role")
    }
}
