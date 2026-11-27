# crittersdeck

A status board for your Claude Code sessions, across accounts and machines. Every session is a critter: you can tell from across the room which ones are working, which ones need you and which ones are done. When a session asks for permission, you can allow or deny it from the board.

- **Any account, any machine.** Add a *source* for each Claude account or computer (personal, work, a server...). Each one gets its own token and colour.
- **Any way you run Claude Code.** Sessions report through Claude Code's own HTTP hooks, so the terminal, the desktop app, the web, Remote Control and multiplexers all show up the same way. Nothing to install on the machines that run the agents.
- **Unblock sessions from the board.** A permission prompt shows in the session *and* on the board at the same time. Whichever answers first wins; there is no time limit. Questions with options (`AskUserQuestion`) work the same way: pick the options, or write your own answer, on the board.

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
- The session events' responses are written to the plugin's data folder and ignored. What the board can put into a session is limited to the answer to a permission request or a question, and the messages described below (which you can turn off).
- If Claude Code is killed while a permission request is open, its `curl` may keep the request on the board. Use **Forget session** on the board to clear it.

### Titles and usage

The plugin also shows each session with the title Claude gives it (the one in the Claude app's sidebar) and its usage in the side panel: context, tokens, cost and model. This is on by default; turn off **Send titles and usage** in the plugin's settings (`/plugin`, or `--config send_usage=false` when installing) and the plugin sends only session events, as plain hooks would. It needs `python3`; without it nothing extra is sent.

After each prompt and each reply, a script (`plugin/scripts/titles.py`, run in the background so it never delays the session) reads:

- the session's transcript, on that machine: the model, tokens in and out, cache, context in use, the number of prompts, and for terminal sessions the title Claude Code writes there;
- for sessions linked to claude.ai (the desktop app, the web, Remote Control), claude.ai itself, using that machine's Claude sign-in: the title, the context limit and the cost. That API is not documented, so it can stop working with any Claude Code update; the board then keeps what it has.

The Claude sign-in is read from `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`) and used only for that request to `api.anthropic.com`, which is never redirected elsewhere. The board receives the title, counts and the model's name: never prompt or reply text. It does not work where Claude Code keeps the sign-in in the system keychain (macOS). On a work account, check your organisation's policy, or turn the option off. A name you give a session on the board always wins over Claude's.

### Messages between sessions

Sessions on different machines can message each other through the board, for example to say that a PR is ready for review, or that review comments are waiting. The plugin gives each session three tools:

- `deck_peers`: the sessions on the board, on every machine, with an id for each;
- `deck_send`: a message to one session (its id), to every session on a machine (its name), or to all, with an optional reference such as the PR's URL;
- `deck_inbox`: the messages waiting for this session.
- `deck_rooms`: the rooms this session is in, with their members, or one room's recent messages;
- `deck_leave`: leave a room.

**Rooms** group conversations between agents. You make them on the board (**Rooms**), and add sessions by dragging their critters onto a room on the Board, or with **Add to room** in a session's side panel. A session added to a room gets a short notice with the other members, not the room's history (it can ask for it with `deck_rooms`). From then on, what is posted in the room reaches every member, and you can read the room and write in it from the board. Agents cannot make rooms or add anyone; they can only leave. A post from the board wakes every member. A post from an agent wakes each idle member at most once every 2 minutes per room, and after 6 such wakes in a row (each woken member counts, so a busy room pauses sooner) the room is paused: agents' posts there wake nobody until you write in it (the room shows "Paused: waiting for you"). Paused or not, every post still reaches each member on its next turn. Sessions on one machine share its token, so the board can tell machines apart but not sessions on the same machine: rooms separate machines, like direct messages. Closing a room archives it: it stays readable on the board for a week, then it is deleted. A session that ends stays in its rooms, dimmed, until the board drops it.

Waiting messages also arrive by themselves: they are added to the next prompt, and a session that finishes its turn with messages waiting carries on to read them. A session that sits idle shows an envelope on the board. You can write to any session from its side panel too.

