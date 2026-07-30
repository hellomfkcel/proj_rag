"use client";
// 403 Not Authorized — shown when user lacks permission for a resource

import { useEffect, useState } from "react";
import Link from "next/link";
import Header from "@/app/components/Header";
import { getAppConfig } from "@/lib/settings";

export default function NotAuthorizedPage() {
  const [adminUrl, setAdminUrl] = useState("");

  useEffect(() => {
    getAppConfig().then((cfg) => setAdminUrl(cfg.admin_console_url || "")).catch(() => {});
  }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header />
      <main className="max-w-2xl mx-auto px-6 py-20 text-center">
        <div className="text-6xl mb-6">🚫</div>
        <h1 className="text-2xl font-bold text-gray-900 mb-3">权限不足</h1>
        <p className="text-gray-500 mb-2">你没有访问此资源所需的权限。</p>
        <p className="text-sm text-gray-400 mb-8">
          此操作需要特定的角色或授权。如需申请权限，请
          {adminUrl ? (
            <>前往 <a href={adminUrl} target="_blank" rel="noreferrer" className="text-purple-600 underline hover:text-purple-700">管理台</a> 提交申请</>
          ) : (
            "联系管理员"
          )}。
        </p>
        <div className="flex items-center justify-center gap-3">
          <Link
            href="/kb"
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition text-sm font-medium"
          >
            返回知识库
          </Link>
          {adminUrl ? (
            <a
              href={adminUrl}
              target="_blank"
              rel="noreferrer"
              className="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition text-sm font-medium"
            >
              🔗 前往管理台
            </a>
          ) : (
            <span
              className="px-4 py-2 border border-gray-200 text-gray-400 rounded-lg text-sm cursor-not-allowed"
              title="未配置 ADMIN_CONSOLE_URL 环境变量"
            >
              管理台未配置
            </span>
          )}
        </div>
      </main>
    </div>
  );
}
