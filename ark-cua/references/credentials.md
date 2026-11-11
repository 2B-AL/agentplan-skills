# Login sync and credential resources

Use CookieCloud for browser login sync. Named environment variables, Secrets, Credential Sets and managed files use Credential Agent.

## Browser session sync

Use the current login and the user's selected desktops and websites. CookieCloud handles browser login sync; `credentials` commands handle other resources. Once configured, the local extension uploads on its own schedule or when the user clicks Manual Sync（手动同步）, and CUA imports each upload. All desktops of one owner share one connection: one extension configuration and one upload reach every desktop that joined. A Chrome debugging connection is needed only to change the extension's configuration.

1. Check results first, without Chrome:

   ```bash
   python3 scripts/cua.py browser-sync --desktop-id <id> [--desktop-id <another-id>] --domain <full-domain> [--wait-seconds 60]
   ```

   Repeat `--domain` for each selected website and `--desktop-id` for each selected desktop, or use `--all-desktops` when the user wants every ready desktop of this login; desktops that are not ready are listed in `data.skipped_desktops`. A desktop that is not yet receiving joins the connection this computer's extension uploads to, with no Chrome change. With several desktops, `data.desktops` holds each desktop's result and `data.status` is the least finished one. Act on `data.status`:
   - `synced`: report each website's `last_synced_at`, then refresh the remote websites to confirm login.
   - `importing`: rerun with `--wait-seconds 60`.
   - `user_action_required`: follow `next.agent_hint` for `data.reason`. `setup_required` and `chrome_connection_required` lead to step 2. For `connection_changed` (the desktop is not in the connection this computer's extension uploads to) and `different_connections` (the selected desktops cannot share one upload), tell the user and act only on their choice; either change stops some connection's uploads. Other reasons, such as `waiting_upload`, `website_not_received`, `upload_rejected`, `import_failed` and `storage_change`, need a step by the user in the extension, such as Manual Sync（手动同步）or adding a website to Sync Domain Keyword（同步域名关键词）; step 2 can make an extension change instead. With several desktops, `unavailable` marks one that could not be read; report its `error_code`.
2. Configure the extension only when step 1 requires it or the user asks for a change. The user installs the official [CookieCloud Chrome extension](https://chromewebstore.google.com/detail/cookiecloud/ffjiejobkoibkjlhjnlgmcnnigeelbdl) in their local Chrome and signs in to the selected websites. This needs Node.js 22+ and an authorized local Chrome debugging address: in Chrome 144+, the user enables `chrome://inspect/#remote-debugging` and approves the connection prompt. Reuse this browser and its login state.

   ```bash
   python3 scripts/cua.py browser-sync --desktop-id <id> [--desktop-id <another-id>] \
     --cdp-url <authorized-local-address> --domain <full-domain> [--with-storage]
   ```

   The helper joins each desktop to its owner's connection, writes the extension once, remembers on this computer which connection the extension uploads to, adds the websites to the extension's local list and keeps its other settings. Local Storage sync stays off unless `--with-storage` is given. Use that flag only when a website's login needs page storage: large page storage can get the upload rejected. If the result reports `confirmation_required`, explain it to the user and repeat only after they agree. Use `--confirm-desktop-switch` when the extension uploads to another connection, whose desktops then stop receiving updates. Desktops that do not share one connection are refused before Chrome is changed. Use `--confirm-domains` when the extension syncs all websites and would narrow to the selected ones.
3. Report `configured` as saved extension settings and `uploaded` as a confirmed upload receipt, then confirm imports with step 1. **Connectors → Browser session sync（连接器 → 浏览器登录态）** shows successful imports and the latest rejected upload. After setup, the user can turn off remote debugging.

To stop one desktop, run `browser-sync-leave --desktop-id <id>`; the connection's other desktops keep receiving uploads. To end browser sync for every desktop of the connection, run `browser-sync-revoke --desktop-id <id> --confirm`; a later setup creates a new connection and needs step 2. Run either only on explicit request; logins already imported into the desktops stay.

Configuration passwords, upload credentials and Cookie values stay inside the local helper. An upload receipt alone does not confirm desktop import or website login. Never repeat an upload automatically.

## Credential Agent resources

The local computer is the source and the requested cloud desktop is the target. The bundled CLI prepares Credential Agent as needed; these resources do not require the Chrome extension.

1. Run `credentials status [--desktop-id <id>]` to check readiness without changing devices. Agent readiness does not indicate browser login status. On `TARGET_CAPABILITY_UNAVAILABLE`, report that the requested resource sync is unavailable and stop setup.
2. Run `credentials setup --skip-browser [--desktop-id <id>]` when resource setup is requested or the target is not ready.
3. Run one exact resource command:

   ```bash
   python3 scripts/cua.py credentials sync env --desktop-id <id> <name>...
   python3 scripts/cua.py credentials sync secret --desktop-id <id> <name>...
   python3 scripts/cua.py credentials sync credential-set --desktop-id <id> --type <type> --name <name>
   python3 scripts/cua.py credentials sync file --desktop-id <id> --profile <profile>
   ```

4. Report the delivery result and any Job ID. A timeout or failure does not prove that nothing was delivered; do not submit the same delivery again blindly.

Resource arguments name what to deliver, not secret values:

| Resource | Source and use |
| --- | --- |
| `env <name>...` | Syncs only the named local environment variables; it does not set the global desktop environment. |
| `secret <name>...` | Names the Secrets to deliver. The Agent may request values through a hidden local terminal prompt. |
| `credential-set --type <type> --name <name>` | Selects a supported set type and name; the Agent handles any field prompts in the user's local terminal. |
| `file --profile <profile>` | Selects one supported built-in file profile, such as `codex-auth` or `aws-credentials`; the argument is not an arbitrary file path. |

If an Agent prompt cannot run interactively, ask the user to run the same command in their own terminal. Keep secret values and pairing codes inside the Agent flow, outside chat, model context and command arguments. Delivery is separate from using a resource; do not claim it changed the global environment or arbitrary files.

## Reset Credential Agent

```bash
python3 scripts/cua.py credentials reset --desktop-id <id> [--device-id <exact-id>]
```

Reset only the requested desktop's Agent. The command must confirm revocation of the exact target Device ID before resetting it; if revocation is unconfirmed, stop. CookieCloud browser sync is managed independently.
