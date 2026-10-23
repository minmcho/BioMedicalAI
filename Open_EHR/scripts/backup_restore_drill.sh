#!/usr/bin/env bash
# OpenCare backup / restore drill — Phase 10 platform hardening.
#
# Proves, against a real database file, that:
#   1. a backup of the dev SQLite database can be taken (SQLite online
#      backup API — safe against a running server),
#   2. the backup restores into a scratch database,
#   3. the restored database is intact: per-table row counts match the
#      source AND the hash-chained audit log verifies end-to-end
#      (app.audit.verify_chain) on the restored copy.
#
# Usage: scripts/backup_restore_drill.sh [source-db] [work-dir]
# Defaults: backend/openehr.db and a mktemp work dir (kept on failure,
# removed on success unless KEEP=1). Exit 0 = drill passed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DB="${1:-$REPO_ROOT/backend/openehr.db}"
WORK_DIR="${2:-$(mktemp -d /tmp/opencare-drill.XXXXXX)}"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

if [ ! -f "$SOURCE_DB" ]; then
  echo "DRILL FAIL: source database not found: $SOURCE_DB" >&2
  exit 1
fi
mkdir -p "$WORK_DIR"
BACKUP_DB="$WORK_DIR/backup.db"
RESTORED_DB="$WORK_DIR/restored.db"

echo "== OpenCare backup/restore drill =="
echo "source:   $SOURCE_DB"
echo "work dir: $WORK_DIR"

# Steps 1+2: online backup, then a restore = a byte copy of the backup
# into a fresh file (what an operator does with the artifact), verified
# by the Python checker below against the source.
"$PYTHON" - "$SOURCE_DB" "$BACKUP_DB" "$RESTORED_DB" <<'PYEOF'
import sqlite3, sys
src_path, backup_path, restored_path = sys.argv[1:4]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(backup_path)
with dst:
    src.backup(dst)
dst.close(); src.close()
with open(backup_path, "rb") as fh, open(restored_path, "wb") as out:
    out.write(fh.read())
print(f"backup taken: {backup_path}")
print(f"restored to:  {restored_path}")
PYEOF

# Step 3: integrity — row counts per table + audit-chain verification
# on the RESTORED database, using the application's own code.
(cd "$REPO_ROOT/backend" && "$PYTHON" - "$SOURCE_DB" "$RESTORED_DB" <<'PYEOF'
import sqlite3, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.audit import verify_chain

def table_counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
              for t in tables}
    con.close()
    return counts

src_counts = table_counts(sys.argv[1])
rst_counts = table_counts(sys.argv[2])
mismatch = {t: (src_counts.get(t), rst_counts.get(t))
            for t in set(src_counts) | set(rst_counts)
            if src_counts.get(t) != rst_counts.get(t)}
if mismatch:
    print(f"DRILL FAIL: row-count mismatches: {mismatch}")
    sys.exit(1)
print(f"row counts match across {len(src_counts)} tables "
      f"(audit_log: {src_counts.get('audit_log', 0)} entries, "
      f"fhir_resources: {src_counts.get('fhir_resources', 0)})")

engine = create_engine(f"sqlite:///{sys.argv[2]}")
session = sessionmaker(bind=engine)()
ok = verify_chain(session)
session.close()
if not ok:
    print("DRILL FAIL: audit hash chain does not verify on the restored database")
    sys.exit(1)
print("audit hash chain verifies on the restored database")
print("DRILL PASS")
PYEOF
)

if [ "${KEEP:-0}" != "1" ] && [ -z "${2:-}" ]; then
  rm -rf "$WORK_DIR"
  echo "work dir removed (KEEP=1 to retain the artifacts)"
fi
