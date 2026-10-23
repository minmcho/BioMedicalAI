import { useAuth } from "../auth/AuthContext";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { AUTH_MODE, DEV_ACCOUNTS } from "../auth/session";

export function LoginPage() {
  const { signIn } = useAuth();
  // Pre-login there is no session, so the runtime config is the
  // built-in default branding; a signed-in shell re-renders with the
  // published branding after sign-in.
  const { branding } = useRuntimeConfig();

  return (
    <main className="login">
      {/* Rich Design Elevation (§8.5): brand block — 56px accent
          tile with the org initial, org name at display size. */}
      <div className="login-brand">
        <span className="login-brand-tile" aria-hidden="true">
          {branding.orgName.charAt(0)}
        </span>
        <h1>{branding.orgName}</h1>
      </div>
      <p className="tagline">Open-source, FHIR-native electronic health record.</p>

      {AUTH_MODE === "oidc" ? (
        <section className="card" aria-label="OIDC sign-in">
          <h2>Sign-in via identity provider</h2>
          <p>
            This deployment is configured for <strong>OIDC</strong> (for example Keycloak).
            Sign-in happens through your identity provider, and the OpenCare API must be
            called with a token it issued. This web build does not run its own SSO flow —
            use the provider sign-in supplied by your deployment.
          </p>
        </section>
      ) : (
        <section className="card" aria-label="Dev sign-in">
          <h2>Dev sign-in</h2>
          <p className="hint">
            Local development only. Pick a seeded role to sign in with its dev token
            (the backend&rsquo;s <code>AUTH_MODE=dev</code> accounts). Production uses OIDC.
          </p>
          <div className="account-list">
            {DEV_ACCOUNTS.map((account) => (
              <button
                key={account.role}
                type="button"
                className="account"
                onClick={() =>
                  signIn({ token: account.token, role: account.role, username: account.username })
                }
              >
                <span className="account-top">
                  <span className="account-label">{account.label}</span>
                  <span className="badge">{account.role}</span>
                </span>
                <span className="account-bottom">
                  <span className="account-meta">{account.username}</span>
                  <span className="account-chevron" aria-hidden="true">
                    ›
                  </span>
                </span>
              </button>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
