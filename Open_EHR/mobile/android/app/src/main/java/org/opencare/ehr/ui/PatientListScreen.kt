package org.opencare.ehr.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
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
 * GET /fhir/Patient — live list from the API. Under a patient token the
 * backend returns only the caller's own record.
 */
@Composable
fun PatientListScreen(api: ApiClient, onPatientClick: (String) -> Unit) {
    var patients by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reloadKey by remember { mutableStateOf(0) }

    LaunchedEffect(reloadKey) {
        loading = true
        try {
            patients = api.patients()
            error = null
        } catch (e: Exception) {
            error = e.message
        } finally {
            loading = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Patients", style = MaterialTheme.typography.headlineSmall)
        if (loading && patients.isEmpty()) {
            CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp))
            Button(onClick = { reloadKey++ }, modifier = Modifier.padding(top = 8.dp)) {
                Text("Retry")
            }
        }
        if (!loading && error == null && patients.isEmpty()) {
            Text("No patients returned by the server.", modifier = Modifier.padding(top = 16.dp))
        }
        LazyColumn(modifier = Modifier.fillMaxWidth()) {
            items(patients, key = { it.id ?: it.displayTitle }) { patient ->
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable { patient.id?.let(onPatientClick) }
                        .padding(vertical = 10.dp),
                ) {
                    Text(patient.displayTitle, style = MaterialTheme.typography.titleMedium)
                    if (patient.displaySubtitle.isNotEmpty()) {
                        Text(patient.displaySubtitle, style = MaterialTheme.typography.bodyMedium)
                    }
                }
                HorizontalDivider()
            }
        }
    }
}
