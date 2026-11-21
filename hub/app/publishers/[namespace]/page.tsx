// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { namespace as getNamespace } from "@/lib/registry";

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

export default async function PublisherPage({ params }: { params: Promise<Params> }) {
  const found = await getNamespace((await params).namespace);
  if (!found) notFound();
  return (
    <div className="wrap page-body">
      <nav className="breadcrumb muted" aria-label="Breadcrumb">
        <Link href="/">Agent Hub</Link>
      </nav>

      <header className="pack-hero">
        <div className="pack-hero-meta row">
          <span className="mono pack-id-pill">@{found.name}</span>
          <span className={`badge ${found.publisher_type}`}>{found.publisher_type}</span>
        </div>
        <h1>{found.display_name}</h1>
        <p className="lead">{TYPE_WORDS[found.publisher_type] ?? found.publisher_type}</p>
        <p className="publisher-meta muted">
          {found.packs.length} published {found.packs.length === 1 ? "agent" : "agents"} · {found.keys.length} signing{" "}
          {found.keys.length === 1 ? "key" : "keys"}
        </p>
      </header>

      <section className="panel" aria-labelledby="publisher-packs-heading">
        <h2 id="publisher-packs-heading">Published agents</h2>
        {found.packs.length ? (
          <ul className="publisher-packs">
            {found.packs.map((id) => (
              <li key={id}>
                <Link href={`/packs/${id}`} className="publisher-pack-link">
                  <span className="mono">{id}</span>
                  <span className="card-cta" aria-hidden="true">Inspect →</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Nothing published yet.</p>
        )}
      </section>

      <section className="panel" aria-labelledby="signing-keys-heading">
        <h2 id="signing-keys-heading">Signing keys</h2>
        <p className="muted group-sub">
          Every archive from this publisher is signed by one of these keys; <code>mendari pull</code> checks it.
        </p>
        <ul className="key-list">
          {found.keys.map((key) => (
            <li key={key.key_id} className="mono">{key.key_id}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}
