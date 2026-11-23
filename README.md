# crittersdeck

A status board for your Claude Code sessions, across accounts and machines. Every session is a critter: you can tell from across the room which ones are working, which ones need you and which ones are done. When a session asks for permission, you can allow or deny it from the board.

- **Any account, any machine.** Add a *source* for each Claude account or computer (personal, work, a server...). Each one gets its own token and colour.
- **Any way you run Claude Code.** Sessions report through Claude Code's own HTTP hooks, so the terminal, the desktop app, the web, Remote Control and multiplexers all show up the same way. Nothing to install on the machines that run the agents.
- **Unblock sessions from the board.** A permission prompt shows in the session *and* on the board at the same time. Whichever answers first wins; there is no time limit.

| Critter | Means |
|---|---|
| Bobbing, with a tool badge | Working (`>_` shell, `✎` editing, `⌕` reading or searching, `◎` web) |
| `?` badge, orange ring, listed under **Needs you** | Waiting for a permission or an answer |
| Striped tile, `!` badge | Finished, and you have not looked yet |
| Grey, asleep | Idle; quiet for a while |
| Faded, dashed | The session ended (hidden after a day) |

## Run it

```sh
pip install .
python -m crittersdeck hash-password          # prints a hash for your board password
export CRITTERSDECK_PASSWORD_HASH='scrypt$...'
export CRITTERSDECK_PUBLIC_URL='https://crittersdeck.example.com'   # the URL the hooks will call
python -m crittersdeck serve --port 7420 --ingest-port 7421
```

The board (UI and API) listens on `--port`, on 127.0.0.1 unless you pass `--host`. With `--ingest-port`, a second listener serves only `POST /ingest`: that is the only one to publish to the internet. The board also accepts `/ingest`, for machines on the same network.

Or with Docker:

```sh
docker build -t crittersdeck .
docker run -d -p 127.0.0.1:7420:7420 -p 7421:7421 -v crittersdeck:/data \
  -e CRITTERSDECK_PASSWORD_HASH='scrypt$...' \
  -e CRITTERSDECK_PUBLIC_URL='https://crittersdeck.example.com' crittersdeck
```

| Variable | Default | |
|---|---|---|
| `CRITTERSDECK_PASSWORD_HASH` | | Board password hash (`python -m crittersdeck hash-password`). |
| `CRITTERSDECK_TRUST_PROXY_HEADER` | | Behind an auth proxy (OIDC, forward auth): the header that carries the signed-in user, e.g. `X-Forwarded-User`. |
| `CRITTERSDECK_TRUSTED_PROXIES` | | Required with the header: comma-separated IP addresses or CIDR ranges of the proxy. The header is ignored from any other address. Use the proxy's real address, never a Docker bridge gateway: with Docker's port publishing every client can appear to come from the gateway. Behind a proxy on another host, publish 7420 only on the interface that proxy uses, or use host networking. |
| `CRITTERSDECK_PUBLIC_URL` | the board's own URL | Base URL written into the hooks. |
| `CRITTERSDECK_DB` | `crittersdeck.db` | SQLite file. |
| `CRITTERSDECK_PLUGIN_MARKETPLACE` | | Where machines can fetch this repository as a Claude Code plugin marketplace (git URL or GitHub `owner/repo`). Enables the install commands. |
| `CRITTERSDECK_IDLE_MINUTES` | `30` | Quiet time before a working session reads as idle. |

With Docker Compose, write the password hash with every `$` doubled (`scrypt$$...$$...`) in the `.env` file: Compose expands `$` in env files.

## Connect a machine

On the board, open **Sources**, give the source a name and a colour, and create it. The board shows the token once, and the commands to run on that machine:

```sh
claude plugin marketplace add <where this repo is, e.g. https://github.com/you/crittersdeck>
claude plugin install crittersdeck@crittersdeck --config ingest_url=https://ingest.example.com --config token=cd_...
```

That installs the **crittersdeck plugin** (in `plugin/`): nothing to edit by hand, and `claude plugin uninstall crittersdeck@crittersdeck` removes it, with its settings and token. The token is kept in Claude Code's secure credential store. Leave out the `--config` parts if you'd rather not have the token in your shell history: Claude Code then asks for both values.

For the board to show these commands, set `CRITTERSDECK_PLUGIN_MARKETPLACE` to the git URL (or `owner/repo` on GitHub) where the machines can fetch this repository. Without it, the board shows a `hooks` block to merge into `~/.claude/settings.json` by hand instead.

