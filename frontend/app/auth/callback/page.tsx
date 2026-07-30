"use client";
// /auth/callback — SSO callback handler
// IdP (Keycloak) redirects here with ?code=xxx after successful login.
// Exchanges the authorization code for tokens via POST /api/v1/auth/token.

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
import { exchangeCode } from "@/lib/auth";

export default function AuthCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const login = useAuthStore((s) => s.login);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = searchParams.get("code");
    if (!code) {
      setError("缺少授权码 — 请重新登录");
      return;
    }

    exchangeCode(code)
      .then((result) => {
        login(result.access_token, result.user, new Date(result.expires_at).getTime());
        router.push("/kb");
      })
      .catch((e) => {
        setError(e?.message || "登录失败，请重试");
      });
  }, [searchParams, login, router]);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <div className="text-4xl mb-4">❌</div>
          <h1 className="text-xl font-semibold text-gray-800 mb-2">登录失败</h1>
          <p className="text-gray-500 mb-6">{error}</p>
          <a
            href="/login"
            className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
          >
            重新登录
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="text-center">
        <div className="animate-spin w-10 h-10 border-4 border-blue-500 border-t-transparent rounded-full mx-auto mb-4" />
        <p className="text-gray-500">正在完成登录...</p>
      </div>
    </div>
  );
}
