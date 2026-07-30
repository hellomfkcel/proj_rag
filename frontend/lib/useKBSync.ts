"use client";
// URL ↔ KB Store sync hook

import { useEffect, useRef } from "react";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import { useKBStore } from "@/stores/useKBStore";

/**
 * Sync selected KBs between Zustand store and URL query params `?kb_ids=xxx,yyy`.
 *
 * - On first mount with a populated URL: restores selection from URL.
 * - When store changes: updates URL (replace, no history push).
 * - When URL is empty and store is empty: no-op.
 */
export function useKBSync() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const { selectedKBs, setSelectedKBs } = useKBStore();
  const initialized = useRef(false);

  // Phase 1: On first mount, restore from URL if present
  useEffect(() => {
    if (initialized.current) return;

    const fromUrl = searchParams.get("kb_ids");
    if (fromUrl) {
      const ids = fromUrl.split(",").filter(Boolean);
      if (ids.length > 0) {
        setSelectedKBs(ids);
      }
    }
    initialized.current = true;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Phase 2: When store changes, update URL
  useEffect(() => {
    if (!initialized.current) return;

    const current = searchParams.get("kb_ids") || "";
    const wanted = selectedKBs.join(",");

    if (current === wanted) return;

    const params = new URLSearchParams(searchParams.toString());
    if (wanted) {
      params.set("kb_ids", wanted);
    } else {
      params.delete("kb_ids");
    }
    const qs = params.toString();
    const url = pathname + (qs ? `?${qs}` : "");
    router.replace(url, { scroll: false });
  }, [selectedKBs, pathname, router, searchParams]);
}
