package org.opencare.ehr

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "opencare_session")

data class SessionState(
    val token: String? = null,
    val role: DevRole? = null,
    val baseUrl: String = Config.DEFAULT_BASE_URL,
) {
    val isSignedIn: Boolean get() = !token.isNullOrEmpty() && role != null
    val username: String get() = role?.let { "dev-${it.name.lowercase()}" } ?: ""
}

/**
 * Session persistence via DataStore (token, role, base URL). The token is
 * a dev credential in AUTH_MODE=dev; production OIDC tokens should move
 * to EncryptedFile/Keystore-backed storage — noted in mobile/README.md.
 */
class SessionStore(private val context: Context) {
    private object Keys {
        val TOKEN = stringPreferencesKey("token")
        val ROLE = stringPreferencesKey("role")
        val BASE_URL = stringPreferencesKey("base_url")
    }

    val state: Flow<SessionState> = context.dataStore.data.map { prefs ->
        SessionState(
            token = prefs[Keys.TOKEN],
            role = DevRole.fromName(prefs[Keys.ROLE]),
            baseUrl = prefs[Keys.BASE_URL] ?: Config.DEFAULT_BASE_URL,
        )
    }

    /** Synchronous snapshot for the ApiClient providers (read once at start). */
    fun snapshot(): SessionState = runBlocking { state.first() }

    suspend fun signIn(role: DevRole, baseUrl: String) {
        context.dataStore.edit { prefs ->
            prefs[Keys.TOKEN] = role.token
            prefs[Keys.ROLE] = role.name
            prefs[Keys.BASE_URL] = baseUrl.trim().trimEnd('/')
        }
    }

    suspend fun signOut() {
        context.dataStore.edit { prefs ->
            prefs.remove(Keys.TOKEN)
            prefs.remove(Keys.ROLE)
        }
    }
}
