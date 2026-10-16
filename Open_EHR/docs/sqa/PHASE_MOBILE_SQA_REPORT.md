# SQA Report — Mobile v1 source (iOS SwiftUI + Android Compose + KMP skeleton)

**Date:** 2026-10-15 (completion pass added the same day, after Min's
verdict "mobile is still not complete")
**Deliverable:** `mobile/` — native iOS app (SwiftUI, Universal),
native Android app (Kotlin/Jetpack Compose), KMP shared-contract
skeleton. Authored on the Linux build VM. This report follows the
standing gate in `docs/SQA.md`: a check that could not run is recorded
NOT RUN, never as passed.

## Status after the completion pass (2026-10-15)

- **Android — full Gradle build BLOCKED on this VM by a platform
  permission, with the toolchain fully installed and everything else
  runnable verified.** Temurin JDK 17.0.20, Gradle 8.7, and Android SDK
  platform 34 / build-tools 34.0.0 / platform-tools are installed in
  `~/workspace/toolchains/`, and the Gradle wrapper is now committed
  (wrapper jar verified, `gradle-wrapper.properties` pinned to Gradle
  8.7 with the distribution SHA-256). But `./gradlew
  :app:assembleDebug` cannot run here: **the assistant sandbox denies
  JVM-initiated TCP connections outright.** Primary evidence, captured
  from a raw-socket test JVM: the connection interceptor answers with
  the literal text *"muse: Other TCP connections is turned off for
  this assistant. To allow it, ask the user to open Muse settings ->
  Permissions -> Direct network protocols and switch other_tcp from
  Deny to Ask."* Corroboration: the Gradle daemon starts and listens,
  but the Gradle client's loopback connection never reaches it
  ("Could not receive a message from the daemon"); `sdkmanager`
  downloads fail the same way (its HTTP CONNECT is answered by the
  interceptor); Java client connects to loopback, to the host IP, and
  to dl.google.com:443 are all intercepted while Python/curl clients
  work normally; the block follows the JVM into a fresh network
  namespace (`Network is unreachable`). **The unblock is a user-side
  setting only Min can change: Muse Settings → Permissions → Direct
  network protocols → `other_tcp`: Deny → Ask.** The moment it is
  flipped, the one-command build is
  `cd mobile/android && ./gradlew :app:assembleDebug` with
  `JAVA_HOME`/`ANDROID_HOME` pointed at `~/workspace/toolchains/`
  (JDK + SDK already in place). Until then, the first real Android
  compile is defined to happen in CI (below) or in Android Studio.
  **No APK is claimed: none exists yet, and this report does not
  pretend otherwise.**
- **iOS — CI-defined; first real compile still NOT RUN locally.**
  SwiftUI cannot compile on Linux (no iOS SDK). What was added: a
  **shared scheme** (`OpenCare.xcscheme`, target ID verified against
  the pbxproj) so `xcodebuild -scheme OpenCare` works from a clean
  checkout at all, and a GitHub Actions workflow
  (`Open_EHR/.github/workflows/mobile.yml`, staged for the repo root)
  whose macOS job builds with the committed project and retries once
  via the XcodeGen/`project.yml` fallback. New on-VM verification: a
  real Swift 6.0.3 toolchain was installed — **all 12 Swift sources
  pass `swiftc -parse`**, and Models/Config/APIClient/Session pass a
  full `swiftc -typecheck` (Linux shims on temp copies only; shipped
  sources unchanged except an explicit `import Combine` added to
  Session.swift). xcodebuild itself remains NOT RUN — first execution
  is the CI macOS runner or Xcode on Min's Air.
