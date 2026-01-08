# Task Explorer Search UX Specification

## Overview

The Task Explorer search bar provides a powerful yet intuitive way to filter tasks using structured queries. This document specifies the intended user experience.

## Filter Syntax

Filters follow the format: `key operator value`

### Display Format (in filter chips)

```
task_name = MyTask
param.lr > 0.01
status != failed
```

### Input Format (typed in search bar)

```
task_name:MyTask          # Shorthand for equals
task_name:=:MyTask        # Explicit equals
param.lr:>:0.01           # Greater than
status:!=:failed          # Not equals
task_name:~:train         # Contains (substring match)
```

## Operators

| Operator      | Description                 | Example                                    |
| ------------- | --------------------------- | ------------------------------------------ |
| `=` (default) | Exact match                 | `status:=:completed` or `status:completed` |
| `!=`          | Not equal                   | `status:!=:failed`                         |
| `>`           | Greater than                | `param.epochs:>:10`                        |
| `<`           | Less than                   | `param.lr:<:0.1`                           |
| `>=`          | Greater than or equal       | `param.batch_size:>=:32`                   |
| `<=`          | Less than or equal          | `created_at:<=:2024-01-01`                 |
| `~`           | Contains (case-insensitive) | `task_name:~:train`                        |

## Autocomplete Flow

The search bar provides progressive autocomplete through three stages:

### Stage 1: Key Selection

When the user starts typing, show matching keys:

```
User types: "par"
Dropdown shows:
  Keys
  ├─ param.lr
  ├─ param.epochs
  └─ param.batch_size
```

**Keyboard navigation:**

- `↓` / `↑` - Move selection through options
- `Enter` - Select highlighted option
- `Escape` - Close dropdown
- Continue typing - Filter options

**After selection:** The key is inserted followed by a colon, cursor stays in input.

```
Search bar: "param.lr:"
```

### Stage 2: Operator Selection

After typing a key and colon, show available operators:

```
User has: "param.lr:"
Dropdown shows:
  Operators
  ├─ =   (equals)
  ├─ !=  (not equals)
  ├─ >   (greater than)
  ├─ <   (less than)
  ├─ >=  (greater or equal)
  ├─ <=  (less or equal)
  └─ ~   (contains)
```

**After selection:** The operator is inserted followed by a colon, cursor stays in input.

```
Search bar: "param.lr:>:"
```

**Shortcut:** User can skip operator selection by typing the value directly.
This defaults to `=` (equals).

```
User types: "param.lr:0.01" → Creates filter: param.lr = 0.01
```

### Stage 3: Value Selection

After operator, show matching values:

```
User has: "status:=:"
Dropdown shows:
  Values for status
  ├─ completed (45)
  ├─ pending (23)
  ├─ running (12)
  └─ failed (5)
```

**After selection or Enter:** The filter is added to the active filters list.

## Complete Examples

### Example 1: Using all autocomplete stages

```
1. User types: "sta"
   Dropdown: [status, started_at, ...]

2. User presses ↓ to highlight "status", then Enter
   Search bar: "status:"

3. Dropdown shows operators: [=, !=, >, ...]
   User presses Enter (selects =)
   Search bar: "status:=:"

4. Dropdown shows values: [completed, pending, ...]
   User presses ↓ twice to "running", then Enter

5. Filter added: status = running
   Search bar cleared, ready for next filter
```

### Example 2: Quick filter with implicit equals

```
1. User types: "task_name:MyTask"
2. User presses Enter
3. Filter added: task_name = MyTask
```

### Example 3: Numeric comparison

```
1. User types: "param.epochs"
2. Autocomplete highlights, user presses Enter
   Search bar: "param.epochs:"

3. User types: ">" (starts with operator character)
   Search bar: "param.epochs:>"

4. User types: ":100" or "100"
   Search bar: "param.epochs:>:100" or "param.epochs:>100"

5. User presses Enter
   Filter added: param.epochs > 100
```

## Interaction Patterns

### Mouse Interaction

- Click on dropdown option → Select and continue
- Click outside dropdown → Close dropdown, keep text

### Keyboard Interaction

- `↓` / `↑` → Navigate dropdown (wraps around)
- `Enter` → Select highlighted option OR submit filter if no dropdown
- `Escape` → Close dropdown
- `Tab` → Select highlighted option and continue
- Typing → Filter/update suggestions

### Focus Behavior

- After selecting any autocomplete option, focus returns to input
- Input cursor positioned at end of text
- User can immediately continue typing

## Filter Chip Interactions

### Display

Filters appear as chips below the search bar:

```
[task_name = MyTask ×] [param.lr > 0.01 ×] [Clear all]
```

### Click to Edit

Clicking a filter chip:

1. Removes the filter from active filters
2. Populates search bar with: `key:operator:value`
3. Focuses the search bar for editing

### Remove Filter

Clicking the × button removes the filter.

## Special Cases

### Values with Colons (URLs, timestamps)

The parser handles values containing colons:

```
Input: "url:=:http://example.com:8080/path"
Parsed: key="url", op="=", value="http://example.com:8080/path"
```

### Text Search

If the input doesn't match the filter pattern, it's treated as a text search across task name and namespace.

## Visual Design

### Autocomplete Dropdown

- Shows section header: "Keys", "Operators", or "Values for {key}"
- Currently selected item has distinct background
- Shows count for values (when available)
- Max 10 items shown
- Positioned directly below input, same width

### Filter Chips

- Blue background to indicate active filter
- Shows: `key operator value` with operator in lighter color
- × button on right side for removal
- Clickable for editing
