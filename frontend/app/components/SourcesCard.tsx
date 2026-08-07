"use client";
// Source Card — hover-to-show popover, auto-dismiss on mouse leave

import { useState, useRef, useEffect, useCallback } from "react";

interface Source {
  chunk_id: string;
  content: string;
}

export default function SourcesCard({ index, source }: { index: number; source: Source }) {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
    };
  }, []);

  const startHide = useCallback(() => {
    // Delay hiding to allow mouse to move between button and popover
    hideTimerRef.current = setTimeout(() => setShow(false), 200);
  }, []);

  const cancelHide = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
  }, []);

  const handleMouseEnter = useCallback(() => {
    cancelHide();
    setShow(true);
  }, [cancelHide]);

  const handleMouseLeave = useCallback(() => {
    startHide();
  }, [startHide]);

  // Close on outside click (for touch / click-to-toggle accessibility)
  useEffect(() => {
    if (!show) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setShow(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [show]);

  const hasContent = source.content && source.content.length > 0;

  return (
    <div ref={ref} className="relative inline-block"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <button
        onClick={() => setShow(!show)}
        className={`px-2.5 py-1 text-xs border rounded-full transition font-medium ${
          show
            ? "border-blue-400 bg-blue-50 text-blue-700"
            : "border-gray-300 bg-white text-gray-600 hover:border-blue-400 hover:bg-blue-50"
        }`}
      >
        [来源 {index + 1}]
      </button>

      {show && (
        <div className="absolute bottom-full left-0 mb-2 w-80 bg-white border border-gray-200 rounded-lg shadow-xl z-50">
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100 bg-gray-50 rounded-t-lg">
            <span className="text-xs font-mono text-gray-400 truncate max-w-[200px]">
              {source.chunk_id?.slice(0, 16)}...
            </span>
            <button
              onClick={() => setShow(false)}
              className="text-gray-400 hover:text-gray-600 text-sm leading-none"
            >
              ✕
            </button>
          </div>
          {/* Body */}
          <div className="px-3 py-2">
            {hasContent ? (
              <p className="text-xs text-gray-700 leading-relaxed line-clamp-6 whitespace-pre-wrap">
                {source.content}
              </p>
            ) : (
              <p className="text-xs text-gray-400 italic">原文未加载</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
