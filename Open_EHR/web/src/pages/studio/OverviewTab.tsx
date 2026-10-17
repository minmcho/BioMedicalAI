import type { ApiClient } from "../../api/client";
import type { AuditEntry, ModulePack, PackInstallation, StudioConfigObject } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { CONFIG_KINDS, groupConfigs } from "./helpers";

interface OverviewData {
  configs: StudioConfigObject[];
  packs: ModulePack[];
  installations: PackInstallation[];
  audit: AuditEntry[];
}

/** Studio overview: the organisation profile from the published branding
 *  config, the published-config inventory per kind, pack state counts,
 *  and the most recent audit activity. All reads, no writes. */
export function OverviewTab({ client }: { client: ApiClient }) {
  const { data, loading, error } = useAsync<OverviewData>(async () => {
    const [configs, packs, installations, audit] = await Promise.all([
      client.listConfigs(),
      client.listPacks(),
      client.listInstallations(),
      client.listAudit(8),
    ]);
    return { configs, packs, installations, audit };
  }, [client]);

  if (loading) return <Loading label="Loading Studio overview…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No Studio data." />;

  const branding = data.configs.find(
    (c) => c.kind === "branding" && c.status === "published" && c.key === "default",
  ) ?? data.configs.find((c) => c.kind === "branding" && c.status === "published");
  const bp = branding?.payload ?? {};
  const orgName = typeof bp.orgName === "string" ? bp.orgName : null;
  const portalTitle = typeof bp.portalTitle === "string" ? bp.portalTitle : null;
  const primaryColor = typeof bp.primaryColor === "string" ? bp.primaryColor : null;
  const supportContact = typeof bp.supportContact === "string" ? bp.supportContact : null;

  const groups = groupConfigs(data.configs);
  const packCounts = new Map<string, number>();
  for (const inst of data.installations) {
    packCounts.set(inst.status, (packCounts.get(inst.status) ?? 0) + 1);
  }
  const notInstalled = data.packs.length - data.installations.length;

  return (
    <div className="studio-overview">
      <section className="card">
        <h3>Organisation profile</h3>
        {orgName ? (
          <>
            <p className="studio-org-name">
              {primaryColor ? (
                <span
                  className="studio-color-chip"
                  style={{ background: primaryColor }}
                  aria-hidden="true"
                />
              ) : null}{" "}
              {orgName}
            </p>
            {portalTitle ? <p className="pack-detail">Portal: {portalTitle}</p> : null}
            {supportContact ? <p className="pack-detail">Support: {supportContact}</p> : null}
            <p className="pack-detail">From published branding config v{branding?.version}.</p>
          </>
        ) : (
          <p className="hint">
            No published branding config yet — set the organisation profile in the Branding tab.
          </p>
        )}
      </section>

      <section className="card">
        <h3>Published configuration inventory</h3>
        {groups.length === 0 ? (
          <Empty label="No configuration objects yet." />
        ) : (
          <table className="studio-table">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Objects</th>
                <th>Published</th>
              </tr>
            </thead>
            <tbody>
              {CONFIG_KINDS.map((kind) => {
                const kindGroups = groups.filter((g) => g.kind === kind);
                const published = kindGroups
                  .map((g) => {
                    const p = g.versions.find((v) => v.status === "published");
                    return p ? `${g.key} v${p.version}` : null;
                  })
                  .filter(Boolean) as string[];
                return (
                  <tr key={kind}>
                    <td>{kind}</td>
                    <td>{kindGroups.length}</td>
                    <td>{published.length > 0 ? published.join(", ") : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Module packs</h3>
        <p>
          {data.packs.length} packs available · {data.installations.length} installed ·{" "}
          {notInstalled} not installed
        </p>
        <ul>
          {[...packCounts.entries()].map(([status, count]) => (
            <li key={status}>
              {status}: {count}
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h3>Recent audit activity</h3>
        {data.audit.length === 0 ? (
          <Empty label="No audit entries yet." />
        ) : (
          <table className="studio-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Subject</th>
              </tr>
            </thead>
            <tbody>
              {data.audit.map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at ? new Date(e.created_at).toLocaleString() : "—"}</td>
                  <td>{e.actor}</td>
                  <td>{e.action}</td>
                  <td>{e.entity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="hint">Full hash-chained log in the Audit tab.</p>
      </section>
    </div>
  );
}
