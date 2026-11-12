import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = { title: "adatta", description: "Probabilistic football projections" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link href="/" className="brand">adatta</Link>
          <span className="topbar-note">local · read-only · champion projections</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
