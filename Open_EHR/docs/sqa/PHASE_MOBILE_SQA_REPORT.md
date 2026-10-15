# SQA Report — Mobile v1 source (iOS SwiftUI + Android Compose + KMP skeleton)

**Date:** 2026-10-15
**Deliverable:** `mobile/` — native iOS app (SwiftUI, Universal),
native Android app (Kotlin/Jetpack Compose), KMP shared-contract
skeleton. Authored on the Linux build VM; **first compilation happens on
Min's Macs** (Xcode on the MacBook Air, Android Studio). This report
follows the standing gate in `docs/SQA.md`: a check that could not run is
recorded NOT RUN, never as passed.

## Scope built

- **iOS** (`mobile/ios/`): hand-authored `OpenCare.xcodeproj`
  (one app target, 12 Swift sources listed explicitly + Info.plist),
  `project.yml` (XcodeGen regeneration path), app sources —
  `OpenCareApp` (role-aware tab shell), `Config` (Info.plist
  `API_BASE_URL`, dev roles/tokens matching `backend/app/security.py`),
  `APIClient` (URLSession async/await, typed 401/403/404/server errors),
  `Models` (Codable FHIR-lite incl. mixed-Bundle decoding), `KeychainStore`
  (Security framework), `Session` (dev role sign-in, Keychain-persisted),
  and six views: SignIn, PatientList, PatientChart, ResultsInbox,
  PortalResults, Schedule. No third-party dependencies.
- **Android** (`mobile/android/`): Gradle skeleton (AGP 8.5.2, Kotlin
  2.0.20, Compose BOM 2024.09.00, OkHttp 4.12.0, kotlinx-serialization
  1.7.1, DataStore 1.1.1, Navigation Compose 2.7.7), manifest + resources,
  `MainActivity` with a role-aware bottom-nav shell, `ApiClient`,
  `Models`, `Config` (dev roles), `SessionStore` (DataStore), and six
  Compose screens mirroring iOS one-for-one.
- **Shared** (`mobile/shared/`): KMP skeleton — `build.gradle.kts`
  (androidTarget + iOS targets declared), `commonMain` canonical
  FHIR-lite models + `OpenCareApi` interface + route constants + dev
  roles. Explicitly NOT linked by either app in v1 (see Limitations).

Every screen calls the real API client; no screen ships a hardcoded
patient list. The only fixture in the tree is a SwiftUI `#Preview` for
the sign-in screen, marked as a preview fixture in code.

## Checks RUN on this VM (with results)

| Check | Method | Result |
|---|---|---|
| Info.plist is a valid plist | `plistlib.load` | PASS — `API_BASE_URL=http://localhost:8000`, ATS local networking true |
| AndroidManifest.xml / strings.xml / themes.xml are valid XML | `xml.etree.ElementTree.parse` ×3 | PASS ×3 |
| project.yml is valid YAML | `yaml.safe_load` | PASS |
| pbxproj object-graph integrity | parsed all object IDs: 40 defined objects, every ID 24-hex, every `/* */`-annotated reference resolves to a definition | PASS |
| pbxproj targets Universal | grep `TARGETED_DEVICE_FAMILY` | PASS — `"1,2"` in both target configurations (Debug + Release) |
| pbxproj ↔ disk consistency | compared PBXFileReference paths vs files under `ios/OpenCare/` | PASS — exact match both ways (13 files); each Swift file appears once as a build file and once in the Sources phase |
| Delimiter balance, all Swift/Kotlin sources | string/comment-aware balance scan (a naive scan's single flag on `Config.swift` was traced to a `//` inside a URL string literal and re-checked with the string-aware scanner) | PASS — all files balanced |
| API surface consistency (iOS) | extracted `session.api.*` calls from all views vs `func` definitions in `APIClient.swift` | PASS — all 7 called methods defined (patients, conditions, medications, observations, inbox, myResults, appointments) |
| API surface consistency (Android) | extracted `api.*` calls vs `suspend fun` definitions in `ApiClient.kt`; screen imports in `MainActivity.kt` vs files + declared composable names in `ui/` | PASS — all calls defined; all 6 screens exist and declare the imported composable name |
| Route/token fidelity vs backend | manual read against `backend/app/fhir.py` + `security.py` | PASS — paths (`fhir/{Type}`, `fhir/inbox`, `portal/my-results`), search params (`patient=`), and all five dev tokens/roles incl. `patient-token` → `Patient/pat-1` match the backend exactly; inbox staff-only and portal patient-only rules match the backend's role checks |
| ADT honesty | code review | PASS — schedule screens display status only; no transition calls exist in either app (v1 read-only, stated in UI docs and README parity matrix) |

## NOT RUN (requires Min's Macs / devices)

- **Swift compilation** — no Swift toolchain exists on Linux for
  SwiftUI/UIKit (SwiftUI cannot compile here at all). First build:
  Xcode on the MacBook Air. The pbxproj is the highest-risk artifact in
  this drop (hand-authored); `project.yml` is the documented fallback.
- **iOS Simulator / device run** — not run. Runtime behaviour (tab
  shell, Keychain on device, ATS behaviour on a physical device) is
  unverified.
- **Android compilation / Gradle sync** — no JDK/Gradle on this VM.
  First build: Android Studio. The Gradle wrapper jar is not committed;
  the README documents generating it on first open.
- **KMP module build** — the `shared/` module was not compiled anywhere;
  its iOS targets require macOS even in principle.
- **End-to-end run against the live backend** from either app — not run
  (depends on both builds above). The API contract itself is covered by
  the backend's pytest suite (Phases 0–1 reports), but no mobile request
  has yet been observed server-side.
- **Offline/sync, HealthKit/Health Connect, biometrics, barcode** —
  design-doc §8 items that are *not* in v1 at all; listed so their
  absence is a recorded decision, not an oversight.

## Known risks for first build (stated, not hidden)

1. Hand-authored `project.pbxproj` may need a small fix in Xcode
   (mitigation: `project.yml` regeneration path, same target/settings).
2. Kotlin/Compose first-compile nits (imports, experimental APIs) are
   possible; the code avoids experimental APIs deliberately
   (no `ExposedDropdownMenuBox`, no pull-refresh).
3. Android Gradle wrapper must be generated on first open (documented
   in `mobile/README.md`).
4. Android dev token is stored in plain DataStore preferences — fine
   for dev tokens, recorded in the README as must-fix before any
   production OIDC token is stored.

## Gate verdict

**Source delivery: PASS on every check runnable on this VM.**
**Build/runtime: NOT VERIFIED — pending first builds on Min's Macs.**
Per the phase rule (nothing partial ships as done), mobile v1 is
recorded as *source delivered, builds pending*, and Phase status in
`docs/PHASES.md` says the same.
