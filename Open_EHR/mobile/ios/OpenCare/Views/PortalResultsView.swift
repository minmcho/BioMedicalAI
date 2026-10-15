import SwiftUI

/// Patient portal — GET /portal/my-results. No patient parameter exists on
/// this route: the backend derives the patient from the caller's token
/// (patient-token is linked to Patient/pat-1) and returns only that
/// patient's Observation + DiagnosticReport resources, newest first.
struct PortalResultsView: View {
    @EnvironmentObject private var session: Session
    @State private var results: [FHIRResource] = []
    @State private var errorText: String?
    @State private var loading = false

    var body: some View {
        Group {
            if loading && results.isEmpty {
                ProgressView("Loading your results…")
            } else if let errorText, results.isEmpty {
                ContentUnavailableView("Could not load results",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else {
                List(results) { result in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(result.displayTitle).font(.headline)
                        Text([result.resourceType, result.displaySubtitle]
                            .filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                .overlay {
                    if results.isEmpty {
                        ContentUnavailableView("No results yet",
                                               systemImage: "doc.text.magnifyingglass",
                                               description: Text("When results are filed, they appear here."))
                    }
                }
            }
        }
        .navigationTitle("My Results")
        .task { await reload() }
        .refreshable { await reload() }
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        do {
            results = try await session.api.myResults()
            errorText = nil
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
