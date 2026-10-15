"""Tenant-isolated record store.

Enterprise rule #1: a tenant can never read or write another tenant's data.
Every operation carries a tenant_id and the store enforces the boundary —
callers cannot opt out. This in-memory implementation mirrors the Postgres
design (every table has tenant_id, every query filters on it, row-level
security in production); it exists so the isolation contract is testable
without a database.
"""


class TenantViolation(Exception):
    """Raised when an operation would cross a tenant boundary."""


class TenantStore:
    def __init__(self):
        self._records = {}  # (tenant_id, record_id) -> record dict

    def put(self, tenant_id, record_id, record):
        if not tenant_id or not record_id:
            raise TenantViolation("tenant_id and record_id are required")
        stored = dict(record)
        stored["tenant_id"] = tenant_id
        stored["record_id"] = record_id
        self._records[(tenant_id, record_id)] = stored
        return stored

    def get(self, tenant_id, record_id):
        try:
            return self._records[(tenant_id, record_id)]
        except KeyError:
            raise TenantViolation(
                f"record {record_id!r} not visible to tenant {tenant_id!r}"
            ) from None

    def list_ids(self, tenant_id):
        return sorted(rid for (tid, rid) in self._records if tid == tenant_id)

    def delete(self, tenant_id, record_id):
        self.get(tenant_id, record_id)  # raises if not visible
        del self._records[(tenant_id, record_id)]
