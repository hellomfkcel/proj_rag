// KB Store — multi-select, URL-synced, localStorage-persisted
"use client";
import { create } from "zustand";

const STORAGE_KEY = "rag_selected_kbs";

function loadFromStorage(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveToStorage(ids: string[]) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {}
}

interface KBState {
  selectedKBs: string[];
  setSelectedKBs: (ids: string[]) => void;
  toggleKB: (id: string) => void;
  clearKBs: () => void;
}

export const useKBStore = create<KBState>((set) => ({
  selectedKBs: loadFromStorage(),

  setSelectedKBs: (ids) => {
    const unique = [...new Set(ids.filter(Boolean))];
    saveToStorage(unique);
    set({ selectedKBs: unique });
  },

  toggleKB: (id) => {
    set((state) => {
      const has = state.selectedKBs.includes(id);
      const next = has
        ? state.selectedKBs.filter((k) => k !== id)
        : [...state.selectedKBs, id];
      const unique = [...new Set(next)];
      saveToStorage(unique);
      return { selectedKBs: unique };
    });
  },

  clearKBs: () => {
    saveToStorage([]);
    set({ selectedKBs: [] });
  },
}));
