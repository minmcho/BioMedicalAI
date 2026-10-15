package org.opencare.shared

/**
 * The OpenCare API contract every client implements (Android:
 * ApiClient.kt with OkHttp; iOS: APIClient.swift with URLSession).
 * Paths are relative to the configured base URL and correspond 1:1 to
 * backend routes in backend/app/fhir.py.
 */
public interface OpenCareApi {
    /** GET fhir/Patient — force-scoped to own record under patient tokens. */
    public suspend fun patients(): List<FHIRResource>

    /** GET fhir/Patient/{id} */
    public suspend fun patient(id: String): FHIRResource

    /** GET fhir/Condition?patient= */
    public suspend fun conditions(patientRef: String): List<FHIRResource>

    /** GET fhir/MedicationRequest?patient= */
    public suspend fun medications(patientRef: String): List<FHIRResource>

    /** GET fhir/Observation?patient= */
    public suspend fun observations(patientRef: String): List<FHIRResource>

    /** GET fhir/inbox?patient= — staff roles only. */
    public suspend fun inbox(patientRef: String): List<FHIRResource>

    /** GET portal/my-results — patient role only, scoped server-side. */
    public suspend fun myResults(): List<FHIRResource>

    /** GET fhir/Appointment — read-only display in mobile v1. */
    public suspend fun appointments(): List<FHIRResource>
}

/** Backend route paths, for clients that build URLs from parts. */
public object Routes {
    public const val HEALTH: String = "health"
    public const val FHIR: String = "fhir"
    public const val INBOX: String = "fhir/inbox"
    public const val MY_RESULTS: String = "portal/my-results"
}

/** Dev roles, mirroring the backend's seeded dev tokens (AUTH_MODE=dev). */
public enum class DevRole(public val token: String, public val displayName: String) {
    ADMIN("admin-token", "Admin"),
    CONFIGURATOR("config-token", "Configurator"),
    CLINICIAN("clinician-token", "Clinician"),
    VIEWER("viewer-token", "Viewer"),
    PATIENT("patient-token", "Patient (pat-1)");

    /** Staff roles may use the results inbox (backend INBOX_ROLES). */
    public val canUseInbox: Boolean
        get() = this == ADMIN || this == CONFIGURATOR || this == CLINICIAN
}
