// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { namespace as getNamespace, type Namespace, RegistryUnavailable } from "@/lib/registry";

import { RegistryUnavailablePage } from "@/app/registry-unavailable";

export const dynamic = "force-dynamic";

type Params = { namespace: string };

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  return { title: (await params).namespace };
}

const TYPE_WORDS: Record<string, string> = {
  community: "Community publisher: anyone can publish; nobody has checked who they are.",
  verified: "Verified publisher: the registry has checked who publishes here.",
  institutional: "Institutional publisher: a health organization, checked by the registry.",
};

const PUBLISHER_BADGE = new Set(["verified", "institutional", "community"]);

export default async function PublisherPage({ params }: { params: Promise<Params> }) {
  const name = (await params).namespace;
  let found: Namespace | null;
  try {
    found = await getNamespace(name);
  } catch (error) {
    if (error instanceof RegistryUnavailable) return <RegistryUnavailablePage retry={`/publishers/${encodeURIComponent(name)}`} />;
    throw error;
  }
  if (!found) notFound();
  const badgeClass = PUBLISHER_BADGE.has(found.publisher_type) ? found.publisher_type : null;
  const revoked = found.keys.filter((key) => key.revoked_at).length;
  const live = found.keys.length - revoked;
  return (
    <div className="wrap page-body">
      <nav className="breadcrumb muted" aria-label="Breadcrumb">
        <Link href="/">Agent Hub</Link>
      </nav>

      <header className="pack-hero">
        <div className="pack-hero-meta row">
          <span className="mono pack-id-pill">@{found.name}</span>
          <span className={badgeClass ? `badge ${badgeClass}` : "badge"}>{found.publisher_type}</span>
        </div>
        <h1>{found.display_name}</h1>
        <p className="lead">{TYPE_WORDS[found.publisher_type] ?? found.publisher_type}</p>
        <p className="publisher-meta muted">
          {found.packs.length} published {found.packs.length === 1 ? "agent" : "agents"} · {live} signing{" "}
          {live === 1 ? "key" : "keys"}
          {revoked ? <> ({revoked} revoked)</> : null}
        </p>
      </header>

      <section className="panel" aria-labelledby="publisher-packs-heading">
        <h2 id="publisher-packs-heading">Published agents</h2>
        {found.packs.length ? (
          <ul className="publisher-packs">
            {found.packs.map((id) => {
              const [ns = found.name, ...rest] = id.split("/");
              const packName = rest.join("/");
              const href = `/packs/${encodeURIComponent(ns)}/${encodeURIComponent(packName)}`;
              return (
                <li key={id}>
                  <Link href={href} className="publisher-pack-link">
                    <span className="mono">{id}</span>
                    <span className="card-cta" aria-hidden="true">Inspect →</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">Nothing published yet.</p>
        )}
      </section>

      <section className="panel" aria-labelledby="signing-keys-heading">
        <h2 id="signing-keys-heading">Signing keys</h2>
        <p className="muted group-sub">
          Every archive from this publisher is signed by one of these keys; <code>mendari pull</code> checks it. A revoked
          key signs nothing after it was revoked: only what the registry received before then still verifies.
        </p>
        <ul className="key-list">
          {found.keys.map((key) => (
            <li key={key.key_id} className={key.revoked_at ? "revoked" : undefined}>
              <span className="mono">{key.key_id}</span>
              {key.revoked_at ? <span className="badge severity-high">Revoked {key.revoked_at.slice(0, 10)}</span> : null}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
