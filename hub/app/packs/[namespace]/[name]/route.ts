// SPDX-License-Identifier: Apache-2.0
// The address an agent's page had before it moved to /agents/<namespace>/<name>: a link, a
// bookmark or a search result from then still opens the page (a permanent redirect, same site).
import { permanentRedirect } from "next/navigation";

type Params = { namespace: string; name: string };

export async function GET(_request: Request, { params }: { params: Promise<Params> }) {
  const { namespace, name } = await params;
  permanentRedirect(`/agents/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`);
}
