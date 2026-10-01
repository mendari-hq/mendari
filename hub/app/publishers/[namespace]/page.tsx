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
    <>
      <p className="muted"><Link href="/">Decision Packs</Link></p>
      <h1>{found.display_name}</h1>
      <p className="lead">{TYPE_WORDS[found.publisher_type] ?? found.publisher_type}</p>
      <h2>Packs</h2>
      {found.packs.length ? (
        <ul>{found.packs.map((id) => <li key={id}><Link href={`/packs/${id}`}>{id}</Link></li>)}</ul>
      ) : (
        <p className="muted">Nothing published yet.</p>
      )}
      <h2>Signing keys</h2>
      <p className="muted">Every archive from this publisher is signed by one of these keys; <code>mendari pull</code> checks it.</p>
      <ul>{found.keys.map((key) => <li key={key.key_id} className="mono">{key.key_id}</li>)}</ul>
    </>
  );
}
