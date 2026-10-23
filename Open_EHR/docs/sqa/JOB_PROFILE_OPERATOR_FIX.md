# Job-Profile Operator Fix — SQA Note (2026-10-20)

## Defect (live, Min's Air, after Air sync Run 9)

The administrator's card Home was missing most modules — Laboratory,
Pharmacy, Surgery ("Surgical Ops") and System Studio among them.
Only the six modules of the seeded `executive` job profile rendered.

## Root-cause chain

1. The demo staff seed (`backend/app/people.py`, `_STAFF_SEED`) links
   the admin sign-in to a staff record: staff-011 "Elena Petrova, MD",
   username `dev-admin`, job_role `executive`. (Realistic org data —
   deliberately kept; the link is not the defect.)
2. The caller's job role is resolved from that link
   (`configstore.caller_job_role`: StaffMember.username match, else
   the UserAccount `staff_id` link). The backend exposes it on
   `GET /home/effective-modules`, and the web shell derives the same
   value from the staff directory (`App.tsx` listStaff lookup).
3. Both menu resolvers then intersected the role-resolved menu with
   the published job profile for that job role — backend
   `configstore.effective_modules`, web `session.ts applyJobProfile`
   (called from `App.tsx`). The seeded `executive` profile lists 6
   modules (command, beds, workforce, pophealth, reports,
   analytics), so the intersection silently removed the other 23
   from the operator's Home.

Job profiles are a staff-scoping feature. Operator roles
(admin / configurator) administer the whole system and must always
receive the full role-resolved menu.

## Fix (defence in depth, three layers; staff behaviour unchanged)

1. **Backend resolver** — `configstore.effective_modules` returns the
   role-resolved modules WITHOUT the job-profile intersection when
   the caller's role is admin or configurator (new
   `configstore.OPERATOR_ROLES` constant). Patient handling unchanged.
2. **Backend identity payload** — `GET /home/effective-modules` now
   reports `job_role: null` for admin/configurator callers (staff
   callers unchanged), so no consumer of the payload can profile an
   operator's menu down. NOTE: this tree has no literal `/auth/me`
   endpoint; this endpoint is the menu/identity surface that exposes
   job_role, and it is where the gate was applied.
   `caller_job_role` itself is NOT changed — its only consumers are
   `effective_modules` and this endpoint, and both are now gated
   (a test pins that it still resolves the underlying job role).
3. **Web shell** — `session.ts applyJobProfile` takes the caller's
   role as a new fourth parameter and returns the groups unchanged
   for admin/configurator; `App.tsx` passes `session.role`. Even a
   stale or future client that resolves an operator's staff job
   role cannot narrow the operator menu.

NOT changed, by design: the demo seed's staff-011 record; the
seeded job-profiles payload; staff (clinician/viewer/…) filtering.

## Tests (all run by the fixer, 2026-10-20)

Backend (`pytest`, project venv):
- `tests/test_home_cards.py` — the WS1a test that pinned the
  defective premise (`admin_is_executive_profiled`) rewritten as
  `test_effective_modules_endpoint_admin_operator_exempt`:
  dev-admin → job_role null + full 29-module admin resolution
  (studio/imports/lab/pharmacy/surgery present). New
  `test_effective_modules_endpoint_configurator_operator_exempt`:
  dev-configurator (department-head-linked) → job_role null + full
  28-module configurator resolution (30 − portal − pophealth, the
  two items whose published roles exclude configurator). The
  profiled-clinician endpoint test is unchanged and still passes —
  staff intersection intact.
- `tests/test_ux_shell_config.py` — new unit pins:
  admin exempt via the username link; configurator exempt via the
  UserAccount `staff_id` link; `caller_job_role` still resolves the
  underlying role in both cases.
- Files run: test_home_cards.py + test_ux_shell_config.py +
  test_seed_upgrade.py = **45 passed**; test_studio_config.py =
  **22 passed**.

Web (vitest): `session.test.ts` gains "applyJobProfile never
filters operator roles, even with a matching profile" (admin vs the
executive profile, configurator vs department-head, clinician staff
intersection re-pinned in the same test). Full web suite =
**534 passed** (baseline 533 + 1 new). Production build exit 0.

Full backend gate is the parent's to run; the fixer's runs above
cover every consumer of the changed code.

## Configuration coverage

Job profiles remain governed System Studio configuration
(`navigation/job-profiles`, draft → publish, as before) — sites
still scope STAFF menus entirely by configuration. The operator
exemption is a **role rule in code, not configuration**: no Studio
surface can narrow an operator's menu, by design.
