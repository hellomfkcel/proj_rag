"use client";
// Global Toast — event-driven, no context provider needed

import { useEffect, useState, useCallback } from "react";

export interface ToastEvent {
  id: number;
  message: string;
  type: "error" | "warning" | "info" | "success";
  action?: { label: string; onClick: () => void };
  duration?: number; // ms, default 5000
}

let _nextId = 0;
const _listeners = new Set<(e: ToastEvent) => void>();

export function showToast(
  message: string,
  type: ToastEvent["type"] = "error",
  action?: ToastEvent["action"],
  duration?: number
) {
  const ev: ToastEvent = { id: ++_nextId, message, type, action, duration };
  _listeners.forEach((fn) => fn(ev));
}

// Convenience wrappers
export function toastError(msg: string, action?: ToastEvent["action"]) { showToast(msg, "error", action); }
export function toastWarning(msg: string, action?: ToastEvent["action"]) { showToast(msg, "warning", action); }
export function toastInfo(msg: string) { showToast(msg, "info"); }
export function toastSuccess(msg: string) { showToast(msg, "success"); }

export default function ToastContainer() {
  const [toasts, setToasts] = useState<ToastEvent[]>([]);

  const addToast = useCallback((ev: ToastEvent) => {
    setToasts((prev) => [...prev, ev]);
    const dur = ev.duration ?? (ev.action ? 8000 : 5000);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== ev.id));
    }, dur);
  }, []);

  useEffect(() => {
    _listeners.add(addToast);
    return () => { _listeners.delete(addToast); };
  }, [addToast]);

  const dismiss = (id: number) => setToasts((prev) => prev.filter((t) => t.id !== id));

  if (toasts.length === 0) return null;

  const color = (t: ToastEvent) =>
    t.type === "error" ? "bg-red-600" :
    t.type === "warning" ? "bg-yellow-500 text-gray-900" :
    t.type === "success" ? "bg-green-600" :
    "bg-blue-600";

  return (
    <div className="fixed bottom-6 right-6 z-[9999] flex flex-col gap-2 max-w-sm">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`${color(t)} text-white px-4 py-3 rounded-lg shadow-lg flex items-start gap-3 animate-slide-up text-sm`}
        >
          <span className="flex-1">{t.message}</span>
          <div className="flex items-center gap-2 shrink-0">
            {t.action && (
              <button
                onClick={() => { t.action!.onClick(); dismiss(t.id); }}
                className="text-xs underline font-medium hover:opacity-80"
              >
                {t.action.label}
              </button>
            )}
            <button onClick={() => dismiss(t.id)} className="opacity-60 hover:opacity-100 text-lg leading-none">&times;</button>
          </div>
        </div>
      ))}
    </div>
  );
}
