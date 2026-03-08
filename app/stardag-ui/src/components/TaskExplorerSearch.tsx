import React from "react";

// Filter types
export interface FilterCondition {
  id: string;
  key: string;
  operator: "=" | "!=" | ">" | "<" | ">=" | "<=" | "~";
  value: string;
}

// Available operators for filter autocomplete
// eslint-disable-next-line react-refresh/only-export-components
export const FILTER_OPERATORS: {
  op: FilterCondition["operator"];
  label: string;
}[] = [
  { op: "=", label: "equals" },
  { op: "!=", label: "not equals" },
  { op: ">", label: "greater than" },
  { op: "<", label: "less than" },
  { op: ">=", label: "greater or equal" },
  { op: "<=", label: "less or equal" },
  { op: "~", label: "contains" },
];

interface TaskExplorerSearchProps {
  searchText: string;
  onSearchInput: (value: string) => void;
  onSearchSubmit: (e: React.FormEvent) => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onFocus: () => void;
  onBlur: () => void;
  filters: FilterCondition[];
  onEditFilter: (filter: FilterCondition) => void;
  onRemoveFilter: (id: string) => void;
  onClearAllFilters: () => void;
  onShowColumnManager: () => void;
  // Autocomplete
  showAutocomplete: boolean;
  autocompleteOptions: string[];
  autocompleteMode: "key" | "operator" | "value";
  autocompleteKey: string;
  selectedIndex: number;
  onAutocompleteSelect: (option: string) => void;
  onSelectedIndexChange: (index: number) => void;
  inputRef: React.RefObject<HTMLInputElement | null>;
}

export function TaskExplorerSearch({
  searchText,
  onSearchInput,
  onSearchSubmit,
  onKeyDown,
  onFocus,
  onBlur,
  filters,
  onEditFilter,
  onRemoveFilter,
  onClearAllFilters,
  onShowColumnManager,
  showAutocomplete,
  autocompleteOptions,
  autocompleteMode,
  autocompleteKey,
  selectedIndex,
  onAutocompleteSelect,
  onSelectedIndexChange,
  inputRef,
}: TaskExplorerSearchProps) {
  return (
    <div className="border-b border-gray-200 bg-white px-4 py-1.5 dark:border-gray-700 dark:bg-gray-800">
      <form onSubmit={onSearchSubmit} className="relative">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <input
              ref={inputRef}
              type="text"
              value={searchText}
              onChange={(e) => onSearchInput(e.target.value)}
              onKeyDown={onKeyDown}
              onFocus={onFocus}
              onBlur={onBlur}
              placeholder="Search tasks... (e.g., task_name = MyTask, param.lr > 0.01)"
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 pl-9 text-xs text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
            />
            <svg
              className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
              />
            </svg>

            {/* Autocomplete dropdown */}
            {showAutocomplete && autocompleteOptions.length > 0 && (
              <div className="absolute left-0 right-0 top-full z-10 mt-1 rounded-md border border-gray-200 bg-white shadow-lg dark:border-gray-600 dark:bg-gray-700">
                {/* Header based on mode */}
                <div className="border-b border-gray-100 px-3 py-1.5 text-xs font-medium text-gray-500 dark:border-gray-600 dark:text-gray-400">
                  {autocompleteMode === "key" && "Keys"}
                  {autocompleteMode === "operator" && "Operators"}
                  {autocompleteMode === "value" && `Values for ${autocompleteKey}`}
                </div>
                {autocompleteOptions.map((option, index) => {
                  const isSelected = index === selectedIndex;
                  // Get operator description if in operator mode
                  const opInfo =
                    autocompleteMode === "operator"
                      ? FILTER_OPERATORS.find((o) => o.op === option)
                      : null;

                  return (
                    <button
                      key={option}
                      type="button"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        onAutocompleteSelect(option);
                      }}
                      onMouseEnter={() => onSelectedIndexChange(index)}
                      className={`block w-full px-3 py-1.5 text-left text-xs ${
                        isSelected
                          ? "bg-blue-50 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                          : "text-gray-700 dark:text-gray-300"
                      }`}
                    >
                      {autocompleteMode === "operator" ? (
                        <span className="flex items-center justify-between">
                          <span className="font-mono">{option}</span>
                          {opInfo && (
                            <span className="text-xs text-gray-400 dark:text-gray-500">
                              {opInfo.label}
                            </span>
                          )}
                        </span>
                      ) : (
                        option
                      )}
                    </button>
                  );
                })}
                {/* Keyboard hint */}
                <div className="border-t border-gray-100 px-3 py-1.5 text-xs text-gray-400 dark:border-gray-600 dark:text-gray-500">
                  <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">↑↓</kbd>{" "}
                  navigate{" "}
                  <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">Enter</kbd>{" "}
                  select{" "}
                  <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">Esc</kbd>{" "}
                  close
                </div>
              </div>
            )}
          </div>

          <button
            type="submit"
            className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
          >
            Search
          </button>

          {/* Column manager button */}
          <button
            type="button"
            onClick={onShowColumnManager}
            className="rounded-md border border-gray-300 p-1.5 text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            title="Manage columns"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"
              />
            </svg>
          </button>
        </div>
      </form>

      {/* Active filters */}
      {filters.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {filters.map((filter) => (
            <span
              key={filter.id}
              className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-xs text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
            >
              <button
                onClick={() => onEditFilter(filter)}
                className="flex items-center gap-1 hover:underline"
                title="Click to edit this filter"
              >
                <span className="font-medium">{filter.key}</span>
                <span className="text-blue-500 dark:text-blue-400">
                  {filter.operator}
                </span>
                <span>{filter.value}</span>
              </button>
              <button
                onClick={() => onRemoveFilter(filter.id)}
                className="ml-1 rounded-full p-0.5 hover:bg-blue-200 dark:hover:bg-blue-800"
                title="Remove filter"
              >
                <svg
                  className="h-3 w-3"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            </span>
          ))}
          <button
            onClick={onClearAllFilters}
            className="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          >
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}
