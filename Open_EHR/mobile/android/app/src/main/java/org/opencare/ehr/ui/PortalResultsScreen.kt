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
 * Patient portal — GET /portal/my-results. There is no patient parameter
 * on this route: the backend derives the patient from the caller's token
 * and returns only that patient's results, newest first.
 */
@Composable
fun PortalResultsScreen(api: ApiClient) {
    var results by remember { mutableStateOf<List<FHIRResource>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reloadKey by remember { mutableStateOf(0) }

    LaunchedEffect(reloadKey) {
        loading = true
        try {
            results = api.myResults()
            error = null
        } catch (e: Exception) {
            error = e.message
        } finally {
            loading = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("My Results", style = MaterialTheme.typography.headlineSmall)
        if (loading && results.isEmpty()) {
            CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp))
            Button(onClick = { reloadKey++ }, modifier = Modifier.padding(top = 8.dp)) {
                Text("Retry")
            }
        }
        if (!loading && error == null && results.isEmpty()) {
            Text("No results yet. When results are filed, they appear here.",
                modifier = Modifier.padding(top = 16.dp))
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
