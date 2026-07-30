"use client";
// Error Boundary — catches React render crashes per-page, shows recovery UI

import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error("[ErrorBoundary] Uncaught render error:", error.message, errorInfo.componentStack);
  }

  handleReload = () => {
    this.setState({ hasError: false, error: null });
    window.location.reload();
  };

  handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div className="min-h-screen flex items-center justify-center bg-gray-50 px-6">
          <div className="text-center max-w-md">
            <div className="text-5xl mb-4">💥</div>
            <h1 className="text-xl font-bold text-gray-900 mb-2">页面出错了</h1>
            <p className="text-sm text-gray-500 mb-1">
              页面渲染过程中发生了未预期的错误。
            </p>
            {this.state.error && (
              <p className="text-xs text-gray-400 font-mono bg-gray-100 rounded-lg px-3 py-1.5 mt-2 mb-4 line-clamp-2">
                {this.state.error.message}
              </p>
            )}
            <div className="flex items-center justify-center gap-3 mt-4">
              <button
                onClick={this.handleReset}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition font-medium"
              >
                尝试恢复
              </button>
              <button
                onClick={this.handleReload}
                className="px-4 py-2 text-sm border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition"
              >
                🔄 刷新页面
              </button>
            </div>
            <p className="text-xs text-gray-400 mt-4">
              如果问题持续存在，请清除浏览器缓存或联系管理员。
            </p>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
