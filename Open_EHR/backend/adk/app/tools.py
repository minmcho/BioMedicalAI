"""ADK function tools for OpenCare Studio agents.

One tool in v1: opencare_api_call. It calls back into the OpenCare API
using the CALLER's bearer <redacted> (forwarded by the backend per run) —
never a service credential, so an agent can only ever see what the
human who invoked it could see — and enforces the config allowlist
(app.config.is_path_allowed) before any request is made.
"""
from __future__ import annotations

import httpx

from .config import is_path_allowed
from .logredact import redact_text

_TIMEOUT = httpx.Timeout(30.0)


def make_api_call_tool(api_base_url: str, api_token: str, api_allowlist: list):
    base = (api_base_url or "").rstrip("/")

    def opencare_api_call(path: str, method: str = "GET") -> dict:
        """Call an OpenCare API endpoint and return its JSON response.

        Args:
            path: API path beginning with / (e.g. /fhir/Patient/pat-1,
                /lab/verification-queue). It must fall under one of this
                agent's allow-listed prefixes.
            method: HTTP method (default GET).

        Returns:
            A dict with status and body, or an error dict when blocked.
        """
        verb = (method or "GET").upper()
        if not is_path_allowed(path, verb, api_allowlist):
            return {"error": (f"Blocked: {verb} {path} is not under an "
                              "allow-listed prefix for this agent.")}
        if not base:
            return {"error": "No API base URL was provided for this run."}
        headers = {"Content-Type": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        try:
            # trust_env=False: the API base is operator configuration on
            # the local network; ambient proxy env vars must not reroute it.
            with httpx.Client(timeout=_TIMEOUT, trust_env=False) as client:
                resp = client.request(verb, f"{base}{path}", headers=headers)
        except Exception as exc:  # network failure -> honest tool error
            # Redacted (BD-53): transport errors can embed the URL /
            # echoed payload fragments; neither may carry PHI onward.
            return {"error": f"API call failed: {redact_text(str(exc))}"}
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:2000]
        return {"status": resp.status_code, "body": body}

    return opencare_api_call
