interface StepperProps {
  steps: string[];
  current: number;
}

/** Ordered step indicator (depth pass, Workstream 1). Each step is
 *  labelled with its text plus a state word (Done / Current / Pending)
 *  — state is never conveyed by colour alone. */
export function Stepper({ steps, current }: StepperProps) {
  return (
    <ol className="stepper" aria-label="Progress">
      {steps.map((label, i) => {
        const state = i < current ? "Done" : i === current ? "Current" : "Pending";
        return (
          <li
            key={`${label}-${i}`}
            className={`step step-${state.toLowerCase()}`}
            aria-current={i === current ? "step" : undefined}
          >
            <span className="step-label">{label}</span>{" "}
            <span className="step-state">({state})</span>
          </li>
        );
      })}
    </ol>
  );
}
