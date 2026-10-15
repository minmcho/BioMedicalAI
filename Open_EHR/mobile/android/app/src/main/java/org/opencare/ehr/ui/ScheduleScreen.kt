package org.opencare.ehr.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
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
 * Schedule — GET /fhir/Appointment. Read-only in mobile v1: the status
 * (booked / arrived / fulfilled / cancelled …) is displayed; ADT
 * transitions run through staff API/web workflows. Patient tokens are
 * force-scoped to their own appointments by the backend.
 */
@Composable
fun ScheduleScreen(api: ApiClient) {
    var appointments by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reloadKey by remember { mutableStateOf(0) }

    LaunchedEffect(reloadKey) {
        loading = true
        try {
            appointments = api.appointments()
            error = null
        } catch (e: Exception) {
            error = e.message
        } finally {
            loading = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Schedule", style = MaterialTheme.typography.headlineSmall)
        if (loading && appointments.isEmpty()) {
            CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp))
            Button(onClick = { reloadKey++ }, modifier = Modifier.padding(top = 8.dp)) {
                Text("Retry")
            }
        }
        if (!loading && error == null && appointments.isEmpty()) {
            Text("Nothing is scheduled.", modifier = Modifier.padding(top = 16.dp))
        }
        LazyColumn(modifier = Modifier.fillMaxWidth()) {
            items(appointments, key = { "Appointment-${it.id}" }) { appointment ->
                Row(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(appointment.displayTitle, style = MaterialTheme.typography.titleMedium)
                        appointment.start?.let { start ->
                            Text(start, style = MaterialTheme.typography.bodyMedium)
                        }
                        appointment.participant
                            ?.mapNotNull { it.actor?.reference }
                            ?.firstOrNull { it.startsWith("Patient/") }
                            ?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
                    }
                    appointment.status?.let { status ->
                        Surface(
                            color = MaterialTheme.colorScheme.secondaryContainer,
                            shape = MaterialTheme.shapes.small,
                        ) {
                            Text(
                                status,
                                style = MaterialTheme.typography.labelMedium,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
                            )
                        }
                    }
                }
                HorizontalDivider()
            }
        }
    }
}
