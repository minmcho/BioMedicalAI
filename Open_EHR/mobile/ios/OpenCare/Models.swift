import Foundation

// MARK: - FHIR-lite models
//
// Codable mirrors of the FHIR R4 shapes the OpenCare backend actually
// returns (contracts/fhir-resources.md). One unified `FHIRResource` struct
// with optional fields across resource types keeps decoding of mixed
// Bundles (inbox, portal results) simple; per-type structs were rejected
// because the inbox Bundle mixes Observation + DiagnosticReport entries.
// Unknown keys are ignored by Codable's default behaviour for structs with
// matching key names, and every field the views use is decoded explicitly.

struct Coding: Codable, Hashable {
    var system: String?
    var code: String?
    var display: String?
}

struct CodeableConcept: Codable, Hashable {
    var coding: [Coding]?
    var text: String?

    /// Best human-readable label: text, else first coding display/code.
    var label: String {
        if let text, !text.isEmpty { return text }
        if let first = coding?.first {
            return first.display ?? first.code ?? "—"
        }
        return "—"
    }
}

struct HumanName: Codable, Hashable {
    var use: String?
    var text: String?
    var family: String?
    var given: [String]?

    var label: String {
        if let text, !text.isEmpty { return text }
        let givenPart = (given ?? []).joined(separator: " ")
        return [givenPart, family ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
    }
}

struct Reference: Codable, Hashable {
    var reference: String?
    var display: String?
}

struct Quantity: Codable, Hashable {
    var value: Double?
    var unit: String?
    var code: String?

    var label: String {
        guard let value else { return "—" }
        let number = value == value.rounded() ? String(Int(value)) : String(value)
        return [number, unit ?? code ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
    }
}

struct Participant: Codable, Hashable {
    var actor: Reference?
    var status: String?
}

struct Meta: Codable, Hashable {
    var versionId: String?
}

/// Unified FHIR-lite resource (see header note). Fields not present in a
/// given resource type decode as nil.
struct FHIRResource: Codable, Identifiable, Hashable {
    var resourceType: String
    var id: String?
    var meta: Meta?
    var status: String?
    // Patient
    var name: [HumanName]?
    var gender: String?
    var birthDate: String?
    // Coded clinical resources (Observation, Condition, Procedure,
    // DiagnosticReport)
    var code: CodeableConcept?
    var subject: Reference?
    var valueQuantity: Quantity?
    var valueString: String?
    var effectiveDateTime: String?
    var issued: String?
    var conclusion: String?
    // MedicationRequest
    var medicationCodeableConcept: CodeableConcept?
    var medicationReference: Reference?
    var intent: String?
    // Appointment
    var start: String?
    var end: String?
    var participant: [Participant]?
    var description: String?

    var displayTitle: String {
        switch resourceType {
        case "Patient":
            return name?.first?.label ?? "Patient \(id ?? "")"
        case "Observation", "Condition", "Procedure", "DiagnosticReport":
            return code?.label ?? resourceType
        case "MedicationRequest":
            return medicationCodeableConcept?.label
                ?? medicationReference?.display
                ?? medicationReference?.reference
                ?? "Medication"
        case "Appointment":
            return description ?? "Appointment"
        default:
            return resourceType
        }
    }

    /// Secondary line for list rows: status + a type-specific detail.
    var displaySubtitle: String {
        var parts: [String] = []
        if let status { parts.append(status) }
        switch resourceType {
        case "Observation":
            if let valueQuantity { parts.append(valueQuantity.label) }
            else if let valueString { parts.append(valueString) }
            if let effectiveDateTime { parts.append(effectiveDateTime) }
        case "DiagnosticReport":
            if let issued { parts.append(issued) }
            if let conclusion { parts.append(conclusion) }
        case "MedicationRequest":
            if let intent { parts.append(intent) }
        case "Appointment":
            if let start { parts.append(start) }
        case "Patient":
            if let birthDate { parts.append("DOB \(birthDate)") }
            if let gender { parts.append(gender) }
        default:
            break
        }
        return parts.joined(separator: " · ")
    }
}

struct BundleEntry: Codable, Hashable {
    var resource: FHIRResource?
}

/// FHIR searchset Bundle as returned by every search/inbox/portal route.
struct FHIRBundle: Codable {
    var resourceType: String?
    var type: String?
    var total: Int?
    var entry: [BundleEntry]?

    var resources: [FHIRResource] {
        (entry ?? []).compactMap { $0.resource }
    }
}