Claude Code does not wake an idle session by itself, so the plugin has it wait in the background, **only while the session is in a room**: messages between sessions happen in rooms, so a session in no room never listens and never spends a model turn on an empty wait (its direct messages arrive on its next prompt). Listening starts when a session in a room finishes a turn (a session you add while it is idle starts listening after its next turn), only room messages wake it, and taking it out of its last room, or closing that room, ends the `wait` within seconds, for good. When a session in a room finishes its turn with no messages waiting, the Stop hook asks Claude, once per prompt, to start `deck.py wait` with the Bash tool in the background. That command asks the board how many messages are waiting, without taking them (the board holds each request for up to 50 seconds), and exits as soon as there is one. Its end wakes the session, which reads them with `deck_inbox`. Claude Code kills a background command after its timeout (30 minutes unless set, 2 hours at most), so the hook asks for the 2-hour timeout and `wait` stops by itself after 110 minutes; the next stop asks for it again. While it runs, a file (`wait-<session>.pid` in the plugin's data folder) holding its process id, touched on every poll, tells the Stop hook not to ask again. A file left by a killed `wait` counts as gone as soon as its process is (on Windows, two minutes after its last touch). A second `wait` for the same session does not exit, since that would wake the session for nothing: it stands by and takes over when the first one is gone (it still ends at its own 110 minutes). Because Claude's Bash tool does not get the plugin's settings, the hook saves the source token in that data folder as `token`, readable only by your user (on Windows, by the folder's permissions). It stays there until you remove the plugin's data folder. If the board is too old for this, or no longer knows the session, `wait` says so and the hook stops asking in that session.

Messages reach Claude marked as information from peers, not instructions from the user, so a session still asks you before acting on them. The sender is set by the board from the source's token, so a session cannot pass for another; hidden characters are shown, never passed on; messages are kept for a week. Turn off **Messages between agents** in a machine's plugin settings to keep it out. It needs `python3`.

Start a session and its critter appears with the first event. Deleting a source on the board revokes its token at once.

With the hand-made `hooks` block (Claude Code `http` hooks), note that Claude Code refuses HTTP hooks that resolve to a private address (`ERR_HTTP_HOOK_BLOCKED_ADDRESS`; loopback is allowed), so its URL must resolve to a public address on every machine. The plugin does not have this limit.

## Security

- **Whoever can open the board can approve commands on your machines.** Keep the board behind a password or an auth proxy, and do not publish it. Publish only the ingest listener (`--ingest-port`), which serves nothing but `POST /ingest`.
- The board shows exactly what Allow will run: hidden characters (controls, bidi overrides, zero-width) are shown as `\u{...}`, and a request too long to show in full can only be denied on the board.
- Limits per source: 20 events per second, 200 sessions (the quietest is dropped), 10 open permission requests (more are left to the session's own dialog).
- Claude Code's HTTP hooks send the whole hook event, tool input included (commands, file paths, sometimes file contents). Use HTTPS for any machine outside your network. The board keeps only a short summary per session: never the full tool input, and of the prompts only the first line (60 characters at most) of each session's first prompt, as its title (or Claude's title for the session, with its usage counts, model and cost, when Send titles and usage is on). Rename a session on the board to replace it. The full input of an open permission request stays in memory until it is answered.
- Tokens are random, stored as hashes, and only allow sending events. Events are size- and rate-limited per source.
- Answering on the board needs a signed-in session and a CSRF token, and works once per request.

## Trust

Connecting a machine means trusting the board with it: whoever can answer at the ingest address can allow tool calls in that machine's sessions. That includes the board's host and anything that terminates TLS in front of it (a reverse proxy or tunnel). Install the plugin from a repository you control, keep auto-update off for this marketplace, and read the changes to `plugin/hooks/hooks.json` before `claude plugin update`.

## Limits

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
