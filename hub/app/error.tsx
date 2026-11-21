// SPDX-License-Identifier: Apache-2.0
"use client";

import { useRouter } from "next/navigation";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  const router = useRouter();
  return (
    <div className="wrap page-body">
      <div className="empty-state" role="alert">
        <p className="section-kicker">Registry unavailable</p>
        <h1>The Hub can&apos;t reach the registry</h1>
        <p className="muted">Try again in a moment.</p>
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
      </div>
    </div>
  );
}
