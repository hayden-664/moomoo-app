import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "moomoo",
  description: "Read-only moomoo portfolio dashboard and options screener",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <nav className="border-b border-border">
          <div className="mx-auto flex max-w-6xl items-center gap-5 px-5 py-3 text-sm">
            <span className="font-semibold tracking-tight">moomoo</span>
            <Link href="/" className="text-muted hover:text-foreground">
              Portfolio
            </Link>
            <Link href="/options" className="text-muted hover:text-foreground">
              Options
            </Link>
            <span className="ml-auto rounded border border-border px-2 py-0.5 text-xs text-muted">
              read-only
            </span>
          </div>
        </nav>
        <main className="mx-auto w-full max-w-6xl flex-1 px-5 py-6">{children}</main>
      </body>
    </html>
  );
}
