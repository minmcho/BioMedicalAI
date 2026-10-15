import SwiftUI

/// GET /fhir/Patient — live list from the API. Under a patient token the
/// backend returns only the caller's own record, so this list is the
/// chart directory for staff and a single-row list for patients.
struct PatientListView: View {
    @EnvironmentObject private var session: Session
    @State private var patients: [FHIRResource] = []
    @State private var errorText: String?
    @State private var loading = false

    var body: some View {
        Group {
            if loading && patients.isEmpty {
                ProgressView("Loading patients…")
            } else if let errorText, patients.isEmpty {
                ContentUnavailableView("Could not load patients",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else {
                List(patients) { patient in
                    NavigationLink(value: patient) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(patient.displayTitle).font(.headline)
                            if !patient.displaySubtitle.isEmpty {
                                Text(patient.displaySubtitle)
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                .overlay {
                    if patients.isEmpty {
                        ContentUnavailableView("No patients",
                                               systemImage: "person.slash",
                                               description: Text("The server returned an empty list."))
                    }
                }
            }
        }
        .navigationTitle("Patients")
        .navigationDestination(for: FHIRResource.self) { patient in
            PatientChartView(patient: patient)
        }
        .task { await reload() }
        .refreshable { await reload() }
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        do {
            patients = try await session.api.patients()
            errorText = nil
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
