// SPDX-License-Identifier: Apache-2.0
"use client";

export default function ErrorPage({ retry }: { retry: () => void }) {
  return (
    <div className="empty" role="alert">
      <h1>The Hub can&apos;t reach the registry</h1>
      <p>Try again in a moment.</p>
      <button type="button" onClick={retry}>Try again</button>
    </div>
  );
}
