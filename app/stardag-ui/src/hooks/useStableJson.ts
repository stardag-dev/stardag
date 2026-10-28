import { useState } from "react";

/**
 * `value`, kept by identity for as long as its JSON content is unchanged.
 * A refresh that re-fetches the same body produces a new object; without
 * this it would re-index the tree and re-run the search, sending the user
 * back to the first match mid-navigation. Serialises only when identity
 * changes.
 */
export function useStableJson<T>(value: T): T {
  const [seen, setSeen] = useState(value);
  const [stable, setStable] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (JSON.stringify(value) !== JSON.stringify(stable)) setStable(value);
  }
  // React re-renders with the updated state before committing.
  return stable;
}
