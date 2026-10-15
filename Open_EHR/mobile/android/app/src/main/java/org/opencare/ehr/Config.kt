package org.opencare.ehr

/**
 * Dev roles and their seeded backend tokens — an exact mirror of the iOS
 * DevRole (mobile/ios/OpenCare/Config.swift) and of the backend's
 * DEFAULT_DEV_TOKENS (backend/app/security.py, AUTH_MODE=dev).
 * Production replaces dev sign-in with the backend's OIDC mode.
 */
enum class DevRole(val token: String, val displayName: String) {
    ADMIN("admin-token", "Admin"),
    CONFIGURATOR("config-token", "Configurator"),
    CLINICIAN("clinician-token", "Clinician"),
    VIEWER("viewer-token", "Viewer"),
    PATIENT("patient-token", "Patient (pat-1)");

    /** Staff roles may use the results inbox (backend INBOX_ROLES). */
    val canUseInbox: Boolean
        get() = this == ADMIN || this == CONFIGURATOR || this == CLINICIAN

    companion object {
        fun fromName(name: String?): DevRole? = entries.firstOrNull { it.name == name }
    }
}

object Config {
    /**
     * Default base URL for the Android emulator: 10.0.2.2 is the
     * emulator's alias for the host machine's localhost, where the
     * backend runs on port 8000. On a physical device, edit the base URL
     * on the sign-in screen to the host's LAN address. The value is
     * persisted with the session (see SessionStore).
     */
    const val DEFAULT_BASE_URL = "http://10.0.2.2:8000"
}
