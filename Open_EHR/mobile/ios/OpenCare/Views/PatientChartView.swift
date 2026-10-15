import SwiftUI

/// One patient's chart: problems (Condition), medications
/// (MedicationRequest), and recent observations — three live searches
/// scoped by ?patient=Patient/{id}.
struct PatientChartView: View {
    @EnvironmentObject private var session: Session
    let patient: FHIRResource

    @State private var conditions: [FHIRResource] = []
    @State private var medications: [FHIRResource] = []
    @State private var observations: [FHIRResource] = []
    @State private var errorText: String?
    @State private var loading = false

    private var patientRef: String { "Patient/\(patient.id ?? "")" }

    var body: some View {
        List {
            Section("Patient") {
                LabeledContent("Name", value: patient.displayTitle)
                if let birthDate = patient.birthDate {
                    LabeledContent("Date of birth", value: birthDate)
                }
                if let gender = patient.gender {
                    LabeledContent("Gender", value: gender)
                }
                LabeledContent("Reference", value: patientRef)
            }
            if let errorText {
                Section {
                    Text(errorText).foregroundStyle(.red)
                }
            }
            chartSection("Problems", items: conditions)
            chartSection("Medications", items: medications)
            chartSection("Observations", items: observations)
        }
        .navigationTitle(patient.displayTitle)
        .task { await reload() }
        .refreshable { await reload() }
        .overlay { if loading && conditions.isEmpty { ProgressView() } }
    }

    @ViewBuilder
    private func chartSection(_ title: String, items: [FHIRResource]) -> some View {
        Section(title) {
            if items.isEmpty {
                Text("None recorded").foregroundStyle(.secondary)
            } else {
                ForEach(items) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.displayTitle)
                        if !item.displaySubtitle.isEmpty {
                            Text(item.displaySubtitle)
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        do {
            async let newConditions = session.api.conditions(patientRef: patientRef)
            async let newMedications = session.api.medications(patientRef: patientRef)
            async let newObservations = session.api.observations(patientRef: patientRef)
            conditions = try await newConditions
            medications = try await newMedications
            observations = try await newObservations
            errorText = nil
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
