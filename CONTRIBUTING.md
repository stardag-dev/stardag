# Contributing to Stardag

Thank you for your interest in contributing to Stardag! This document provides guidelines and information for contributors.

## Code of Conduct

Please be respectful and constructive in all interactions. We're building something together.

## License Structure

Stardag uses a split licensing model. **Your contribution's license depends on where in the codebase you're contributing:**

| Directory               | License    | CLA Required |
| ----------------------- | ---------- | ------------ |
| `lib/` (SDK & Examples) | Apache 2.0 | No           |
| `docs/`                 | Apache 2.0 | No           |
| `app/` (API & UI)       | BSL 1.1    | **Yes**      |

### Why the split?

- **SDK (`lib/`)**: Fully open source so developers can freely build on Stardag
- **Platform (`app/`)**: BSL 1.1 allows self-hosting but prevents competing SaaS offerings

### Contributing to `app/`

Contributions to the `app/` directory require signing a Contributor License Agreement (CLA). This allows us to:

- Maintain licensing flexibility
- Potentially relicense to a more permissive license in the future
- Protect both you and the project legally

See [CLA.md](app/CLA.md) for the full agreement. By submitting a PR to `app/`, you agree to the CLA terms.

## Getting Started

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Node.js 18+ (for UI development)

### Development Setup

See [DEV_README.md](DEV_README.md) for complete setup instructions, including:

- Installing all packages
- Running tests and linting
- Running the full stack (API + UI)
- Authentication for local development

## Making Contributions

### 1. Find or Create an Issue

- Check [existing issues](https://github.com/stardag-dev/stardag/issues)
- For new features, open an issue first to discuss the approach

### 2. Fork and Branch

```bash
git checkout -b feature/your-feature-name
```

### 3. Make Your Changes

- Follow existing code style
- Add tests for new functionality
- Update documentation if needed

### 4. Run Tests and Linting

```bash
# In lib/stardag
uv run pytest
uv run pre-commit run --all-files
```

### 5. Submit a Pull Request

- Provide a clear description of the changes
- Reference any related issues
- For `app/` changes: confirm CLA agreement in PR description

## Code Style

### Python

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting
- Type hints are required for public APIs
- Follow existing patterns in the codebase

### TypeScript (UI)

- We use ESLint and Prettier
- Follow existing component patterns

## Testing

- **Unit tests**: Required for new functionality in `lib/`
- **Integration tests**: In `integration-tests/` directory
- **E2E tests**: Run with `./scripts/e2e-test.sh`

## Documentation

- Documentation source is in `docs/`
- Preview locally: `cd docs && uv run mkdocs serve`
- API docs are auto-generated from docstrings

## Questions?

- Open a [GitHub Discussion](https://github.com/stardag-dev/stardag/discussions)
- Email: info@stardag.com

## Thank You!

Every contribution helps make Stardag better. We appreciate your time and effort!
