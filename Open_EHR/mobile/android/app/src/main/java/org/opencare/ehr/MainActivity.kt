package org.opencare.ehr

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import kotlinx.coroutines.launch
import org.opencare.ehr.ui.PatientChartScreen
import org.opencare.ehr.ui.PatientListScreen
import org.opencare.ehr.ui.PortalResultsScreen
import org.opencare.ehr.ui.ResultsInboxScreen
import org.opencare.ehr.ui.ScheduleScreen
import org.opencare.ehr.ui.SignInScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = SessionStore(applicationContext)
        setContent {
            MaterialTheme {
                val state by store.state.collectAsState(initial = store.snapshot())
                // rememberUpdatedState feeds the ApiClient providers, so
                // the one client instance always sees the latest token and
                // base URL without being rebuilt.
                val tokenState = rememberUpdatedState(state.token)
                val baseUrlState = rememberUpdatedState(state.baseUrl)
                val api = remember {
                    ApiClient({ baseUrlState.value }, { tokenState.value })
                }

                if (!state.isSignedIn) {
                    SignInScreen(store = store, initialBaseUrl = state.baseUrl)
                } else {
                    MainShell(api = api, state = state, store = store)
                }
            }
        }
    }
}

private data class Tab(val route: String, val label: String)

@Composable
private fun MainShell(api: ApiClient, state: SessionState, store: SessionStore) {
    val navController = rememberNavController()
    val role = state.role
    val tabs = buildList {
        add(Tab("patients", "Patients"))
        add(Tab("schedule", "Schedule"))
        if (role?.canUseInbox == true) add(Tab("inbox", "Inbox"))
        if (role == DevRole.PATIENT) add(Tab("portal", "My Results"))
        add(Tab("account", "Account"))
    }
    val backStack by navController.currentBackStackEntryAsState()
    val currentRoute = backStack?.destination?.route

    Scaffold(
        bottomBar = {
            NavigationBar {
                tabs.forEach { tab ->
                    NavigationBarItem(
                        selected = currentRoute == tab.route ||
                            (tab.route == "patients" && currentRoute?.startsWith("chart") == true),
                        onClick = {
                            navController.navigate(tab.route) {
                                popUpTo("patients") { inclusive = false }
                                launchSingleTop = true
                            }
                        },
                        icon = { Text(tab.label.first().toString()) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = "patients",
            modifier = Modifier.padding(padding),
        ) {
            composable("patients") {
                PatientListScreen(api) { patientId ->
                    navController.navigate("chart/$patientId")
                }
            }
            composable(
                route = "chart/{patientId}",
                arguments = listOf(navArgument("patientId") { type = NavType.StringType }),
            ) { entry ->
                val patientId = entry.arguments?.getString("patientId") ?: ""
                PatientChartScreen(api, patientId) { navController.popBackStack() }
            }
            composable("schedule") { ScheduleScreen(api) }
            composable("inbox") { ResultsInboxScreen(api) }
            composable("portal") { PortalResultsScreen(api) }
            composable("account") { AccountScreen(state, store) }
        }
    }
}

@Composable
private fun AccountScreen(state: SessionState, store: SessionStore) {
    val scope = rememberCoroutineScope()
    Column(modifier = Modifier.padding(16.dp)) {
        Text("Account", style = MaterialTheme.typography.headlineSmall)
        Text("Role: ${state.role?.displayName ?: "—"}", modifier = Modifier.padding(top = 12.dp))
        Text("User: ${state.username}")
        Text("API: ${state.baseUrl}")
        Button(
            onClick = { scope.launch { store.signOut() } },
            modifier = Modifier.padding(top = 20.dp),
        ) {
            Text("Sign out")
        }
    }
}
