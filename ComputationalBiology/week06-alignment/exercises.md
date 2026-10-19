# Exercises — Week 6

1. By hand first: fill the Needleman–Wunsch table for "ACG" -> "AG"
   (scoring: match +2, mismatch -1, gap -2), predict the score and the
   alignment, then check with needleman_wunsch(). (Worksheet has the
   blank grid.)
2. Traceback tie-breaking: find a pair of short sequences where two
   different alignments reach the same optimal score. State which one
   this implementation returns and which traceback preference causes it.
3. Gap sensitivity: pick one pair where an indel is plausible
   (e.g. "ACGTACGT" vs "ACGACGT") and align it at gap = -1, -2, -5.
   Record the three alignments. At which penalty does the preferred
   explanation switch from "one indel" to "substitutions"?
4. Global vs local: construct a case where Smith–Waterman's answer is
   biologically the right one and Needleman–Wunsch's is misleading
   (a short conserved motif inside otherwise unrelated sequence is the
   classic shape). One paragraph on why.
5. Complexity: Week 5 counted table cells for 10,000 bp x 10,000 bp.
   Alignment adds a traceback of length O(n+m) — why is that not the
   bottleneck, and what does that imply about where production
   aligners spend their effort instead?