How the plugin works: each hook runs `curl` directly (exec form, no shell), so there is no runtime to install. It needs curl 8.3 or later (for `--variable`), which current Windows 10 and 11, macOS and Linux releases include. Unlike Claude Code's `http` hooks, `curl` can reach a board on a private address. If the board is down, `curl` fails and Claude Code carries on as usual.

- The token reaches `curl` through the hook's environment, not its command line, so other users of the machine cannot read it from the process list.
- Only the board's answer to a permission request becomes hook output. For every other event the response is written to the plugin's data folder and ignored, so a board cannot add text to your sessions.
- If Claude Code is killed while a permission request is open, its `curl` may keep the request on the board. Use **Forget session** on the board to clear it.

### Titles from Claude (optional)

By default a session is named after the first line of its first prompt, and you can rename it on the board. To show the title Claude gives each session instead (the one in the Claude app's sidebar), also install the **crittersdeck-titles** plugin, with the same values:

```sh
claude plugin install crittersdeck-titles@crittersdeck --config ingest_url=https://ingest.example.com --config token=cd_...
```

It needs `python3`. After each prompt and each reply, its script (`plugin-titles/scripts/titles.py`, run in the background so it never delays the session) finds the title and sends it to the board:

- For sessions linked to claude.ai (the desktop app, the web, Remote Control) it asks claude.ai, using this machine's Claude sign-in. That API is not documented, so it can stop working with any Claude Code update; the board then keeps the first-prompt title.
- For terminal sessions it reads the title Claude Code writes to the session's transcript.

The Claude sign-in is read from `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`) and used only for that request to `api.anthropic.com`, which is never redirected elsewhere: the board receives the title and nothing else. It does not work where Claude Code keeps the sign-in in the system keychain (macOS); terminal sessions still work there. On a work account, check your organisation's policy before installing it. A name you give a session on the board always wins over Claude's.

Start a session and its critter appears with the first event. Deleting a source on the board revokes its token at once.

With the hand-made `hooks` block (Claude Code `http` hooks), note that Claude Code refuses HTTP hooks that resolve to a private address (`ERR_HTTP_HOOK_BLOCKED_ADDRESS`; loopback is allowed), so its URL must resolve to a public address on every machine. The plugin does not have this limit.

## Security

- **Whoever can open the board can approve commands on your machines.** Keep the board behind a password or an auth proxy, and do not publish it. Publish only the ingest listener (`--ingest-port`), which serves nothing but `POST /ingest`.
- The board shows exactly what Allow will run: hidden characters (controls, bidi overrides, zero-width) are shown as `\u{...}`, and a request too long to show in full can only be denied on the board.
- Limits per source: 20 events per second, 200 sessions (the quietest is dropped), 10 open permission requests (more are left to the session's own dialog).
- Claude Code's HTTP hooks send the whole hook event, tool input included (commands, file paths, sometimes file contents). Use HTTPS for any machine outside your network. The board keeps only a short summary per session: never the full tool input, and of the prompts only the first line (60 characters at most) of each session's first prompt, as its title (or Claude's title for the session, with crittersdeck-titles). Rename a session on the board to replace it. The full input of an open permission request stays in memory until it is answered.
- Tokens are random, stored as hashes, and only allow sending events. Events are size- and rate-limited per source.
- Answering on the board needs a signed-in session and a CSRF token, and works once per request.

## Trust

Connecting a machine means trusting the board with it: whoever can answer at the ingest address can allow tool calls in that machine's sessions. That includes the board's host and anything that terminates TLS in front of it (a reverse proxy or tunnel). Install the plugin from a repository you control, keep auto-update off for this marketplace, and read the changes to `plugin/hooks/hooks.json` before `claude plugin update`.

## Limits

- Questions with options (`AskUserQuestion`) are shown as *needs you*, but they are answered in the session: they do not go through a hook.
- Context use and cost are not shown: they live in the local transcript, which the hooks do not send.
- Claude Code only. Other agents are not supported yet.
- Requests are tracked per session: if a subagent of the same session uses a tool while a request is open, the board withdraws it (the session's own dialog still has it).

## Develop

Dependencies for the Docker image are pinned with hashes in `requirements.lock` (`uv pip compile pyproject.toml --generate-hashes -o requirements.lock`).


```sh
pip install -e '.[test]'
pytest
```

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
