// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Mendari Hub", template: "%s · Mendari Hub" },
  description: "Decision Packs for healthcare: packaged, evaluated, signed and shared.",
};

function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className="brand-mark" aria-hidden="true">
      <rect width="32" height="32" rx="7" />
      <path d="M16 7.25 20 11.25 16 15.25 12 11.25Z" />
      <path className="dim" d="M20.75 12 24.75 16 20.75 20 16.75 16Z" />
      <path className="dim" d="M16 16.75 20 20.75 16 24.75 12 20.75Z" />
      <path className="dim" d="M11.25 12 15.25 16 11.25 20 7.25 16Z" />
    </svg>
  );
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#content">Skip to content</a>
        {/* The notice (src/mendari/notices.py NOTICE; tests/test_notice.py holds them equal). */}
        <p className="preview-notice" role="note">Research preview. Not a medical device. Not for emergencies: patients should call 911.</p>
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand">
              <BrandMark size={28} />
              <span className="brand-wordmark">Mendari</span>
              <span className="brand-sub">Hub</span>
            </Link>
            <nav className="topbar-nav" aria-label="Main">
              <Link href="/">Browse packs</Link>
              <Link href="/publishers/mendari">Reference packs</Link>
              <span className="tag">synthetic data only</span>
            </nav>
          </div>
        </header>
        <main id="content" className="site-main">{children}</main>
        <footer className="site-footer">
          <div className="wrap foot-inner">
            <div className="foot-brand">
              <p className="brand brand-sm">
                <BrandMark size={22} />
                <span className="brand-wordmark">Mendari</span>
                <span className="brand-sub">Hub</span>
              </p>
              <p className="muted">
                Signed Decision Pack registry. Every card is rendered from the pack&apos;s verified archive; validate on your own cases before use.
              </p>
            </div>
            <div className="foot-meta">
              <nav className="foot-links" aria-label="Registry">
                <Link href="/">Browse packs</Link>
                <Link href="/publishers/mendari">Reference packs</Link>
              </nav>
              <p className="muted">Apache-2.0 · Research preview</p>
            </div>
          </div>
        </footer>
      </body>
    </html>
  );
}
