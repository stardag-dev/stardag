import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

export interface BreadcrumbItem {
  /** What is drawn. Shortened where the full value is long — see `title`. */
  label: string;
  /**
   * The full value, when `label` is an abbreviation of it. Rendered as
   * the crumb's tooltip, so shortening a long id in the trail does not
   * put it out of reach.
   */
  title?: string;
  /** Status and other at-a-glance marks drawn after the label. */
  detail?: ReactNode;
  onClick?: () => void;
}

interface BreadcrumbContextValue {
  items: BreadcrumbItem[];
  setItems: (items: BreadcrumbItem[]) => void;
}

const BreadcrumbContext = createContext<BreadcrumbContextValue>({
  items: [],
  setItems: () => {},
});

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [items, setItemsState] = useState<BreadcrumbItem[]>([]);

  const setItems = useCallback((newItems: BreadcrumbItem[]) => {
    setItemsState(newItems);
  }, []);

  return (
    <BreadcrumbContext.Provider value={{ items, setItems }}>
      {children}
    </BreadcrumbContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBreadcrumb() {
  return useContext(BreadcrumbContext);
}
