package org.opencare.ehr.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.opencare.ehr.Config
import org.opencare.ehr.DevRole
import org.opencare.ehr.SessionStore

/**
 * Dev-mode sign-in: pick a role, get that role's seeded backend token
 * (mirrors the iOS SignInView). The base URL is editable here because the
 * right value differs per setup: emulator 10.0.2.2, device = LAN address.
 */
@Composable
fun SignInScreen(store: SessionStore, initialBaseUrl: String) {
    var selected by remember { mutableStateOf(DevRole.CLINICIAN) }
    var baseUrl by remember { mutableStateOf(initialBaseUrl) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        Text("OpenCare", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(4.dp))
        Text(
            "Development sign-in — uses the backend's seeded dev tokens " +
                "(backend/app/security.py). The patient role is linked to " +
                "Patient/pat-1 and sees only that record.",
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(16.dp))
        DevRole.entries.forEach { role ->
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth(),
            ) {
                RadioButton(selected = selected == role, onClick = { selected = role })
                Text(role.displayName)
            }
        }
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text("API base URL") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "Emulator default: ${Config.DEFAULT_BASE_URL}",
            style = MaterialTheme.typography.bodySmall,
        )
        Spacer(Modifier.height(16.dp))
        Button(
            onClick = { scope.launch { store.signIn(selected, baseUrl) } },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Sign in as ${selected.displayName}")
        }
    }
}
