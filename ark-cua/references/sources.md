# Adapter sources

The self-contained adapters in `vendor/` preserve the complete command surfaces
of these reviewed source revisions:

| Scheme | Repository | Commit |
| --- | --- | --- |
| AgentPlan | `2B-AL/ap-cua-skill` | `1318629003f8c11f249915b456b4b0c7bda18e7e` |
| ByteSSO | `cua_skill_bytesso` | `43804ad654831a9e74ca6c35934ab530d126f58c` |

The unified package intentionally changes only:

- state defaults from legacy `~/.openclaw/...` paths to scheme-scoped paths
  below `~/.ark-agentplan/ark-cua/`;
- generated retry/setup commands to point back to the unified launcher;
- ByteSSO dependency runtime storage to the unified runtime namespace;
- ByteSSO local self-test paths for the vendored layout.

The unified launcher adds scheme resolution, capability gating, state isolation,
desktop command aliases, and explicit pre-network validation. It does not
rewrite either backend protocol.
