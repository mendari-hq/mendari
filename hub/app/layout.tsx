// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Mendari Hub", template: "%s · Mendari Hub" },
  description: "Decision Packs for healthcare: packaged, evaluated, signed and shared.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#content">Skip to content</a>
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand">
              <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
                <rect width="32" height="32" rx="7" />
                <path d="M16 6.5 25.5 16 16 25.5 6.5 16Z" />
              </svg>
              Mendari Hub
            </Link>
            <span className="tag">synthetic data only</span>
          </div>
        </header>
        {/* The notice (src/mendari/notices.py NOTICE; tests/test_notice.py holds them equal). */}
        <p className="preview-notice" role="note">Research preview. Not a medical device. Not for emergencies: patients should call 911.</p>
        <main id="content">{children}</main>
      </body>
    </html>
  );
}
