import { useCallback, useMemo, useState } from "react";

export interface RowSelection {
  /** Selected ids, in the order of ``visibleIds``. */
  selectedIds: string[];
  selectedCount: number;
  isSelected: (id: string) => boolean;
  toggle: (id: string, checked: boolean) => void;
  /** Select/deselect every currently visible row (the header checkbox). */
  toggleAllVisible: (checked: boolean) => void;
  clear: () => void;
  /** Every visible row is selected (false when there are no rows). */
  allVisibleSelected: boolean;
  /** At least one — but not every — visible row is selected. */
  someVisibleSelected: boolean;
}

const EMPTY: ReadonlySet<string> = new Set();

/**
 * Row selection for a paginated table.
 *
 * **Selection is scoped to what is on screen.** Pass a ``resetKey`` that
 * changes whenever the visible set changes (page, filters, environment)
 * and the selection empties; ``selectedIds`` is additionally intersected
 * with ``visibleIds``. That is deliberate: silently carrying off-screen
 * rows into a destructive bulk action is exactly the kind of hidden state
 * that makes such an action untrustworthy. Clearing is honest.
 *
 * The reset is derived rather than performed in an effect — the selection
 * is stored together with the key it was made under, and a selection made
 * under a different key simply reads as empty. No cascading render, and no
 * window in which a stale selection is observable.
 */
export function useRowSelection(visibleIds: string[], resetKey?: string): RowSelection {
  const [stored, setStored] = useState<{
    key: string | undefined;
    ids: ReadonlySet<string>;
  }>(() => ({ key: resetKey, ids: EMPTY }));

  const selected = stored.key === resetKey ? stored.ids : EMPTY;

  const selectedIds = useMemo(
    () => visibleIds.filter((id) => selected.has(id)),
    [visibleIds, selected],
  );

  const isSelected = useCallback((id: string) => selected.has(id), [selected]);

  const toggle = useCallback(
    (id: string, checked: boolean) => {
      setStored((prev) => {
        const base = prev.key === resetKey ? prev.ids : EMPTY;
        const next = new Set(base);
        if (checked) {
          next.add(id);
        } else {
          next.delete(id);
        }
        return { key: resetKey, ids: next };
      });
    },
    [resetKey],
  );

  const toggleAllVisible = useCallback(
    (checked: boolean) => {
      setStored({ key: resetKey, ids: checked ? new Set(visibleIds) : EMPTY });
    },
    [resetKey, visibleIds],
  );

  const clear = useCallback(() => setStored({ key: resetKey, ids: EMPTY }), [resetKey]);

  return {
    selectedIds,
    selectedCount: selectedIds.length,
    isSelected,
    toggle,
    toggleAllVisible,
    clear,
    allVisibleSelected:
      visibleIds.length > 0 && selectedIds.length === visibleIds.length,
    someVisibleSelected:
      selectedIds.length > 0 && selectedIds.length < visibleIds.length,
  };
}
