import type { Metadata } from "next";
import { Hanken_Grotesk } from "next/font/google";
import "./globals.css";
import { SiteHeader } from "@/components/site/SiteHeader";

// Open-font stand-in for writeit.ai's domain-locked proxima-nova. Self-hosted
// by next/font so the site stays a self-contained module.
const hanken = Hanken_Grotesk({
  subsets: ["latin"],
  variable: "--font-hanken",
});

const siteUrl = "https://team-harness.writeit.ai";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "team-harness — Documentation",
    template: "%s — team-harness",
  },
  description:
    "Coordinate a team of AI coding agent CLIs — Codex, Gemini, Claude Code, Antigravity, and more — from one LLM coordinator. Documentation for team-harness.",
  openGraph: {
    title: "team-harness — Documentation",
    description:
      "Coordinate a team of AI coding agent CLIs from one LLM coordinator.",
    url: siteUrl,
    siteName: "team-harness",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className={`${hanken.variable} font-sans antialiased`}>
        <SiteHeader />
        <main>{children}</main>
      </body>
    </html>
  );
}
