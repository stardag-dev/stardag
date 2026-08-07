import { useEffect, useRef } from "react";

interface CheckboxProps {
  checked: boolean;
  /**
   * Renders the mixed ("some but not all") state. Only meaningful while
   * ``checked`` is false — a checked box is never indeterminate, which is
   * also how the DOM property behaves.
   */
  indeterminate?: boolean;
  onChange: (checked: boolean) => void;
  /**
   * Accessible name. Required: a bare checkbox in a table row is
   * unusable without one, and the tests query by it.
   */
  label: string;
  /**
   * When false the label renders as visible text next to the box. The
   * default (true) keeps the label available to assistive technology
   * only — the right choice for per-row selection boxes.
   */
  labelHidden?: boolean;
  disabled?: boolean;
  className?: string;
}

/**
 * The one checkbox in this codebase.
 *
 * Introduced for row selection in the builds table; kept generic (and
 * indeterminate-capable) so any later multi-select surface reuses it
 * rather than hand-rolling a fourth set of Tailwind classes.
 */
export function Checkbox({
  checked,
  indeterminate = false,
  onChange,
  label,
  labelHidden = true,
  disabled = false,
  className = "",
}: CheckboxProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  // `indeterminate` is a DOM property with no HTML attribute, so it can
  // only be set imperatively.
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.indeterminate = !checked && indeterminate;
    }
  }, [checked, indeterminate]);

  const input = (
    <input
      ref={inputRef}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={(e) => onChange(e.target.checked)}
      aria-label={labelHidden ? label : undefined}
      className="h-4 w-4 cursor-pointer rounded border-gray-300 accent-blue-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:accent-blue-500"
    />
  );

  if (labelHidden) {
    return <span className={`inline-flex items-center ${className}`}>{input}</span>;
  }

  return (
    <label
      className={`inline-flex cursor-pointer items-center gap-2 text-sm text-gray-700 select-none dark:text-gray-300 ${
        disabled ? "cursor-not-allowed opacity-50" : ""
      } ${className}`}
    >
      {input}
      <span>{label}</span>
    </label>
  );
}
