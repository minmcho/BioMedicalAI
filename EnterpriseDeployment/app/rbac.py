"""Role-based access control for the deployment.

Roles are ordered by privilege. A permission check answers one question:
may this role perform this action? Deny by default — anything not listed
is forbidden.
"""

ROLES = ("viewer", "analyst", "operator", "admin")

PERMISSIONS = {
    "viewer": {"read"},
    "analyst": {"read", "query_ai", "run_evals"},
    "operator": {"read", "query_ai", "run_evals", "ingest", "manage_jobs"},
    "admin": {"read", "query_ai", "run_evals", "ingest", "manage_jobs",
              "manage_users", "manage_tenants", "view_audit"},
}


def can(role, action):
    """True only if the role exists and the action is granted to it."""
    return action in PERMISSIONS.get(role, set())


def require(role, action):
    if not can(role, action):
        raise PermissionError(f"role {role!r} may not perform {action!r}")
