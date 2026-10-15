# Exercises — Week 5

1. By hand first: draw the DP table for "ACG" -> "AGG" on paper, predict
   the distance, then check with edit_distance().
2. Extend edit_operations() with a test where a deletion is optimal, and
   explain in one sentence why the traceback prefers it.
3. Complexity: for two 10,000 bp sequences, how many table cells are
   computed, and how much memory does the full table need if each cell is
   an 8-byte integer? What does that imply for whole-genome use?
4. Thinking ahead: hamming distance fails when lengths differ. State in
   your own words why alignment (Week 6) needs scores and gaps, not just
   a single distance number.
