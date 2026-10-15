"""Evaluation gate: the deployment may not ship below a quality bar.

Runs a graded case set through a responder function, scores exact-match
and keyword-containment per case, and reports a pass/fail verdict against
a threshold. In CI this is the step that blocks a merge when model or
prompt changes regress answer quality.
"""


def score_case(expected, actual):
    """1.0 exact (case-insensitive), partial credit for containing the key phrase."""
    exp = expected.strip().lower()
    act = (actual or "").strip().lower()
    if act == exp:
        return 1.0
    if exp and exp in act:
        return 0.7
    return 0.0


def run_evals(cases, responder, threshold=0.8):
    """cases: [{'id', 'prompt', 'expected'}]; responder: prompt -> answer text."""
    results = []
    for case in cases:
        actual = responder(case["prompt"])
        results.append({
            "id": case["id"],
            "score": score_case(case["expected"], actual),
            "expected": case["expected"],
            "actual": actual,
        })
    mean = sum(r["score"] for r in results) / len(results) if results else 0.0
    return {
        "mean_score": round(mean, 4),
        "threshold": threshold,
        "passed": mean >= threshold,
        "results": results,
    }
