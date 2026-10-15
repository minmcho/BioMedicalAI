package org.opencare.ehr.ui

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
 * One patient's chart: the Patient resource plus problems (Condition),
 * medications (MedicationRequest), and observations — live searches
 * scoped by ?patient=Patient/{id}.
 */
@Composable
fun PatientChartScreen(api: ApiClient, patientId: String, onBack: () -> Unit) {
    var patient by remember { mutableStateOf<FHIRResource?>(null) }
    var conditions by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var medications by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var observations by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }

    LaunchedEffect(patientId) {
        loading = true
        try {
            val ref = "Patient/$patientId"
            patient = api.patient(patientId)
            conditions = api.conditions(ref)
            medications = api.medications(ref)
            observations = api.observations(ref)
            error = null
        } catch (e: Exception) {
            error = e.message
        } finally {
            loading = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Button(onClick = onBack) { Text("Back") }
        Text(
            patient?.displayTitle ?: "Patient",
            style = MaterialTheme.typography.headlineSmall,
            modifier = Modifier.padding(top = 12.dp),
        )
        patient?.let {
            Text("Reference: Patient/$patientId", style = MaterialTheme.typography.bodyMedium)
            it.birthDate?.let { dob -> Text("Date of birth: $dob") }
            it.gender?.let { g -> Text("Gender: $g") }
        }
        if (loading) {
            CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp))
        }
        LazyColumn(modifier = Modifier.fillMaxWidth()) {
            chartSection("Problems", conditions)
            chartSection("Medications", medications)
            chartSection("Observations", observations)
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.chartSection(
    title: String,
    items: List<FHIRResource>,
) {
    item {
        Text(
            title,
            style = MaterialTheme.typography.titleLarge,
            modifier = Modifier.padding(top = 20.dp, bottom = 4.dp),
        )
    }
    if (items.isEmpty()) {
        item { Text("None recorded", style = MaterialTheme.typography.bodyMedium) }
    } else {
        items(items, key = { "${it.resourceType}-${it.id}" }) { resource ->
            Column(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                Text(resource.displayTitle, style = MaterialTheme.typography.titleMedium)
                if (resource.displaySubtitle.isNotEmpty()) {
                    Text(resource.displaySubtitle, style = MaterialTheme.typography.bodyMedium)
                }
            }
            HorizontalDivider()
        }
    }
}
