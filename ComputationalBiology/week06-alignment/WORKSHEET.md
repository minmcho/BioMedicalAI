# Worksheet — Week 6: Sequence Alignment

Fill this in by hand (or in your editor) BEFORE running the code.
The grid stays blank on purpose — predicting first, then checking
against `needleman_wunsch()`, is the exercise. Review and push happen
together.

Week 2 context: this is the Build item for Mon Oct 19 – Sun Oct 25 in
Bootcamp/PREP_3_MONTHS.md. The same week also carries: FHIR R4 / OMOP
CDM reading with mapping notes into DataArchitecture/, and the
Databricks Free Edition setup (account + first Delta Lake notebook —
create/read/time travel), which feeds the Nov 1 lakehouse v0 freeze.
Budget the by-hand exercise at ~45 minutes.

## Exercise 1 — Needleman–Wunsch for "ACG" vs "AG", by hand

Scoring: match +2, mismatch -1, gap -2.
Conventions: rows = "ACG" (top to bottom), columns = "AG" (left to
right). Cell (i, j) = best global-alignment score for the first i
characters of ACG against the first j characters of AG. First row and
first column accumulate gap penalties (aligning against nothing).

|       |  "" |  A  |  G  |
|-------|-----|-----|-----|
| **""** |     |     |     |
| **A**  |     |     |     |
| **C**  |     |     |     |
| **G**  |     |     |     |

- My predicted optimal score, BEFORE filling the table: ______
- My predicted alignment (write both rows, using "-" for gaps):
  ______________________________________________
- Now check: run `needleman_wunsch("ACG", "AG")` from this folder's
  module.
- Did the code agree with my table? If not, which cell diverged first,
  and which of the three moves (diagonal / up / left) did I score
  wrong there?
  ______________________________________________

## Exercise 2 — gap-penalty sensitivity, in my own numbers

Pair I chose (indel plausible): ______________________________

- Alignment at gap = -1: ______________________________
- Alignment at gap = -2: ______________________________
- Alignment at gap = -5: ______________________________
- The penalty at which the explanation switches, and one sentence on
  what that says about gap penalties as a modelling choice:
  ______________________________________________

## Exercise 3 — global vs local

- Smith–Waterman on "TTTACGTAAA" vs "ACGT" returns:
  ______________________________________________
- Needleman–Wunsch on the same pair returns (score only): ______
- Why the scores differ, in one or two sentences:
  ______________________________________________

## Week 2 deliverable — 300-word biological interpretation (draft)

Draft below; target ~300 words. Prompts only — the words are yours.

1. What an alignment asserts that a distance number does not
   (positional homology — which base corresponds to which):
2. Where each variant shows up in practice (global: orthologs,
   full-length comparisons; local: read mapping, motif/domain search):
3. One way the scoring scheme can mislead, and how you would notice:

[Draft here]
