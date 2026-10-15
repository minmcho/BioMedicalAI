/**
 * SMART EHR-launch entry point (launch.html).
 *
 * The EHR / sandbox launcher opens this page with `iss` and `launch`
 * query parameters. We hand them to fhirclient's authorize(), which
 * redirects the browser to the authorization server; the server returns
 * to the redirect URI (the app root, index.html), where app.ts completes
 * the flow with oauth2.ready().
 */
import FHIR from "fhirclient/browser";
import { loadSmartConfig } from "./config";

function showError(message: string): void {
  const el = document.getElementById("launch-status");
  if (el) el.textContent = message;
}

async function main(): Promise<void> {
  const params = new URLSearchParams(window.location.search);
  const iss = params.get("iss") ?? undefined;
  const launch = params.get("launch") ?? undefined;
  const cfg = loadSmartConfig();

  if (!iss && !cfg.fhirServiceUrl) {
    showError(
      "No `iss` launch parameter and no configured FHIR service URL. " +
        "Open this page from an EHR/sandbox launcher, or use the app's local demo mode.",
    );
    return;
  }
  if (!cfg.clientId) {
    showError(
      "No SMART client_id configured. Register this app with your FHIR " +
        "server (see smart-app/README.md), save the client_id in the app " +
        "settings on the index page, then launch again.",
    );
    return;
  }

  await FHIR.oauth2.authorize({
    clientId: cfg.clientId,
    scope: cfg.scope,
    // EHR launch: iss/launch come from the launcher's query parameters.
    // Standalone launch: fall back to the configured FHIR service URL.
    iss: iss ?? cfg.fhirServiceUrl,
    launch,
    redirectUri: cfg.redirectUri,
  });
}

void main().catch((err: unknown) =>
  showError(`Launch failed: ${err instanceof Error ? err.message : String(err)}`),
);
