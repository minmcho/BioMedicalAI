package org.opencare.ehr.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.opencare.ehr.ApiClient
import org.opencare.ehr.FHIRResource

/**
 * Staff results inbox — GET /fhir/inbox?patient=. Returns the patient's
 * Observation + DiagnosticReport bundle, newest first. Staff roles only;
 * the backend answers 403 for viewer/patient.
 */
@Composable
fun ResultsInboxScreen(api: ApiClient) {
    var patients by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var selected by remember { mutableStateOf<FHIRResource?>(null) }
    var results by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var menuOpen by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        try {
            patients = api.patients()
        } catch (e: Exception) {
            error = e.message
        }
    }

    LaunchedEffect(selected) {
        val patient = selected ?: return@LaunchedEffect
        loading = true
        try {
            results = api.inbox("Patient/${patient.id}")
            error = null
        } catch (e: Exception) {
            results = emptyList()
            error = e.message
        } finally {
            loading = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Results Inbox", style = MaterialTheme.typography.headlineSmall)
        Button(
            onClick = { menuOpen = true },
            modifier = Modifier.padding(top = 12.dp),
        ) {
            Text(selected?.displayTitle ?: "Select patient")
        }
        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
            patients.forEach { patient ->
                DropdownMenuItem(
                    text = { Text(patient.displayTitle) },
                    onClick = {
                        selected = patient
                        menuOpen = false
                    },
                )
            }
        }
        if (loading) {
            CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp))
        }
        if (selected != null && !loading && error == null && results.isEmpty()) {
            Text("No results for this patient.", modifier = Modifier.padding(top = 16.dp))
        }
        LazyColumn(modifier = Modifier.fillMaxWidth()) {
            items(results, key = { "${it.resourceType}-${it.id}" }) { result ->
                Column(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                    Text(result.displayTitle, style = MaterialTheme.typography.titleMedium)
                    val subtitle = listOf(result.resourceType, result.displaySubtitle)
                        .filter { it.isNotEmpty() }.joinToString(" · ")
                    if (subtitle.isNotEmpty()) {
                        Text(subtitle, style = MaterialTheme.typography.bodyMedium)
                    }
                }
                HorizontalDivider()
            }
        }
    }
}
