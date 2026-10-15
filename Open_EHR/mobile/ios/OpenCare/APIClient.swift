import Foundation

/// Typed errors surfaced from the backend so views can show honest states
/// (a 403 is a permissions fact, a 404 under patient scoping means
/// "not yours / not found" — the backend deliberately does not disclose).
enum APIError: Error, LocalizedError {
    case notSignedIn
    case unauthorized            // 401
    case forbidden               // 403
    case notFound                // 404
    case server(status: Int, detail: String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .notSignedIn: return "Not signed in."
        case .unauthorized: return "Session rejected (401). Sign in again."
        case .forbidden: return "Your role may not do this (403)."
        case .notFound: return "Not found (404)."
        case .server(let status, let detail): return "Server \(status): \(detail)"
        case .transport(let message): return "Network error: \(message)"
        }
    }
}

/// Async URLSession client for the OpenCare FHIR API. Every method maps to
/// a real backend route (backend/app/fhir.py); there are no local fixtures
/// in the runtime path.
final class APIClient {
    private let baseURL: URL
    private let session: URLSession
    /// Bearer-token provider, wired to the Session object at app start.
    var tokenProvider: () -> String?

    init(baseURL: URL = Config.apiBaseURL, tokenProvider: @escaping () -> String? = { nil }) {
        self.baseURL = baseURL
        self.tokenProvider = tokenProvider
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - Generic GET

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        guard let token = tokenProvider(), !token.isEmpty else { throw APIError.notSignedIn }
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)
        if !query.isEmpty { components?.queryItems = query }
        guard let url = components?.url else { throw APIError.transport("Bad URL for \(path)") }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("No HTTP response")
        }
        switch http.statusCode {
        case 200...299:
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                throw APIError.transport("Could not decode response: \(error.localizedDescription)")
            }
        case 401: throw APIError.unauthorized
        case 403: throw APIError.forbidden
        case 404: throw APIError.notFound
        default:
            let detail = String(data: data, encoding: .utf8) ?? ""
            throw APIError.server(status: http.statusCode, detail: detail)
        }
    }

    private func search(_ resourceType: String, patient: String? = nil,
                        extra: [URLQueryItem] = []) async throws -> [FHIRResource] {
        var query = extra
        if let patient { query.append(URLQueryItem(name: "patient", value: patient)) }
        let bundle: FHIRBundle = try await get("fhir/\(resourceType)", query: query)
        return bundle.resources
    }

    // MARK: - Routes used by the views

    /// GET /fhir/Patient — under a patient token the backend force-scopes
    /// this to the caller's own record.
    func patients() async throws -> [FHIRResource] {
        try await search("Patient")
    }

    /// GET /fhir/Patient/{id}
    func patient(id: String) async throws -> FHIRResource {
        try await get("fhir/Patient/\(id)")
    }

    /// GET /fhir/Condition?patient=
    func conditions(patientRef: String) async throws -> [FHIRResource] {
        try await search("Condition", patient: patientRef)
    }

    /// GET /fhir/MedicationRequest?patient=
    func medications(patientRef: String) async throws -> [FHIRResource] {
        try await search("MedicationRequest", patient: patientRef)
    }

    /// GET /fhir/Observation?patient=
    func observations(patientRef: String) async throws -> [FHIRResource] {
        try await search("Observation", patient: patientRef)
    }

    /// GET /fhir/inbox?patient= — staff roles only (403 for viewer/patient).
    func inbox(patientRef: String) async throws -> [FHIRResource] {
        let bundle: FHIRBundle = try await get(
            "fhir/inbox", query: [URLQueryItem(name: "patient", value: patientRef)])
        return bundle.resources
    }

    /// GET /portal/my-results — patient role only; the backend scopes it to
    /// the caller's own patient_ref, no parameter is (or can be) supplied.
    func myResults() async throws -> [FHIRResource] {
        let bundle: FHIRBundle = try await get("portal/my-results")
        return bundle.resources
    }

    /// GET /fhir/Appointment (optionally ?patient=) — patient tokens are
    /// force-scoped server-side; the status is displayed read-only in v1.
    func appointments(patientRef: String? = nil) async throws -> [FHIRResource] {
        try await search("Appointment", patient: patientRef)
    }
}
