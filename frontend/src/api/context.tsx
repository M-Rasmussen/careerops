import { createContext, useContext } from "react";
import type { DataAdapter } from "../types";

export const DataContext = createContext<DataAdapter | null>(null);

export function useData(): DataAdapter {
  const adapter = useContext(DataContext);
  if (!adapter) throw new Error("useData must be used inside <DataContext.Provider>");
  return adapter;
}
