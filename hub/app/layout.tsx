import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "OpenHealth Hub", template: "%s · OpenHealth Hub" },
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
              OpenHealth Hub
            </Link>
            <span className="tag">synthetic data only</span>
          </div>
        </header>
        <main id="content">{children}</main>
      </body>
    </html>
  );
}
