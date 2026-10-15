/**
 * OpenCare SMART on FHIR companion app — one page, two modes:
 *
 * (a) SMART mode: after launch.html + the authorization server redirect
 *     back here, fhirclient's oauth2.ready() resolves with a client bound
 *     to the launched patient; we render that patient's summary, problem
 *     list, medications, and lab trends from the EHR's FHIR server.
 *
 * (b) Local/demo mode: when there is no SMART session, the page offers a
 *     clearly labelled connection to a local OpenCare backend using its
 *     dev-token auth and the SAME rendering, so the app is fully runnable
 *     and demonstrable without any EHR registration.
 */
import FHIR from "fhirclient/browser";
import { loadSmartConfig, saveSmartConfig, type SmartConfig } from "./config";
import {
  bundleResources,
  groupLabTrends,
  sparklineSvg,
  summarize,
  type FhirResource,
  type PatientSummary,
  type TrendSeries,
} from "./fhirShape";
import { LocalFhirClient } from "./localApi";

// ---------------------------------------------------------------------------
// Rendering (shared by both modes)
// ---------------------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderSummary(
  root: HTMLElement,
  summary: PatientSummary,
  trends: TrendSeries[],
): void {
  root.replaceChildren();

  const header = el("section");
  header.className = "card";
  header.append(el("h2", summary.name || "Patient"));
  const meta = el(
    "p",
    `Patient ${summary.id}` +
      (summary.birthDate ? ` · Born ${summary.birthDate}` : "") +
      (summary.gender ? ` · ${summary.gender}` : ""),
  );
  meta.className = "muted";
  header.append(meta);
  root.append(header);

  const problems = el("section");
  problems.className = "card";
  problems.append(el("h3", "Conditions"));
  if (summary.conditions.length === 0) {
    problems.append(el("p", "No conditions on record."));
  } else {
    const list = el("ul");
    for (const c of summary.conditions) {
      list.append(el("li", c.code ? `${c.display} (${c.code})` : c.display));
    }
    problems.append(list);
  }
  root.append(problems);

  const meds = el("section");
  meds.className = "card";
  meds.append(el("h3", "Medications"));
  if (summary.medications.length === 0) {
    meds.append(el("p", "No medication requests on record."));
  } else {
    const list = el("ul");
    for (const m of summary.medications) {
      list.append(el("li", m.status ? `${m.display} — ${m.status}` : m.display));
    }
    meds.append(list);
  }
  root.append(meds);

  const labs = el("section");
  labs.className = "card";
  labs.append(el("h3", "Lab trends"));
  if (trends.length === 0) {
    labs.append(el("p", "No numeric lab observations on record."));
  } else {
    const table = el("table");
    for (const s of trends) {
      const row = el("tr");
      const latest = s.points[s.points.length - 1];
      row.append(el("td", `${s.display} (${s.code})`));
      const chart = el("td");
      chart.innerHTML = sparklineSvg(s); // numeric data + XML-escaped labels only
      row.append(chart);
      row.append(el("td", latest ? `${latest.value} ${s.unit} · ${latest.date.slice(0, 10)}` : ""));
      table.append(row);
    }
    labs.append(table);
  }
  root.append(labs);
}

// ---------------------------------------------------------------------------
// Mode plumbing
// ---------------------------------------------------------------------------

function setMode(mode: "smart" | "local", detail: string): void {
  const badge = document.getElementById("mode-badge");
  if (badge) {
    badge.textContent = mode === "smart" ? `SMART mode — ${detail}` : "Local demo mode";
    badge.className = mode === "smart" ? "badge smart" : "badge local";
  }
  const panel = document.getElementById("local-panel");
  if (panel) panel.hidden = mode === "smart";
}

function setStatus(message: string): void {
  const status = document.getElementById("app-status");
  if (status) status.textContent = message;
}

