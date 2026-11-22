// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

export default function ErrorPage({ error, reset }: { error: Error; reset: () => void }) {
  const router = useRouter();
  const isRegistry = /registry/i.test(error?.message ?? "");
  return (
    <div className="wrap page-body">
      <div className="empty-state" role="alert">
        <p className="section-kicker">{isRegistry ? "Registry unavailable" : "Something went wrong"}</p>
        <h1>{isRegistry ? "The Hub can't reach the registry" : "This page could not be loaded"}</h1>
        <p className="muted">Try again in a moment, or return to the catalog.</p>
        <div className="error-actions">
          <button
            type="button"
            className="retry-button"
            onClick={() => {
              router.refresh();
              reset();
            }}
          >
            Try again
          </button>
          <Link href="/" className="clear-filters">Back to Agent Hub</Link>
        </div>
      </div>
    </div>
  );
}