- **CI (both platforms):** `.github/workflows/mobile.yml` — Android
  job (ubuntu-latest, JDK 17, `./gradlew :app:assembleDebug`, APK
  uploaded as an artifact) and iOS job (macos-latest, xcodebuild
  unsigned simulator build + XcodeGen fallback). YAML validated by
  parsing. **Neither job has run yet** — they run on the next publish
  to BioMedicalAI (the file is staged at `Open_EHR/.github/workflows/`
  and moves to the repo root `.github/workflows/` in the publish
  step, which is the layout its `Open_EHR/mobile/**` path filters
  assume).

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
| Swift syntax, all 12 sources (completion pass) | real Swift 6.0.3 toolchain (`swiftc -parse`, installed to `~/workspace/toolchains/swift`) | PASS ×12 — zero diagnostics |
| Swift semantics, core layer (completion pass) | `swiftc -typecheck` on Models + Config + APIClient + Session | PASS — zero errors. Linux-only shims used on temp copies/stubs (FoundationNetworking import; signature-exact KeychainStore/Combine stubs for Apple-only frameworks); shipped sources untouched except explicit `import Combine` in Session.swift |
| Swift deep review (completion pass) | line-by-line read of all 12 files | PASS with findings recorded below — every referenced View exists (6 views + RootView + SignOutView), iOS 17 deployment target matches the APIs used (`ContentUnavailableView`, two-parameter `onChange`, `LabeledContent`), no nested Button-inside-NavigationLink (the known dead-tap pattern), no TODO/FIXME/placeholders |
| Shared scheme present (completion pass) | `OpenCare.xcscheme` added; XML parses; `BlueprintIdentifier` equals the PBXNativeTarget ID in the pbxproj | PASS — previously NO shared scheme existed, so `xcodebuild -scheme OpenCare` from a clean checkout would have failed; fixed |
| Android resources + manifest vs real toolchain (completion pass) | Android SDK build-tools 34.0.0 `aapt2 compile` + `aapt2 link` against `platforms/android-34/android.jar` | PASS — compiled `res/`, linked a skeleton APK (package `org.opencare.ehr`, INTERNET permission, label OpenCare, R.java generated). The manifest's missing `package` attribute is by design (AGP injects it from `namespace`); the standalone link used a temp copy with it injected — shipped manifest unchanged |
| Gradle wrapper completeness (completion pass) | `gradlew`, `gradlew.bat`, `gradle-wrapper.jar` (valid zip, wrapper classes present), `gradle-wrapper.properties` | PASS — pinned to Gradle 8.7 (AGP 8.5.2 minimum) with `distributionSha256Sum` computed from the distribution zip downloaded from services.gradle.org. Files sourced from the official gradle/gradle v8.7.0 tag (the `gradle wrapper` task itself could not run — see blocker above) |
| CI workflow validity (completion pass) | `yaml.safe_load` on `.github/workflows/mobile.yml` | PASS — parses; jobs `android-build` + `ios-build` present with the specified steps |

## NOT RUN (requires Min's Macs / devices)

- **Swift compilation (full)** — a Linux Swift 6.0.3 toolchain now
  exists on the VM and was used for parse + core-layer typecheck
  (above), but SwiftUI/UIKit do not exist off macOS, so full
  compilation cannot happen here at all. First build: the CI macOS
  job or Xcode on the MacBook Air. The pbxproj is the highest-risk
  artifact in this drop (hand-authored); `project.yml` is the
  documented fallback, and the CI job exercises it automatically if
  the primary build fails.
- **iOS Simulator / device run** — not run. Runtime behaviour (tab
  shell, Keychain on device, ATS behaviour on a physical device) is
  unverified.
- **Android compilation / Gradle sync** — **BLOCKED on this VM** (not
  for lack of a toolchain: JDK 17 + Gradle 8.7 + SDK 34 are installed,
  the wrapper is committed). The sandbox denies JVM-initiated TCP
  (`other_tcp` = Deny), which kills both the Gradle client↔daemon
  connection and all Java dependency downloads. Exact evidence and
  the user-side fix are in the completion-pass status section above.
  First real build: the CI Android job or Android Studio. Zero Kotlin
  compile errors could be observed either way — the build never
  reached compilation — so **no claim is made that the Kotlin sources
  compile**; they carry the v1 structural checks, a full manual
  review, and the aapt2 resource pass from the completion pass.
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
3. ~~Android Gradle wrapper must be generated on first open~~ —
   RESOLVED in the completion pass: the wrapper is committed and
   checksum-pinned; no generation step remains.
4. Android dev token is stored in plain DataStore preferences — fine
   for dev tokens, recorded in the README as must-fix before any
   production OIDC token is stored.

## Gate verdict

**Source delivery: PASS on every check runnable on this VM** — now
including real-compiler Swift parse (12/12) + typecheck of the core
layer, a real aapt2 resource link, a committed checksum-pinned Gradle
wrapper, a committed shared Xcode scheme, and a valid CI workflow.
**Build/runtime: STILL NOT VERIFIED.** Android's Gradle build is
blocked on this VM by the sandbox's `other_tcp` = Deny permission
(JVM TCP) — a user-side setting, with the exact evidence recorded
above; iOS's xcodebuild cannot exist on Linux by construction. Both
first real compiles are fully defined (CI jobs + local instructions
in `mobile/README.md`) but **have not run anywhere yet**. Per the
phase rule (nothing partial ships as done), mobile v1 remains
recorded as *source delivered, builds pending first execution*, and
Phase status in `docs/PHASES.md` says the same.
