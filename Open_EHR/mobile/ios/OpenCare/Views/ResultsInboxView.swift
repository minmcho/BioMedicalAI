import SwiftUI

/// Staff results inbox — GET /fhir/inbox?patient=. The clinician picks a
/// patient, the server returns that patient's Observation +
/// DiagnosticReport bundle, newest first. Staff roles only; the backend
/// returns 403 for viewer/patient, and the tab is hidden for them too.
struct ResultsInboxView: View {
    @EnvironmentObject private var session: Session
    @State private var patients: [FHIRResource] = []
    @State private var selectedPatientID: String?
    @State private var results: [FHIRResource] = []
    @State private var errorText: String?
    @State private var loading = false

    var body: some View {
        List {
            Section("Patient") {
                if patients.isEmpty {
                    Text("No patients loaded").foregroundStyle(.secondary)
                } else {
                    Picker("Patient", selection: $selectedPatientID) {
                        Text("Select…").tag(String?.none)
                        ForEach(patients) { patient in
                            Text(patient.displayTitle).tag(String?.some(patient.id ?? ""))
                        }
                    }
                }
            }
            if let errorText {
                Section { Text(errorText).foregroundStyle(.red) }
            }
            Section("Results") {
                if results.isEmpty {
                    Text(selectedPatientID == nil ? "Pick a patient to see results."
                                                   : "No results for this patient.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(results) { result in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(result.displayTitle)
                            Text([result.resourceType, result.displaySubtitle]
                                .filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("Results Inbox")
        .overlay { if loading { ProgressView() } }
        .task { await loadPatients() }
        .onChange(of: selectedPatientID) { _, newValue in
            Task { await loadResults(patientID: newValue) }
        }
    }

    private func loadPatients() async {
        do {
            patients = try await session.api.patients()
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    private func loadResults(patientID: String?) async {
        guard let patientID else { results = []; return }
        loading = true
        defer { loading = false }
        do {
            results = try await session.api.inbox(patientRef: "Patient/\(patientID)")
            errorText = nil
        } catch {
            results = []
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
