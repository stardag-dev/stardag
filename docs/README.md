# Stardag Documentation

This directory contains the source for [docs.stardag.com](https://docs.stardag.com) (or GitHub Pages until custom domain is configured).

Built with [MkDocs](https://www.mkdocs.org/) + [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/).

## Quick Start

```bash
cd docs

# Install dependencies
uv sync

# Start development server (live reload)
uv run mkdocs serve
# Open http://localhost:8000

# Build static site
uv run mkdocs build
# Output: docs/site/
```

## Project Structure

```
docs/
├── README.md           # This file
├── pyproject.toml      # Python dependencies (uv-managed)
├── uv.lock             # Locked dependencies
├── mkdocs.yml          # MkDocs configuration
├── docs/               # Documentation source (markdown)
│   ├── index.md        # Landing page
│   ├── getting-started/
│   ├── concepts/
│   ├── how-to/
│   ├── configuration/
│   ├── platform/
│   └── reference/
└── site/               # Built output (gitignored)
```

## Common Tasks

### Adding a New Page

1. Create a markdown file in the appropriate section:

   ```bash
   touch docs/docs/how-to/new-guide.md
   ```

2. Add it to the navigation in `mkdocs.yml`:

   ```yaml
   nav:
     - How-To Guides:
         - how-to/index.md
         - New Guide: how-to/new-guide.md # Add this
   ```

3. Preview with `uv run mkdocs serve`

### Editing Existing Content

1. Find the file in `docs/docs/`
2. Edit the markdown
3. Changes auto-reload in the dev server

### Adding API Documentation

API docs are auto-generated from docstrings using [mkdocstrings](https://mkdocstrings.github.io/).

To document a new module in `reference/api.md`:

<!-- prettier-ignore -->
```markdown
## My Module

::: stardag.my_module
    options:
      show_root_heading: true
      members:
        - MyClass
        - my_function
```

### Updating Dependencies

```bash
cd docs
uv add some-package        # Add new dependency
uv sync                    # Sync after pyproject.toml changes
```

### Tun Tests

Using [pytest-markdown-docs](https://github.com/modal-labs/pytest-markdown-docs).

```bash
uv run pytest --markdown-docs --markdown-docs-syntax=superfences docs/**/*.md;
```

## CI/CD & Hosting

### GitHub Pages (Current)

Deployment is configured via `.github/workflows/docs.yml`.

**Current state**: Manual trigger only (workflow_dispatch)

**To enable automatic deployment**:

1. Go to repo Settings > Pages
2. Set "Source" to "GitHub Actions"
3. Uncomment the `on: push` trigger in the workflow:
   ```yaml
   on:
     push:
       branches:
         - main
       paths:
         - "docs/**"
         - "lib/stardag/src/stardag/**"
   ```

**Manual deployment**:

1. Go to Actions tab
2. Select "Deploy Documentation"
3. Click "Run workflow"

### Custom Domain (Future)

To use `docs.stardag.com`:

1. Add CNAME record: `docs.stardag.com` → `andhus.github.io`
2. Create `docs/docs/CNAME` with content: `docs.stardag.com`
3. Update `site_url` in `mkdocs.yml`
4. Enable "Enforce HTTPS" in GitHub Pages settings

## Content Guidelines

### TODOs

Use HTML comments for TODOs that need verification:

```markdown
<!-- TODO: Verify this behavior with maintainer -->
```

### Code Examples

Use fenced code blocks with language hints:

````markdown
```python
import stardag as sd

@sd.task
def example() -> int:
    return 42
```
````

### Admonitions

Material theme supports admonitions:

<!-- prettier-ignore -->
```markdown
!!! note
    This is a note.

!!! warning
    This is a warning.

!!! tip
    This is a tip.
```

### Tabs

For showing alternatives:

<!-- prettier-ignore -->
````markdown
=== "pip"
    ```bash
    pip install stardag
    ```

=== "uv"
    ```bash
    uv add stardag
    ```
````

## Troubleshooting

### Build Warnings

The build may show warnings about docstrings in the SDK. These don't prevent the build but should be fixed:

```
WARNING - griffe: .../target/_factory.py: Confusing indentation...
```

To enforce strict mode (fail on warnings):

```bash
uv run mkdocs build --strict
```

### mkdocstrings Not Finding Modules

Ensure stardag is installed in the docs venv:

```bash
uv sync  # Should install stardag from ../lib/stardag
```

### Prettier Formatting Issues

The `reference/api.md` file is excluded from prettier in `.pre-commit-config.yaml` because prettier breaks mkdocstrings YAML syntax (the indented `options:` block under `:::` directives).

## Related Files

- `.github/workflows/docs.yml` - CI/CD workflow
- `.claude/tasks/hosted-docs.md` - Task tracking and decisions
- `lib/stardag/src/stardag/__init__.py` - Module docstring (shown in API reference)
