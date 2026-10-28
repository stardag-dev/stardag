import {
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import {
  ancestorIds,
  revealInPages,
  union,
  type TreeNode,
  type Windows,
} from "../utils/jsonTree";

/** How long typing must pause before the tree is searched. */
export const SEARCH_DEBOUNCE_MS = 150;

/**
 * How many matches a search reveals at once. A one-letter query over a
 * large value matches most of it, and expanding to all of those would
 * draw the whole tree; the rest are revealed as next/previous reaches
 * them.
 */
export const REVEAL_MATCHES = 100;

export interface TreeSearchState<M> {
  query: string;
  matches: M[];
  current: number;
}

interface Options<N extends TreeNode, M extends { index: number }> {
  nodes: readonly N[];
  /** Matches over `nodes`, best first. Must be stable (a module function). */
  find: (nodes: readonly N[], query: string) => M[];
  setExpanded: Dispatch<SetStateAction<ReadonlySet<string>>>;
  setWindows: Dispatch<SetStateAction<Windows>>;
  setFocused: Dispatch<SetStateAction<number>>;
  treeRef: RefObject<HTMLElement | null>;
}

/**
 * The search over a flat tree view (the JSON tree, the diff view): the
 * box's text, a debounced search over the nodes that reveals the first
 * `REVEAL_MATCHES` matches and goes to the best one, next/previous that
 * re-reveal a match folded away since, and the scroll into view.
 */
export function useTreeSearch<N extends TreeNode, M extends { index: number }>({
  nodes,
  find,
  setExpanded,
  setWindows,
  setFocused,
  treeRef,
}: Options<N, M>) {
  const [input, setInput] = useState("");
  const [search, setSearch] = useState<TreeSearchState<M>>({
    query: "",
    matches: [],
    current: 0,
  });

  // A re-indexed node list ("only differences") invalidates the matches'
  // indices at once, not after the debounce: drop them now and keep the
  // text, so the search re-runs over the new list.
  const [searchedNodes, setSearchedNodes] = useState(nodes);
  if (searchedNodes !== nodes) {
    setSearchedNodes(nodes);
    if (search.matches.length > 0)
      setSearch((prev) => ({ ...prev, matches: [], current: 0 }));
  }

  const matchByIndex = useMemo(
    () => new Map(search.matches.map((m) => [m.index, m])),
    [search.matches],
  );
  const currentMatch: M | undefined = search.matches[search.current];

  // Search once typing pauses: reveal the matches and go to the first.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (!input.trim()) {
        setSearch((prev) =>
          prev.query === "" ? prev : { query: "", matches: [], current: 0 },
        );
        return;
      }
      const matches = find(nodes, input);
      setSearch({ query: input.trim(), matches, current: 0 });
      if (matches.length > 0) {
        setExpanded((prev) =>
          union(
            prev,
            ancestorIds(
              nodes,
              matches.slice(0, REVEAL_MATCHES).map((m) => m.index),
            ),
          ),
        );
        setWindows((prev) => revealInPages(nodes, prev, matches[0].index));
        setFocused(matches[0].index);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [input, nodes, find, setExpanded, setWindows, setFocused]);

  // Bring the current match into view on every search and every
  // next/previous, including a step that lands on the same match (a
  // single result the user has scrolled or folded away from).
  useEffect(() => {
    const match = search.matches[search.current];
    if (match === undefined) return;
    const el = treeRef.current?.querySelector<HTMLElement>(
      `[data-row="${match.index}"]`,
    );
    el?.scrollIntoView?.({ block: "nearest" });
  }, [search, treeRef]);

  const goToMatch = (step: 1 | -1) => {
    const count = search.matches.length;
    if (count === 0) return;
    const current = (search.current + step + count) % count;
    const index = search.matches[current].index;
    if (index >= nodes.length) return;
    // A new object even when `current` is unchanged, so the scroll runs.
    setSearch((prev) => ({ ...prev, current }));
    // The user may have folded it away since the search ran, and it may
    // lie past its parent's page.
    setExpanded((prev) => union(prev, ancestorIds(nodes, [index])));
    setWindows((prev) => revealInPages(nodes, prev, index));
    setFocused(index);
  };

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      goToMatch(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape" && input) {
      event.preventDefault();
      event.stopPropagation(); // not the enclosing modal's Escape
      setInput("");
    }
  };

  return {
    input,
    setInput,
    search,
    matchByIndex,
    currentMatch,
    goToMatch,
    onSearchKeyDown,
  };
}