async function trySmartMode(root: HTMLElement): Promise<boolean> {
  let client: Awaited<ReturnType<typeof FHIR.oauth2.ready>>;
  try {
    client = await FHIR.oauth2.ready();
  } catch {
    return false; // No SMART session in this page load -> local mode.
  }
  const patientId = client.patient?.id;
  if (!patientId) return false;
  setMode("smart", client.getState?.("serverUrl") ?? "EHR FHIR server");
  setStatus("Loading patient from the EHR…");
  const q = `patient=${encodeURIComponent(patientId)}`;
  const [patient, conditions, medications, observations] = await Promise.all([
    client.patient.read() as Promise<FhirResource>,
    client.request(`Condition?${q}`).then(bundleResources),
    client.request(`MedicationRequest?${q}`).then(bundleResources),
    client.request(`Observation?${q}&_count=200`).then(bundleResources),
  ]);
  renderSummary(root, summarize(patient, conditions, medications), groupLabTrends(observations));
  setStatus("");
  return true;
}

// ---------------------------------------------------------------------------
// Local mode + settings forms
// ---------------------------------------------------------------------------

function wireLocalMode(root: HTMLElement): void {
  const baseInput = document.getElementById("local-base") as HTMLInputElement | null;
  const tokenInput = document.getElementById("local-token") as HTMLInputElement | null;
  const connectBtn = document.getElementById("local-connect") as HTMLButtonElement | null;
  const select = document.getElementById("local-patient") as HTMLSelectElement | null;
  if (!baseInput || !tokenInput || !connectBtn || !select) return;

  baseInput.value = localStorage.getItem("opencare.local.base") ?? "http://127.0.0.1:8000";
  tokenInput.value = localStorage.getItem("opencare.local.token") ?? "clinician-token";

  let client: LocalFhirClient | null = null;

  select.addEventListener("change", async () => {
    if (!client || !select.value) return;
    setStatus("Loading patient from the OpenCare backend…");
    try {
      const { summary, trends } = await client.getPatientSummary(select.value);
      renderSummary(root, summary, trends);
      setStatus("");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    }
  });

  connectBtn.addEventListener("click", async () => {
    localStorage.setItem("opencare.local.base", baseInput.value);
    localStorage.setItem("opencare.local.token", tokenInput.value);
    client = new LocalFhirClient(baseInput.value, tokenInput.value);
    setStatus("Connecting to the OpenCare backend…");
    try {
      const patients = await client.listPatients();
      select.replaceChildren();
      for (const p of patients) {
        const opt = document.createElement("option");
        opt.value = String(p.id ?? "");
        opt.textContent = `${String(p.name?.[0]?.given?.join(" ") ?? "")} ${String(
          p.name?.[0]?.family ?? p.id ?? "",
        )}`.trim();
        select.append(opt);
      }
      if (patients.length === 0) {
        setStatus("Connected, but the backend has no patients yet — load the seed data (backend/seed).");
        return;
      }
      select.dispatchEvent(new Event("change"));
    } catch (err) {
      setStatus(
        `Could not reach the backend: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  });
}

function wireSettingsForm(): void {
  const form = document.getElementById("settings-form") as HTMLFormElement | null;
  if (!form) return;
  const cfg = loadSmartConfig();
  const set = (id: string, value: string) => {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.value = value;
  };
  set("smart-client-id", cfg.clientId);
  set("smart-scope", cfg.scope);
  set("smart-fhir-url", cfg.fhirServiceUrl);
  set("smart-redirect-uri", cfg.redirectUri);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const get = (id: string) =>
      (document.getElementById(id) as HTMLInputElement | null)?.value.trim() ?? "";
    const next: SmartConfig = {
      clientId: get("smart-client-id"),
      scope: get("smart-scope"),
      fhirServiceUrl: get("smart-fhir-url"),
      redirectUri: get("smart-redirect-uri") || ".",
    };
    saveSmartConfig(next);
    setStatus("SMART settings saved to this browser (localStorage).");
  });
}

async function main(): Promise<void> {
  const root = document.getElementById("summary");
  if (!root) return;
  wireSettingsForm();
  const smart = await trySmartMode(root).catch((err: unknown) => {
    setStatus(`SMART load failed: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  });
  if (!smart) {
    setMode("local", "");
    wireLocalMode(root);
    setStatus("No SMART session — use the local demo mode below, or launch from an EHR.");
  }
}

void main();
