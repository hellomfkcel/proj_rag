/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // 开发环境允许局域网 IP 访问（避免 "Cross origin request" 警告）
  allowedDevOrigins: ["192.168.1.127", "localhost", "127.0.0.1"],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
      {
        source: "/healthz",
        destination: "http://localhost:8000/healthz",
      },
    ];
  },
};

module.exports = nextConfig;
