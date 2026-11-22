// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

// A production build tells the boundary only that rendering failed, never why: a page whose
// registry read failed says so itself (registry-unavailable.tsx).
export default function ErrorPage({ reset }: { reset: () => void }) {
  const router = useRouter();
  return (
    <div className="wrap page-body">
      <div className="empty-state" role="alert">
        <p className="section-kicker">Something went wrong</p>
        <h1>This page could not be loaded</h1>
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
