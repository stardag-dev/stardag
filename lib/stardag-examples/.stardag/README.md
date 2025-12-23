# Stardag Project Configuration

This directory contains project-specific Stardag configuration.

## config.json

The `config.json` file sets defaults for this project:

```json
{
  "profile": "local",
  "allowed_organizations": ["stardag-examples"]
}
```

### Fields

| Field                   | Description                                      |
| ----------------------- | ------------------------------------------------ |
| `profile`               | Default profile to use (`local` for dev)         |
| `allowed_organizations` | Organizations that can be used with this project |

### Why Use Project Config?

1. **Consistency**: All contributors use the same defaults
2. **Safety**: The `allowed_organizations` field prevents accidentally building against the wrong organization
3. **Convenience**: No need to manually configure org/workspace for each contributor

### Overriding

Project config can be overridden by:

1. User profile config (`~/.stardag/profiles/{profile}/config.json`)
2. Environment variables (`STARDAG_ORGANIZATION_ID`, etc.)

See the main [CONFIGURATION_GUIDE.md](/CONFIGURATION_GUIDE.md) for full details.
