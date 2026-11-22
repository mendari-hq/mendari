// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";

// A page whose registry read failed (lib/registry.ts RegistryUnavailable) says so itself: in a
// production build the error boundary (error.tsx) is only told that something failed, not what.
export function RegistryUnavailablePage({ retry }: { retry: string }) {
  return (
    <div className="wrap page-body">
      <div className="empty-state" role="alert">
        <p className="section-kicker">Registry unavailable</p>
        <h1>The Hub can&apos;t reach the registry</h1>
        <p className="muted">
          Nothing on this page could be checked right now. <Link href={retry}>Try again</Link> in a moment, or{" "}
          <Link href="/">return to the catalog</Link>.
        </p>
      </div>
    </div>
  );
}
