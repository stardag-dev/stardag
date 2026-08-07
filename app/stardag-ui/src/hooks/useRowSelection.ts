import { useCallback, useEffect, useMemo, useState } from "react";

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

/**
 * Row selection for a paginated table.
 *
 * **Selection is scoped to what is on screen.** Passing a ``resetKey``
 * that changes whenever the visible set changes (page, filters,
 * environment) clears the selection, and ``selectedIds`` is always
 * intersected with ``visibleIds``. That is deliberate: silently carrying
 * off-screen rows into a destructive bulk action is exactly the kind of
 * hidden state that makes such an action untrustworthy. Clearing is
 * honest and visible.
 */
export function useRowSelection(
  visibleIds: string[],
  resetKey?: string,
): RowSelection {
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    setSelected(new Set());
  }, [resetKey]);

  const selectedIds = useMemo(
    () => visibleIds.filter((id) => selected.has(id)),
    [visibleIds, selected],
  );

  const isSelected = useCallback((id: string) => selected.has(id), [selected]);

  const toggle = useCallback((id: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  }, []);

  const toggleAllVisible = useCallback(
    (checked: boolean) => {
      setSelected(() => (checked ? new Set(visibleIds) : new Set()));
    },
    [visibleIds],
  );

  const clear = useCallback(() => setSelected(new Set()), []);

  return {
    selectedIds,
    selectedCount: selectedIds.length,
    isSelected,
    toggle,
    toggleAllVisible,
    clear,
    allVisibleSelected: visibleIds.length > 0 && selectedIds.length === visibleIds.length,
    someVisibleSelected:
      selectedIds.length > 0 && selectedIds.length < visibleIds.length,
  };
}
