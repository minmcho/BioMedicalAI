import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type {
  DuplicateCandidate,
  Patient,
  RegistrationResult,
} from "../api/types";

// Patient registration + edit forms (Front Door pass), shared by the
// Patients page, the chart header and the front-desk flow.
//
// Quality bar (the user's standing standard): every field is
// validated by value type on BOTH sides — the client checks here give
// inline, actionable errors before anything is sent, and the server
// re-validates and its 422 detail is shown verbatim. A select is
// never a blank mystery list: the only select here is sex, whose
// options are the FHIR AdministrativeGender value set (a code set,
// not site data).

export const GENDERS = ["female", "male", "other", "unknown"] as const;

/** The front-desk workflow, shown wherever a page participates in
 *  it (design standard: workflow state visible, text + colour, never
 *  colour alone). Steps: Register → Book → Check in. */
export function FrontDeskSteps({
  current,
}: {
  current: "register" | "book" | "checkin";
}) {
  const steps: Array<{ key: typeof current; label: string }> = [
    { key: "register", label: "1 · Register" },
    { key: "book", label: "2 · Book appointment" },
    { key: "checkin", label: "3 · Check in" },
  ];
  return (
    <div className="workflow-steps" aria-label="Front-desk workflow">
      {steps.map((s, i) => (
        <span key={s.key}>
          {i > 0 && <span aria-hidden="true"> → </span>}
          <span className={s.key === current ? "badge step-current" : "badge"}>
            {s.label}
            {s.key === current ? " (current)" : ""}
          </span>
        </span>
      ))}
    </div>
  );
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PHONE_CHARS_RE = /^[+()\-.\s\d]+$/;

export function isEmail(value: string): boolean {
  return EMAIL_RE.test(value);
}

export interface PatientFormValues {
  family: string;
  given: string;
  birthDate: string;
  gender: string;
  phone: string;
  email: string;
  addressLine: string;
  city: string;
  state: string;
  postalCode: string;
  emergencyName: string;
  emergencyPhone: string;
}

/** Is `value` a real calendar date in YYYY-MM-DD form? (2026-02-30
 *  is not — Date parsing alone would silently roll it over.) */
export function isRealDate(value: string): boolean {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return false;
  const [, y, mo, d] = m.map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === mo - 1 &&
    dt.getUTCDate() === d
  );
}

export function checkPhone(value: string): string | null {
  if (!PHONE_CHARS_RE.test(value)) {
    return "Use digits and phone punctuation only (+ ( ) - . spaces).";
  }
  const digits = value.replace(/\D/g, "");
  if (digits.length < 7 || digits.length > 15) {
    return "A phone number has 7 to 15 digits.";
  }
  return null;
}

/** Client-side typed validation, mirroring the server rules in
 *  backend/app/registration.py + validate_fields.py. Returns a
 *  field -> message map; empty means valid. */
export function validatePatientForm(
  v: PatientFormValues,
): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!v.family.trim()) errors.family = "Family name is required.";
  if (!v.given.trim()) errors.given = "Given name is required.";
  if (!v.birthDate) {
    errors.birthDate = "Date of birth is required.";
  } else if (!isRealDate(v.birthDate)) {
    errors.birthDate = "Enter a real calendar date (YYYY-MM-DD).";
  } else if (v.birthDate > new Date().toISOString().slice(0, 10)) {
    errors.birthDate = "Date of birth cannot be in the future.";
  }
  if (!GENDERS.includes(v.gender as (typeof GENDERS)[number])) {
    errors.gender = "Select a value from the list.";
  }
  if (v.email.trim() && !EMAIL_RE.test(v.email.trim())) {
    errors.email = "That does not look like an email address (name@example.org).";
  }
  if (v.phone.trim()) {
    const msg = checkPhone(v.phone.trim());
    if (msg) errors.phone = msg;
  }
  if (v.emergencyPhone.trim()) {
    const msg = checkPhone(v.emergencyPhone.trim());
    if (msg) errors.emergencyPhone = msg;
    if (!v.emergencyName.trim()) {
      errors.emergencyName =
        "The emergency contact's name is required when a phone is given.";
    }
  }
  return errors;
}

