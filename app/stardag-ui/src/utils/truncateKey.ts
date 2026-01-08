/**
 * Smart truncation for nested keys (e.g., param.nested.level2.lr)
 *
 * Algorithm:
 * 1. If the key fits, return as-is
 * 2. Split by dots, keep first segment + last segment(s)
 * 3. Add ellipsis in the middle
 * 4. Progressively shorten until it fits
 *
 * Examples:
 * - "param.nested.level2.lr" -> "param...level2.lr" -> "param...l2.lr" -> "...lr"
 */

/**
 * Truncate a nested key to fit within a max character count
 * Prioritizes showing the last part of the key
 */
export function truncateNestedKey(key: string, maxChars: number): string {
  if (key.length <= maxChars) {
    return key;
  }

  const parts = key.split(".");

  // If only one part, just truncate normally
  if (parts.length === 1) {
    if (maxChars <= 3) {
      return "...";
    }
    return "..." + key.slice(-(maxChars - 3));
  }

  // Try to keep first and last parts with ellipsis in middle
  const firstPart = parts[0];
  const lastPart = parts[parts.length - 1];
  const ellipsis = "...";

  // Minimum: "...lastPart"
  if (maxChars <= ellipsis.length + lastPart.length) {
    // Can't even fit ellipsis + last part, truncate the last part too
    if (maxChars <= 3) {
      return "...";
    }
    return "..." + lastPart.slice(-(maxChars - 3));
  }

  // Try: "firstPart...lastPart"
  const fullTruncated = `${firstPart}${ellipsis}${lastPart}`;
  if (fullTruncated.length <= maxChars) {
    return fullTruncated;
  }

  // Try: shortened first + ellipsis + last
  const availableForFirst = maxChars - ellipsis.length - lastPart.length;
  if (availableForFirst >= 1) {
    return `${firstPart.slice(0, availableForFirst)}${ellipsis}${lastPart}`;
  }

  // Fall back to just ellipsis + last part (truncated if needed)
  const remainingForLast = maxChars - ellipsis.length;
  if (remainingForLast > 0) {
    return ellipsis + lastPart.slice(-remainingForLast);
  }

  return "...";
}

/**
 * Measure text width using canvas
 * Returns width in pixels
 *
 * @param text - The text to measure
 * @param font - CSS font string (e.g., "500 12px Inter")
 * @param letterSpacing - Letter spacing in pixels (0 by default)
 * @param uppercase - Whether to measure as uppercase (for CSS text-transform)
 */
function measureTextWidth(
  text: string,
  font: string,
  letterSpacing: number = 0,
  uppercase: boolean = false,
): number {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (!context) return text.length * 10; // Fallback estimate
  context.font = font;
  const measureText = uppercase ? text.toUpperCase() : text;
  const baseWidth = context.measureText(measureText).width;
  // Add letter-spacing: (n-1) gaps for n characters
  const spacingWidth =
    measureText.length > 1 ? letterSpacing * (measureText.length - 1) : 0;
  return baseWidth + spacingWidth;
}

/**
 * Options for pixel-based truncation
 */
export interface TruncateOptions {
  font?: string;
  letterSpacing?: number;
  uppercase?: boolean;
}

/**
 * Truncate a nested key to fit within a max pixel width
 * Uses binary search to find optimal truncation
 */
export function truncateNestedKeyToWidth(
  key: string,
  maxWidth: number,
  options: TruncateOptions = {},
): string {
  const {
    font = "14px Inter, system-ui, sans-serif",
    letterSpacing = 0,
    uppercase = false,
  } = options;

  const fullWidth = measureTextWidth(key, font, letterSpacing, uppercase);
  if (fullWidth <= maxWidth) {
    return key;
  }

  // Binary search for the right character count
  let low = 3;
  let high = key.length;
  let result = "...";

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const truncated = truncateNestedKey(key, mid);
    const width = measureTextWidth(truncated, font, letterSpacing, uppercase);

    if (width <= maxWidth) {
      result = truncated;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }

  return result;
}

/**
 * Format a column key into a human-readable label
 */
export function keyToLabel(key: string): string {
  // For param.* keys, keep the full key as label
  if (key.startsWith("param.")) {
    return key;
  }

  // For asset.* keys, keep the full key as label
  if (key.startsWith("asset.")) {
    return key;
  }

  // For core keys, format nicely (task_name -> Task Name)
  return key
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}
