# Auth scheme selection

## Contents

- [Resolution order](#resolution-order)
- [Atomic bundle](#atomic-bundle)
- [State layout](#state-layout)
- [Capability behavior](#capability-behavior)

## Resolution order

Resolve exactly one scheme in this order:

1. `--auth-scheme`;
2. `ARK_CUA_AUTH_SCHEME`;
3. the user's persisted `auth-scheme use` selection;
4. `agentplan`.

Only `agentplan` and `bytesso` are valid. Do not infer a scheme from credentials,
network reachability, legacy state, or command names.

## Atomic bundle

The scheme atomically selects:

- authentication behavior;
- deployment endpoints;
- transport protocol;
- capability set;
- state namespace;
- error behavior.

Never combine an auth provider from one scheme with endpoints or commands from
the other.

## State layout

```text
~/.ark-agentplan/ark-cua/
├── selection.json
├── auth-schemes/
│   ├── agentplan/
│   │   ├── auth.json
│   │   └── session.json
│   └── bytesso/
│       ├── auth.json
│       └── session.json
└── runtime/
    └── bytesso/
        └── dependencies/
```

Override the root only with `ARK_CUA_STATE_DIR`. The unified launcher forces
the vendored adapters to use the selected namespace and does not read legacy
`~/.openclaw` state.

## Capability behavior

If the chosen deployment does not provide a requested capability, fail before
network access with `CAPABILITY_UNAVAILABLE`, `accepted=false`, and
`available_in`. Never switch schemes automatically.