export const EMPTY_PATIENT_FORM: PatientFormValues = {
  family: "",
  given: "",
  birthDate: "",
  gender: "unknown",
  phone: "",
  email: "",
  addressLine: "",
  city: "",
  state: "",
  postalCode: "",
  emergencyName: "",
  emergencyPhone: "",
};

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <span className="field-error" id={id} role="alert">
      {message}
    </span>
  );
}

function serverErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  }
  return err instanceof Error ? err.message : String(err);
}

function PatientFields({
  values,
  errors,
  onChange,
}: {
  values: PatientFormValues;
  errors: Record<string, string>;
  onChange: (patch: Partial<PatientFormValues>) => void;
}) {
  const text = (
    key: keyof PatientFormValues,
    label: string,
    props: Record<string, string> = {},
  ) => (
    <label className="field" key={key}>
      {label}
      <input
        aria-label={label}
        value={values[key]}
        onChange={(e) => onChange({ [key]: e.target.value })}
        aria-invalid={errors[key] ? true : undefined}
        aria-describedby={errors[key] ? `err-${key}` : undefined}
        {...props}
      />
      <FieldError id={`err-${key}`} message={errors[key]} />
    </label>
  );
  return (
    <>
      {text("family", "Family name *")}
      {text("given", "Given name *")}
      {text("birthDate", "Date of birth *", { type: "date" })}
      <label className="field">
        Sex *
        <select
          aria-label="Sex"
          value={values.gender}
          onChange={(e) => onChange({ gender: e.target.value })}
        >
          {GENDERS.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
        <FieldError id="err-gender" message={errors.gender} />
      </label>
      {text("phone", "Phone", { type: "tel", placeholder: "+1 555-0101" })}
      {text("email", "Email", { type: "email", placeholder: "name@example.org" })}
      {text("addressLine", "Address line")}
      {text("city", "City")}
      {text("state", "State")}
      {text("postalCode", "Postal code")}
      {text("emergencyName", "Emergency contact name")}
      {text("emergencyPhone", "Emergency contact phone", { type: "tel" })}
    </>
  );
}

function DuplicateWarning({
  candidates,
  busy,
  onConfirm,
  onCancel,
}: {
  candidates: DuplicateCandidate[];
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="state state-error" role="alert">
      <p>
        <strong>Possible duplicate.</strong> A patient with the same name
        and date of birth already exists:
      </p>
      <ul>
        {candidates.map((c) => (
          <li key={c.id}>
            {c.name} · born {c.birthDate ?? "—"} · id {c.id}
            {c.active ? "" : " · entered in error"}
          </li>
        ))}
      </ul>
      <p className="hint">
        If this is the same person, cancel and open their chart instead.
        Register anyway only for a genuinely different person.
      </p>
      <div className="row-actions">
        <button type="button" disabled={busy} onClick={onConfirm}>
          Register anyway — a different person
        </button>
        <button type="button" className="linklike" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Register a new patient. Duplicate flow: a pre-check runs first;
 *  matches (or a server 409) show the warning panel and registration
 *  only proceeds on the explicit "Register anyway" action. */
export function RegisterPatientForm({
  client,
  onRegistered,
}: {
  client: ApiClient;
  onRegistered?: (result: RegistrationResult) => void;
}) {
  const [values, setValues] = useState<PatientFormValues>(EMPTY_PATIENT_FORM);
  const [manualId, setManualId] = useState({ system: "", value: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<RegistrationResult | null>(null);

  const patch = (p: Partial<PatientFormValues>) =>
    setValues((v) => ({ ...v, ...p }));

  async function submit(confirmDuplicate: boolean) {
    const errs = validatePatientForm(values);
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    if (manualId.value.trim() && !manualId.system.trim()) {
      setFormError("A manual identifier needs both a system and a value.");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      if (!confirmDuplicate) {
        const candidates = await client.patientDuplicateCheck({
          family: values.family.trim(),
          given: values.given.trim(),
          birthDate: values.birthDate,
        });
        if (candidates.length > 0) {
          setDuplicates(candidates);
          return;
        }
      }
      const result = await client.registerPatient({
        family: values.family.trim(),
        given: values.given.trim(),
        birthDate: values.birthDate,
        gender: values.gender,
        phone: values.phone.trim() || undefined,
        email: values.email.trim() || undefined,
        identifiers: manualId.value.trim()
          ? [{ system: manualId.system.trim(), value: manualId.value.trim() }]
          : undefined,
        address: {
          line: values.addressLine.trim() || undefined,
          city: values.city.trim() || undefined,
          state: values.state.trim() || undefined,
          postalCode: values.postalCode.trim() || undefined,
        },
        emergency_contact: {
          name: values.emergencyName.trim() || undefined,
          phone: values.emergencyPhone.trim() || undefined,
        },
        confirm_duplicate: confirmDuplicate || undefined,
      });
      setDuplicates(null);
      setDone(result);
      setValues(EMPTY_PATIENT_FORM);
      setManualId({ system: "", value: "" });
      onRegistered?.(result);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const body = err.body as { candidates?: DuplicateCandidate[] } | null;
        if (body?.candidates?.length) {
          setDuplicates(body.candidates);
          return;
        }
      }
      setFormError(`Registration failed: ${serverErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label="Register patient"
      onSubmit={(e) => {
        e.preventDefault();
        void submit(false);
      }}
    >
      <PatientFields values={values} errors={errors} onChange={patch} />
      <label className="field">
        Identifier system (optional)
        <input
          aria-label="Identifier system"
          value={manualId.system}
          onChange={(e) => setManualId({ ...manualId, system: e.target.value })}
          placeholder="urn:opencare:mrn"
        />
      </label>
      <label className="field">
        Identifier value (optional)
        <input
          aria-label="Identifier value"
          value={manualId.value}
          onChange={(e) => setManualId({ ...manualId, value: e.target.value })}
          placeholder="EXT-99"
        />
      </label>
      <button type="submit" disabled={busy}>
        Register patient
      </button>
      {formError && (
        <p className="error-note" role="alert">
          {formError}
        </p>
      )}
      {done && (
        <p className="state state-empty" role="status">
          Registered {done.name?.[0]?.family ?? ""}{" "}
          {done.name?.[0]?.given?.[0] ?? ""} — MRN {done.mrn} (patient id{" "}
          {done.id}).
        </p>
      )}
      {duplicates && (
        <DuplicateWarning
          candidates={duplicates}
          busy={busy}
          onConfirm={() => void submit(true)}
          onCancel={() => setDuplicates(null)}
        />
      )}
    </form>
  );
}

export function patientToFormValues(patient: Patient): PatientFormValues {
  const name = patient.name?.[0];
  const telecom = (patient.telecom as
    | Array<{ system?: string; value?: string }>
    | undefined) ?? [];
  const address = (patient.address as
    | Array<{
        line?: string[];
        city?: string;
        state?: string;
        postalCode?: string;
      }>
    | undefined)?.[0];
  const contact = (patient.contact as
    | Array<{
        name?: { text?: string };
        telecom?: Array<{ system?: string; value?: string }>;
      }>
    | undefined)?.[0];
  const phoneOf = (list: Array<{ system?: string; value?: string }>) =>
    list.find((t) => t.system === "phone")?.value ?? "";
  return {
    family: name?.family ?? "",
    given: name?.given?.[0] ?? "",
    birthDate: patient.birthDate ?? "",
    gender: patient.gender ?? "unknown",
    phone: phoneOf(telecom),
    email: telecom.find((t) => t.system === "email")?.value ?? "",
    addressLine: address?.line?.[0] ?? "",
    city: address?.city ?? "",
    state: address?.state ?? "",
    postalCode: address?.postalCode ?? "",
    emergencyName: contact?.name?.text ?? "",
    emergencyPhone: phoneOf(contact?.telecom ?? []),
  };
}

/** Build the full replacement Patient body for the FHIR update: the
 *  stored resource with the edited fields applied (everything else —
 *  identifiers, extensions, unknown fields — is preserved). */
export function formValuesToPatientBody(
  patient: Patient,
  v: PatientFormValues,
): Record<string, unknown> {
  const telecom: Array<{ system: string; value: string }> = [];
  if (v.phone.trim()) telecom.push({ system: "phone", value: v.phone.trim() });
  if (v.email.trim()) telecom.push({ system: "email", value: v.email.trim() });
  const body: Record<string, unknown> = {
    ...patient,
    resourceType: "Patient",
    name: [{ family: v.family.trim(), given: [v.given.trim()] }],
    birthDate: v.birthDate,
    gender: v.gender,
    telecom,
  };
  if (v.addressLine.trim() || v.city.trim() || v.state.trim() || v.postalCode.trim()) {
    body.address = [
      {
        line: v.addressLine.trim() ? [v.addressLine.trim()] : [],
        city: v.city.trim(),
        state: v.state.trim(),
        postalCode: v.postalCode.trim(),
      },
    ];
  } else {
    delete body.address;
  }
  if (v.emergencyName.trim() || v.emergencyPhone.trim()) {
    body.contact = [
      {
        name: { text: v.emergencyName.trim() },
        relationship: [{ text: "Emergency contact" }],
        telecom: v.emergencyPhone.trim()
          ? [{ system: "phone", value: v.emergencyPhone.trim() }]
          : [],
      },
    ];
  } else {
    delete body.contact;
  }
  return body;
}

/** Edit an existing patient (demographics) + entered-in-error. Save
 *  is a full-resource PUT /fhir/Patient/{id}; entered-in-error sets
 *  active: false — records are never deleted. */
export function PatientEditForm({
  client,
  patient,
  onSaved,
  onCancel,
}: {
  client: ApiClient;
  patient: Patient;
  onSaved?: (updated: Patient) => void;
  onCancel?: () => void;
}) {
  const [values, setValues] = useState<PatientFormValues>(() =>
    patientToFormValues(patient),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const isVoid = patient.active === false;

  const patch = (p: Partial<PatientFormValues>) =>
    setValues((v) => ({ ...v, ...p }));

  async function save() {
    const errs = validatePatientForm(values);
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    try {
      const updated = await client.updatePatient(
        patient.id as string,
        formValuesToPatientBody(patient, values),
      );
      onSaved?.(updated);
    } catch (err) {
      setFormError(`Save failed: ${serverErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function voidRecord() {
    setBusy(true);
    setFormError(null);
    try {
      const body = formValuesToPatientBody(patient, values);
      body.active = false;
      const updated = await client.updatePatient(patient.id as string, body);
      onSaved?.(updated);
    } catch (err) {
      setFormError(`Entered-in-error failed: ${serverErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      className="inline-form"
      aria-label={`Edit patient ${patient.id ?? ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <PatientFields values={values} errors={errors} onChange={patch} />
      <button type="submit" disabled={busy}>
        Save changes
      </button>
      {onCancel && (
        <button type="button" className="linklike" onClick={onCancel}>
          Cancel
        </button>
      )}
      {formError && (
        <p className="error-note" role="alert">
          {formError}
        </p>
      )}
      {!isVoid && !confirmVoid && (
        <p className="hint">
          Wrong record?{" "}
          <button
            type="button"
            className="linklike"
            onClick={() => setConfirmVoid(true)}
          >
            Mark entered in error
          </button>{" "}
          — the record is kept with active: false, never deleted.
        </p>
      )}
      {!isVoid && confirmVoid && (
        <div className="state state-error" role="alert">
          <p>
            Mark this record <strong>entered in error</strong>? It stays in
            the system with active: false and drops out of normal use.
          </p>
          <div className="row-actions">
            <button type="button" disabled={busy} onClick={() => void voidRecord()}>
              Yes — mark entered in error
            </button>
            <button
              type="button"
              className="linklike"
              onClick={() => setConfirmVoid(false)}
            >
              Keep the record
            </button>
          </div>
        </div>
      )}
      {isVoid && (
        <p className="hint">
          This record is marked entered in error (active: false).
        </p>
      )}
    </form>
  );
}
