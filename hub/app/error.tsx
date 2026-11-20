// SPDX-License-Identifier: Apache-2.0
"use client";

import { useRouter } from "next/navigation";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  const router = useRouter();
  return (
    <div className="empty" role="alert">
      <h1>The Hub can&apos;t reach the registry</h1>
      <p>Try again in a moment.</p>
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
  );
}
