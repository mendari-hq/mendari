// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";

export default function NotFound() {
  return (
    <div className="wrap page-body">
      <div className="empty-state">
        <p className="section-kicker">404 · Not found</p>
        <h1>Not found</h1>
        <p className="muted">No pack or publisher by that name. <Link href="/">Search the packs</Link>.</p>
      </div>
    </div>
  );
}
