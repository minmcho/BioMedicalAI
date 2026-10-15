// Kotlin Multiplatform skeleton for the OpenCare shared contract.
//
// Status (v1, honest): this module defines the canonical FHIR-lite
// models and the API interface in commonMain. The Android app currently
// carries its own mirror of these types (mobile/android/.../Models.kt)
// and does NOT yet depend on this module, and the iOS app uses its own
// Codable mirror — see mobile/README.md ("Shared contract" section).
// Wiring the Android app onto this module and consuming it from iOS via
// a KMP framework are documented follow-ups. The iosX64/iosArm64 targets
// below only compile on a macOS host.
plugins {
    kotlin("multiplatform") version "2.0.20"
    kotlin("plugin.serialization") version "2.0.20"
}

kotlin {
    androidTarget()

    // Requires a macOS host to compile/link; declared so the module's
    // intent is explicit. Framework name: OpenCareShared.
    iosX64()
    iosArm64()
    iosSimulatorArm64()

    sourceSets {
        commonMain.dependencies {
            implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")
        }
    }
}
