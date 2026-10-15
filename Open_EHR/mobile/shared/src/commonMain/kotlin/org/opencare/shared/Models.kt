package org.opencare.shared

import kotlinx.serialization.Serializable

/**
 * Canonical FHIR-lite contract models (KMP commonMain).
 *
 * These definitions are the shared contract for OpenCare clients: the
 * Android app mirrors them with kotlinx.serialization data classes in
 * its own module, and the iOS app mirrors them with Codable structs
 * (mobile/ios/OpenCare/Models.swift). Field names are FHIR R4 JSON keys
 * exactly as the backend returns them (contracts/fhir-resources.md).
 * One unified [FHIRResource] with nullable fields across types keeps
 * mixed Bundles (inbox, portal results) simple on every platform.
 */
@Serializable
public data class Coding(
    val system: String? = null,
    val code: String? = null,
    val display: String? = null,
)

@Serializable
public data class CodeableConcept(
    val coding: List<Coding>? = null,
    val text: String? = null,
) {
    /** Best human-readable label: text, else first coding display/code. */
    public val label: String
        get() = when {
            !text.isNullOrEmpty() -> text
            else -> coding?.firstOrNull()?.let { it.display ?: it.code } ?: "—"
        }
}

@Serializable
public data class HumanName(
    val use: String? = null,
    val text: String? = null,
    val family: String? = null,
    val given: List<String>? = null,
) {
    public val label: String
        get() = when {
            !text.isNullOrEmpty() -> text
            else -> listOf(given?.joinToString(" ") ?: "", family ?: "")
                .filter { it.isNotEmpty() }.joinToString(" ")
        }
}

@Serializable
public data class Reference(
    val reference: String? = null,
    val display: String? = null,
)

@Serializable
public data class Quantity(
    val value: Double? = null,
    val unit: String? = null,
    val code: String? = null,
)

@Serializable
public data class Participant(
    val actor: Reference? = null,
    val status: String? = null,
)

@Serializable
public data class Meta(
    val versionId: String? = null,
)

@Serializable
public data class FHIRResource(
    val resourceType: String,
    val id: String? = null,
    val meta: Meta? = null,
    val status: String? = null,
    // Patient
    val name: List<HumanName>? = null,
    val gender: String? = null,
    val birthDate: String? = null,
    // Coded clinical resources
    val code: CodeableConcept? = null,
    val subject: Reference? = null,
    val valueQuantity: Quantity? = null,
    val valueString: String? = null,
    val effectiveDateTime: String? = null,
    val issued: String? = null,
    val conclusion: String? = null,
    // MedicationRequest
    val medicationCodeableConcept: CodeableConcept? = null,
    val medicationReference: Reference? = null,
    val intent: String? = null,
    // Appointment
    val start: String? = null,
    val end: String? = null,
    val participant: List<Participant>? = null,
    val description: String? = null,
)

@Serializable
public data class BundleEntry(
    val resource: FHIRResource? = null,
)

@Serializable
public data class FHIRBundle(
    val resourceType: String? = null,
    val type: String? = null,
    val total: Int? = null,
    val entry: List<BundleEntry>? = null,
) {
    public val resources: List<FHIRResource>
        get() = entry?.mapNotNull { it.resource } ?: emptyList()
}
