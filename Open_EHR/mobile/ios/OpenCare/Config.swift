import Foundation

/// App configuration. The backend base URL comes from the Info.plist key
/// `API_BASE_URL` so the same build can point at a laptop, a LAN host, or a
/// deployed server without code changes. Default: http://localhost:8000
/// (correct for the iOS Simulator talking to a backend on the same Mac;
/// on a physical device use the Mac's LAN address instead).
enum Config {
    static var apiBaseURL: URL {
        if let raw = Bundle.main.object(forInfoDictionaryKey: "API_BASE_URL") as? String,
           let url = URL(string: raw) {
            return url
        }
        return URL(string: "http://localhost:8000")!
    }
}

/// Dev-mode sign-in identities. These match the backend's seeded dev tokens
/// (backend/app/security.py, AUTH_MODE=dev) exactly. Production builds use
/// AUTH_MODE=oidc on the server and a real OIDC flow; the dev picker exists
/// so every role's scoping can be exercised in development.
enum DevRole: String, CaseIterable, Identifiable {
    case admin
    case configurator
    case clinician
    case viewer
    case patient

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .admin: return "Admin"
        case .configurator: return "Configurator"
        case .clinician: return "Clinician"
        case .viewer: return "Viewer"
        case .patient: return "Patient (pat-1)"
        }
    }

    /// Dev bearer token for this role (backend DEFAULT_DEV_TOKENS).
    var devToken: String {
        switch self {
        case .admin: return "admin-token"
        case .configurator: return "config-token"
        case .clinician: return "clinician-token"
        case .viewer: return "viewer-token"
        case .patient: return "patient-token"
        }
    }

    /// Staff roles may use the results inbox (backend INBOX_ROLES).
    var canUseInbox: Bool {
        switch self {
        case .admin, .configurator, .clinician: return true
        case .viewer, .patient: return false
        }
    }
}
