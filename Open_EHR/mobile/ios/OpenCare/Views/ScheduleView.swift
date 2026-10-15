import SwiftUI

/// Schedule — GET /fhir/Appointment. v1 is read-only on mobile: the
/// appointment status (booked / arrived / fulfilled / cancelled …) is
/// displayed, and ADT transitions are performed through the API/web by
/// staff workflows. The backend's patient scoping applies automatically
/// to patient tokens.
struct ScheduleView: View {
    @EnvironmentObject private var session: Session
    @State private var appointments: [FHIRResource] = []
    @State private var errorText: String?
    @State private var loading = false

    var body: some View {
        Group {
            if loading && appointments.isEmpty {
                ProgressView("Loading schedule…")
            } else if let errorText, appointments.isEmpty {
                ContentUnavailableView("Could not load schedule",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else {
                List(appointments) { appointment in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(appointment.displayTitle).font(.headline)
                            if let start = appointment.start {
                                Text(start).font(.subheadline).foregroundStyle(.secondary)
                            }
                            if let patientActor = appointment.participant?
                                .compactMap({ $0.actor?.reference })
                                .first(where: { $0.hasPrefix("Patient/") }) {
                                Text(patientActor)
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        if let status = appointment.status {
                            Text(status)
                                .font(.caption).bold()
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .background(Capsule().fill(Color.secondary.opacity(0.15)))
                        }
                    }
                }
                .overlay {
                    if appointments.isEmpty {
                        ContentUnavailableView("No appointments",
                                               systemImage: "calendar.badge.exclamationmark",
                                               description: Text("Nothing is scheduled."))
                    }
                }
            }
        }
        .navigationTitle("Schedule")
        .task { await reload() }
        .refreshable { await reload() }
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        do {
            appointments = try await session.api.appointments()
            errorText = nil
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
