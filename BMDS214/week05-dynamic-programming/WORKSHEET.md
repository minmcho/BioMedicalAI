# Worksheet — Week 5: Dynamic Programming & Edit Distance

Fill this in by hand (or in your editor) BEFORE running the code.
The grid stays blank on purpose — predicting first, then checking against
`edit_distance()`, is the exercise. Review and push happen together.

Week 1 context: this is Step 1 of the 3-month prep (Bootcamp/PREP_3_MONTHS.md).
Step 2 (Anthropic Academy account + Claude 101) and Step 3 (the 300-word
interpretation at the bottom of this sheet) follow it.
Note: Fri Oct 16 also carries the HCAI packet submission — budget this
exercise at ~45 minutes so the week holds both.

## Exercise 1 — ACG -> AGG by hand

Conventions: rows = source "ACG" (top to bottom), columns = target "AGG"
(left to right). Cell (i, j) = edit distance between the first i characters
of ACG and the first j characters of AGG. First row and first column are
the base cases (transforming to/from the empty string).

|       |  "" |  A  |  G  |  G  |
|-------|-----|-----|-----|-----|
| **""** |     |     |     |     |
| **A**  |     |     |     |     |
| **C**  |     |     |     |     |
| **G**  |     |     |     |     |

- My predicted distance for ACG -> AGG, BEFORE filling the table: ______
- The operation sequence my traceback gives (mark the path you took
  through the table — diagonal / up / left — for the final cell):
  ______________________________________________
- Now check: run `edit_distance("ACG", "AGG")` and
  `edit_operations("ACG", "AGG")` from this folder's module.
- Did the code agree with my table? If not, which cell diverged first,
  and what rule did I misapply there?
  ______________________________________________

## Exercise 2 — a deletion that wins

- My test case (source, target) where a deletion is optimal:
  ______________________________________________
- One sentence on why the traceback prefers the deletion here:
  ______________________________________________

## Exercise 3 — complexity, in my own numbers

- Cells computed for two 10,000 bp sequences: ______________
- Memory for the full table at 8 bytes per cell: ______________
- What that implies for whole-genome use:
  ______________________________________________

## Exercise 4 — thinking ahead to Week 6

Why alignment needs scores and gaps, not a single distance number
(2-3 sentences, my own words):
______________________________________________________________
______________________________________________________________

## Step 3 deliverable — 300-word biological interpretation (draft)

Draft below; target ~300 words. Prompts only — the words are yours.

1. What edit distance actually measures, in biological terms:
2. Where it shows up in practice (a read vs a reference, a mutated gene
   vs wild type, variant description):
3. One limit of a plain distance number that Week 6 alignment addresses:

[Draft here]
