package org.opencare.ehr

import kotlinx.serialization.Serializable

/**
 * FHIR-lite models — kotlinx.serialization mirrors of the iOS Codable
 * models (mobile/ios/OpenCare/Models.swift) and of the shapes defined in
 * mobile/shared (the KMP contract module). One unified [FHIRResource] with
 * nullable fields across resource types keeps mixed Bundles (inbox,
 * portal results) simple to decode. Unknown keys are ignored (see the
 * Json configuration in ApiClient).
 */
@Serializable
data class Coding(
    val system: String? = null,
    val code: String? = null,
    val display: String? = null,
)

@Serializable
data class CodeableConcept(
    val coding: List<Coding>? = null,
    val text: String? = null,
) {
    val label: String
        get() = when {
            !text.isNullOrEmpty() -> text
            else -> coding?.firstOrNull()?.let { it.display ?: it.code } ?: "—"
        }
}

@Serializable
data class HumanName(
    val use: String? = null,
    val text: String? = null,
    val family: String? = null,
    val given: List<String>? = null,
) {
    val label: String
        get() = when {
            !text.isNullOrEmpty() -> text
            else -> listOf(given?.joinToString(" ") ?: "", family ?: "")
                .filter { it.isNotEmpty() }.joinToString(" ")
        }
}

@Serializable
data class Reference(
    val reference: String? = null,
    val display: String? = null,
)

@Serializable
data class Quantity(
    val value: Double? = null,
    val unit: String? = null,
    val code: String? = null,
) {
    val label: String
        get() {
            val v = value ?: return "—"
            val number = if (v == Math.floor(v)) v.toInt().toString() else v.toString()
            return listOf(number, unit ?: code ?: "").filter { it.isNotEmpty() }.joinToString(" ")
        }
}

@Serializable
data class Participant(
    val actor: Reference? = null,
    val status: String? = null,
)

@Serializable
data class Meta(
    val versionId: String? = null,
)

@Serializable
data class FHIRResource(
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
) {
    val displayTitle: String
        get() = when (resourceType) {
            "Patient" -> name?.firstOrNull()?.label?.takeIf { it.isNotEmpty() } ?: "Patient ${id ?: ""}"
            "Observation", "Condition", "Procedure", "DiagnosticReport" -> code?.label ?: resourceType
            "MedicationRequest" -> medicationCodeableConcept?.label
                ?: medicationReference?.display
                ?: medicationReference?.reference
                ?: "Medication"
            "Appointment" -> description ?: "Appointment"
            else -> resourceType
        }

    val displaySubtitle: String
        get() {
            val parts = mutableListOf<String>()
            status?.let { parts.add(it) }
            when (resourceType) {
                "Observation" -> {
                    valueQuantity?.let { parts.add(it.label) }
                        ?: valueString?.let { parts.add(it) }
                    effectiveDateTime?.let { parts.add(it) }
                }
                "DiagnosticReport" -> {
                    issued?.let { parts.add(it) }
                    conclusion?.let { parts.add(it) }
                }
                "MedicationRequest" -> intent?.let { parts.add(it) }
                "Appointment" -> start?.let { parts.add(it) }
                "Patient" -> {
                    birthDate?.let { parts.add("DOB $it") }
                    gender?.let { parts.add(it) }
                }
            }
            return parts.joinToString(" · ")
        }
}

@Serializable
data class BundleEntry(
    val resource: FHIRResource? = null,
)

@Serializable
data class FHIRBundle(
    val resourceType: String? = null,
    val type: String? = null,
    val total: Int? = null,
    val entry: List<BundleEntry>? = null,
) {
    val resources: List<FHIRResource> get() = entry?.mapNotNull { it.resource } ?: emptyList()
}
