package org.opencare.ehr

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/** Typed API failure so screens can show honest states (401/403/404). */
class ApiException(val status: Int, message: String) : Exception(message) {
    companion object {
        fun forStatus(status: Int, body: String): ApiException = when (status) {
            401 -> ApiException(status, "Session rejected (401). Sign in again.")
            403 -> ApiException(status, "Your role may not do this (403).")
            404 -> ApiException(status, "Not found (404).")
            else -> ApiException(status, "Server $status: $body")
        }
    }
}

/**
 * OkHttp client for the OpenCare FHIR API. Every method maps to a real
 * backend route (backend/app/fhir.py); there are no local fixtures in the
 * runtime path. [baseUrlProvider] and [tokenProvider] are lambdas so the
 * session store can update them without rebuilding the client.
 */
class ApiClient(
    private val baseUrlProvider: () -> String,
    private val tokenProvider: () -> String?,
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val json = Json { ignoreUnknownKeys = true }

    private suspend fun getJson(path: String, query: Map<String, String> = emptyMap()): String {
        val token = tokenProvider()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException(401, "Not signed in.")
        val urlBuilder = (baseUrlProvider().trimEnd('/') + "/" + path).toHttpUrl().newBuilder()
        query.forEach { (k, v) -> urlBuilder.addQueryParameter(k, v) }
        val request = Request.Builder()
            .url(urlBuilder.build())
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/json")
            .get()
            .build()
        return withContext(Dispatchers.IO) {
            http.newCall(request).execute().use { response ->
                val body = response.body?.string() ?: ""
                if (!response.isSuccessful) throw ApiException.forStatus(response.code, body)
                body
            }
        }
    }

    private suspend fun search(
        resourceType: String,
        patient: String? = null,
    ): List<FHIRResource> {
        val query = if (patient != null) mapOf("patient" to patient) else emptyMap()
        val bundle = json.decodeFromString<FHIRBundle>(getJson("fhir/$resourceType", query))
        return bundle.resources
    }

    /** GET /fhir/Patient — force-scoped to own record under patient tokens. */
    suspend fun patients(): List<FHIRResource> = search("Patient")

    /** GET /fhir/Patient/{id} */
    suspend fun patient(id: String): FHIRResource =
        json.decodeFromString(getJson("fhir/Patient/$id"))

    /** GET /fhir/Condition?patient= */
    suspend fun conditions(patientRef: String): List<FHIRResource> = search("Condition", patientRef)

    /** GET /fhir/MedicationRequest?patient= */
    suspend fun medications(patientRef: String): List<FHIRResource> =
        search("MedicationRequest", patientRef)

    /** GET /fhir/Observation?patient= */
    suspend fun observations(patientRef: String): List<FHIRResource> =
        search("Observation", patientRef)

    /** GET /fhir/inbox?patient= — staff roles only (403 otherwise). */
    suspend fun inbox(patientRef: String): List<FHIRResource> {
        val bundle = json.decodeFromString<FHIRBundle>(
            getJson("fhir/inbox", mapOf("patient" to patientRef))
        )
        return bundle.resources
    }

    /** GET /portal/my-results — patient role only; scoped server-side. */
    suspend fun myResults(): List<FHIRResource> {
        val bundle = json.decodeFromString<FHIRBundle>(getJson("portal/my-results"))
        return bundle.resources
    }

    /** GET /fhir/Appointment — status displayed read-only in v1. */
    suspend fun appointments(): List<FHIRResource> = search("Appointment")
}
