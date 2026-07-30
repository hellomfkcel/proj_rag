// Root Layout — wraps all pages with AuthGuard + QueryProvider + global Toast

import type { Metadata } from "next";
import "./globals.css";
import ClientShell from "./ClientShell";

export const metadata: Metadata = {
  title: "RAG v14 — 企业知识库",
  description: "企业级多知识库 RAG 问答平台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-gray-50 text-gray-900 font-sans">
        <ClientShell>{children}</ClientShell>
      </body>
    </html>
  );
}
