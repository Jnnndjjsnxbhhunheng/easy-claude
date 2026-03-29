import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Easy Claude — MCP + Skill Agent",
  description: "AI Agent with MCP tools, Skills, and team protocols. Real-time step visualization.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh">
      <body className="h-screen overflow-hidden">{children}</body>
    </html>
  );
}
