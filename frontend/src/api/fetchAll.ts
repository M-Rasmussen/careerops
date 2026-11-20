// No count endpoint exists, so totals come from paging through every row at the API maximum.
// shortcut: O(rows) per dashboard load, fine for a personal tracker; add a /stats endpoint if it gets slow.
const PAGE = 200;

export async function fetchAll<T>(page: (limit: number, offset: number) => Promise<T[]>): Promise<T[]> {
  const rows: T[] = [];
  for (;;) {
    const batch = await page(PAGE, rows.length);
    rows.push(...batch);
    if (batch.length < PAGE) return rows;
  }
}
