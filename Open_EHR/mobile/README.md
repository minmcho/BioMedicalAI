# OpenCare mobile — native iOS (SwiftUI) + Android (Kotlin Compose)

Two native apps over the same OpenCare FHIR API — no mobile-only backend.
Design decision and rationale: system design §8 (native SwiftUI + Kotlin
replaced the earlier React Native proposal; sharing lives in the API
contract, the KMP skeleton in `shared/`, and a shared design spec).

```
mobile/
├── ios/       SwiftUI app "OpenCare" (Universal: iPhone + iPad)
├── android/   Jetpack Compose app "OpenCare"
└── shared/    Kotlin Multiplatform contract skeleton (models + API)
```

**Build status (honest):** this source was authored and validated on a
Linux VM (syntax + reference-consistency checks — see
`docs/sqa/PHASE_MOBILE_SQA_REPORT.md`). It has **not been compiled yet**:
first builds happen in Xcode (MacBook Air) and Android Studio. Expect the
normal first-build fixes any fresh project needs; the SQA report lists
exactly what was and was not verified.

## Backend first (both apps)

Run the core API before either app (from the repo root):

```bash
cd backend
../.venv/bin/uvicorn app.main:app        # http://127.0.0.1:8000
```

Seed some synthetic patients/results with the usual API calls (see the
root README and backend tests for payload shapes). Dev tokens below are
the backend's `AUTH_MODE=dev` identities.

## iOS — open & build (Xcode, MacBook Air)

1. Open `mobile/ios/OpenCare.xcodeproj` in Xcode.
2. Pick a team under *Signing & Capabilities* for your device/simulator
   (bundle ID `org.opencare.ehr`). The target is Universal
   (`TARGETED_DEVICE_FAMILY = 1,2` — iPhone + iPad).
3. Run. The app reads its backend URL from the `API_BASE_URL` key in
   `OpenCare/Info.plist` (default `http://localhost:8000`).

**URL caveats:** `localhost` works in the **iOS Simulator** (it shares the
Mac's network). On a **physical device**, `localhost` is the phone itself —
set `API_BASE_URL` to the Mac's LAN address (e.g. `http://192.168.1.20:8000`)
and start the backend with `--host 0.0.0.0`. `NSAllowsLocalNetworking` is
set in Info.plist for these local-network http calls; production must use
https and drop that allowance.

**Regeneration path:** the `.xcodeproj` is hand-authored (one app target,
all sources listed explicitly). If a future Xcode dislikes it, regenerate
an equivalent project from `mobile/ios/project.yml` with
[XcodeGen](https://github.com/yonaskolb/XcodeGen): `cd mobile/ios && xcodegen generate`.

Sources: `OpenCareApp.swift` (@main + role-aware tab shell), `Config.swift`
(base URL + dev roles/tokens), `APIClient.swift` (URLSession async/await,
typed errors), `Models.swift` (Codable FHIR-lite), `KeychainStore.swift`
(Security-framework token storage), `Session.swift` (auth state), and
`Views/` — SignIn, PatientList, PatientChart, ResultsInbox,
PortalResults, Schedule. No third-party dependencies.

## Android — open & build (Android Studio)

1. Open the `mobile/android/` folder in Android Studio and let Gradle sync.
   (If the Gradle wrapper is missing on first open, generate it once with
   `gradle wrapper --gradle-version 8.7` or Android Studio's wrapper
   prompt — the wrapper jar is not committed in this source drop.)
2. Run the `app` configuration on an emulator or device.

**URL caveats:** the Android **emulator cannot use your computer's
`localhost`** — `10.0.2.2` is its alias for the host, so the default base
URL is `http://10.0.2.2:8000`. On a **physical device**, edit the base
URL on the sign-in screen to the host's LAN address (the field is
editable and persists with the session). Cleartext http is enabled in the
manifest for development only.

Stack: Compose (BOM 2024.09.00) + Material 3, OkHttp 4.12,
kotlinx-serialization 1.7.1, DataStore for the session, Navigation
Compose. Screens in `app/src/main/java/org/opencare/ehr/ui/` mirror iOS
one-for-one. The dev token lives in DataStore (plain preferences) in v1 —
for production OIDC tokens, move it to Keystore-encrypted storage.

## Shared contract (`shared/`)

A Kotlin Multiplatform skeleton holding the canonical FHIR-lite models
and the `OpenCareApi` interface in `commonMain`. Plainly stated: **the
apps do not link it yet** — Android carries its own mirror of the models
in its app module and iOS carries a Codable mirror, because the contract
is REST/JSON and both platforms decode it natively today. The follow-ups
are (a) point the Android app's models at this module, and (b) consume it
from iOS as a KMP framework (which only builds on macOS). The value today
is one reviewed place where the contract lives.

## Dev sign-in (both apps)

Sign-in is a role picker — dev mode has no passwords. Tokens are the
backend's seeded dev tokens (`backend/app/security.py`):

| Role | Token | Sees |
|---|---|---|
| Admin | `admin-token` | Everything, inbox |
| Configurator | `config-token` | Everything, inbox |
| Clinician | `clinician-token` | Patients, charts, inbox, schedule |
| Viewer | `viewer-token` | Read-only lists/chart/schedule (inbox = 403) |
| Patient | `patient-token` | Own record only (`Patient/pat-1`): patients list = self, My Results portal, own appointments |

Production sign-in is the backend's OIDC mode (Keycloak) — wiring a
native OIDC flow into these apps is a follow-up, not part of v1.

## Feature parity

| Feature | iOS | Android | Web |
|---|---|---|---|
| Dev role sign-in | ✅ | ✅ | (API tokens) |
| Patient list | ✅ | ✅ | — (no web UI yet) |
| Patient chart (problems/meds/observations) | ✅ | ✅ | — |
| Results inbox (staff) | ✅ | ✅ | — |
| Patient portal results | ✅ | ✅ | — |
| Schedule (read-only, status badges) | ✅ | ✅ | — |
| ADT transitions from device | ❌ v1 read-only | ❌ v1 read-only | — |
| Writes of any kind | ❌ | ❌ | — |

Mobile v1 is a read-only clinical companion: every screen is a live API
read; nothing on either app writes to the chart.
