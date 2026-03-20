export type LayoutDirection = "TB" | "LR";

export type PositionCache = Record<
  LayoutDirection,
  Map<string, { x: number; y: number }>
>;

export function createPositionCache(): PositionCache {
  return { TB: new Map(), LR: new Map() };
}

export const MAX_LABEL_CHARS = 20;

export function truncateLabel(label: string): string {
  return label.length > MAX_LABEL_CHARS ? `${label.slice(0, MAX_LABEL_CHARS)}…` : label;
}
