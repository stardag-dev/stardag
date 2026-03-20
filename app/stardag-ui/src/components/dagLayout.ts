export type LayoutDirection = "TB" | "LR";

export type PositionCache = Record<
  LayoutDirection,
  Map<string, { x: number; y: number }>
>;

export function createPositionCache(): PositionCache {
  return { TB: new Map(), LR: new Map() };
}
