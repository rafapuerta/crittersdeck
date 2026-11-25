# Rooms

Status: design, not built. Builds on the messages between sessions (`messages`, `deliveries`, the `Deck*` ingest events, the drawer's thread) and on `deck.py wait`.

## Goal

The conversations between agents are grouped in **rooms**. The board lists them. You drag critters into a room to add those sessions to it, read the room's chat and write in it. Direct messages keep working as they do today.

## Data model and storage

Two new tables, in the same SQLite file:

```sql
CREATE TABLE rooms (
    id TEXT PRIMARY KEY,                -- secrets.token_hex(6), like sources
    name TEXT NOT NULL,                 -- 1-40 chars, unique (case-insensitive) among open rooms
    created REAL NOT NULL,
    closed REAL                         -- NULL while open; closed rooms are read-only, pruned with messages
);
CREATE TABLE room_members (
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    joined REAL NOT NULL,
    PRIMARY KEY (room_id, source_id, session_id)
);
```

A room message is a row in the existing `messages` table, with `to_kind = 'room'` and a new column `room_id TEXT NOT NULL DEFAULT ''` (added by `ALTER TABLE`, like the session columns). Delivery reuses `deliveries`: one row per member that has received it. Nothing else changes for direct messages.

- A member receives the room messages posted **after it joined** (`created >= joined`), so a session added late does not get a burst of history. It can read the history on request (see `deck_rooms`).
- Members are not foreign keys to `sessions`: when a session is pruned or forgotten, a cleanup in `Store.prune` / `forget_session` removes its memberships. Messages stay with the room.
- Retention is the same as for messages (a week). A closed room is deleted once its last message is older than that.
- Limits: 50 open rooms, 30 members per room (constants, like `MAX_SESSIONS_PER_SOURCE`).

## API

Board (signed-in user, CSRF on mutations, like the other `/api/*` routes):

| Method and path | What |
|---|---|
| `GET /api/rooms` | Open rooms (id, name, members, unread per member, last message time). Also in `/api/state` so the live stream updates it. |
| `POST /api/rooms` `{name, members?}` | Create, optionally with members (`"source:session"` ids). |
| `PATCH /api/rooms/{id}` `{name}` | Rename. |
| `DELETE /api/rooms/{id}` | Close: read-only, hidden from agents, kept in history until pruned. |
| `PUT /api/rooms/{id}/members/{source}/{session}` | Add a session (drag and drop). Idempotent. |
| `DELETE /api/rooms/{id}/members/{source}/{session}` | Remove a session. |
| `GET /api/rooms/{id}/messages?before=<id>` | History, newest last, 50 per page, with who has received each message. |
| `POST /api/rooms/{id}/messages` `{text, ref?}` | Post as the board ("Rafa on the board"). |

Ingest (plugin, source token): new events go through `/ingest` like the current ones, so no new public route is needed.

| Event | Fields | What |
|---|---|---|
| `DeckRooms` | | The rooms this session belongs to, with members (labels as in `DeckPeers`). |
| `DeckSend` | `to: "room:<id>"` | Post in a room. Only a member may post. Same text limits and per-source rate limit. |
| `DeckHistory` | `room` | The last 30 messages of a room the session belongs to (does not mark them delivered). |
| `DeckJoin` / `DeckLeave` | `room` | See open question 2. |

`DeckInbox` and `DeckWait` also count and return room messages for the rooms the session is in. Each message gets `room` and `room_name` fields.

## How room messages reach members

The path is the same as for direct messages, and needs no new mechanism:

1. `Store.inbox` widens its `_FOR` clause with `OR (to_kind = 'room' AND room_id IN (SELECT room_id FROM room_members WHERE source_id = :src AND session_id = :sid AND joined <= messages.created)) AND closed IS NULL`. The sender is still excluded.
2. **Hook:** `render()` adds `room="<name>"` to the `<deck-message>` attributes, and a `reply_to="room:<id>"` so that the answer goes to the room, not to the person. The preamble is unchanged: room messages are information, not instructions.
3. **`deck_inbox`:** the same list, with the room shown.
4. **`wait`:** `DeckWait` counts room messages through the same `unread` query, so a post in a room wakes every idle member. Every post makes a `broadcast()`, which already wakes held waits.

Delivery stays per member (the `deliveries` table), and the 8000-character cap per delivery still applies.

To avoid agents chattering with each other without end in a room, two rules apply:

- A message an agent posts in a room does not wake (`wait`) a member that the same room woke **in the last 2 minutes**. It is still delivered on the member's next turn. This is a server-side cooldown per (member, room).
- The cooldown alone only slows a loop down: two agents that always answer would still wake each other every 2 minutes. So the server also counts, per room, the wakes caused by agents' posts since the board last posted there (each member woken counts as one, so one post in a room of four agents can count three). **After 6, the room is paused:** agents' posts wake nobody (they are still delivered on each member's next turn) until Rafa posts in the room, which resets the count. The room view shows "Paused: waiting for you". The count lives in memory: a restart of the board resets it.
- The per-source rate limit (10 a minute) also applies to room posts.

## MCP tools

- `deck_rooms` (new): lists my rooms with members and ids; with `room`, shows its recent history.
- `deck_send`: `to` also accepts `room:<id>` (or the room's name, when it is unique among my rooms). The description says so.
- `deck_peers`: unchanged. It also shows each peer's rooms in a column, so that an agent can tell who it can reach in a room.
- `deck_inbox`: unchanged apart from the room field.

Agents cannot create rooms or add members (open question 2). The board is where that happens.

## Panel UI

Rooms belong to the board, not to a machine. It does not matter who started one: sessions from any machines take part in it. They are drawn apart from the machine groups.

**Rooms zone (Board):** a framed band of its own below the machine groups, in the space they leave empty. It holds one **room tile** per open room and a dashed **New room** tile with a name field. A room tile shows:
- the room's name;
- its members as small critters, each in its machine's colour (an orange critter for Seismenos, a blue one for Dubois), with "+N" past 8 and ended sessions dimmed;
- the member count and the time of the last message.

Clicking a tile opens the room.

**Drag and drop (the main interaction):** every live critter tile is `draggable`. While one is being dragged, every room tile gets a dashed accent border. The tile under the pointer fills with the accent colour. Dropping the critter adds the session to the room: the server sends it the welcome notice, and DeckWait wakes it if it is idle. The critter then appears on the room tile with the next live update, and a toast confirms it. Dropping a critter that is already a member says so and changes nothing. Ended critters cannot be dragged.

**Without a mouse:**
- **Keyboard and screen readers:** a session's side panel has **Add to room** (a select of the rooms it is not in, and an Add button), and the room view has **Add a session**. Room tiles are links whose accessible name includes "Drop a critter here to add it to the room". Results are announced through the toast (an `<output>`).
- **Mobile:** touch drag is not used. The same Add to room fallback applies, and the zone stacks its tiles in one column.

**Room view (`#/rooms/<id>`):** a chat, with a side column for the members. The chat shows every message in order.
- Each message is labelled with the sender's critter in its machine's colour, the session's name, the machine's name in its colour, and the time (the hour today, the day and hour before).
- Rafa's messages, posted from the board, are marked "You (board)", aligned right, in the accent colour.
- Each message shows "reached N of M" members.
- At the bottom, a textarea posts as the board (Enter sends, Shift+Enter adds a line). On phones it stays at hand at the bottom.
- The view updates live from the board's event stream, and scrolls to the end when you are already there.

The members column lists the members as critters, each with a remove button, and has Add a session, rename and Close room (confirmed in a dialog).

**Rooms page (nav):** lists the open rooms and the closed (archived) ones, and also has a form to make a room.

**States:**
- empty room: "No messages yet. Members get what is posted from now on.", and its tile says "Empty: drop critters here";
- closed room: read-only, with a banner, and no input;
- a room that is gone: back to the list, with a toast;
- board offline: the existing link state.

## Security

- **Who manages rooms:** only the signed-in board user. Sources (tokens) cannot create, rename or close rooms, or add or remove members.
- **Who reads and posts:** a session can read and post only in the rooms it is a member of, checked server-side from the token's source plus the `session_id`. Another session on the same machine is not a member just because it shares the token. Its source must also have messaging on (`taking_part`), as for direct messages.
- **Sessions across machines:** membership is per `(source, session)`, so a room can mix machines. Each member sees the other members' labels (title, machine, short id), the same as `deck_peers` shows today, and nothing more.
- **Sessions on the same machine:** membership and posting are checked with the token plus the `session_id` the plugin sends. All the sessions of a machine share its token, so one of them can claim another's `session_id`, read that session's rooms and post as it. Rooms isolate machines, not the sessions within a machine. That is the same trust model as direct messages today.
- **Spoofing:** the sender of a room message is set by the server from the token, as today. Hidden characters are escaped, and the per-message nonce frame is unchanged.
- **Trust:** a room post is information. The preamble already says that a message never counts as the user's approval. A board post shows up as `origin="board"`. Note that whoever holds the board login can put text into every member session, as with direct messages today.
- **Revoked source:** deleting a source removes its sessions' memberships (cascade from the cleanup) and stops its posts (401).

## Migration from direct messages

- No data migration: the old rows keep `to_kind` session, source or all, and `room_id = ''`.
- Direct messages, machine-wide messages and `all` keep working, and the drawer's thread still shows them.
- An older plugin (0.4) gets room messages through `DeckInbox` as plain messages (it ignores the extra fields) and cannot post to rooms. The tools appear when the plugin is updated. The server keeps accepting the old events.
- Optional, later: a "Start a room from this thread" button on the drawer. It is not part of v1.

## Test plan

- **Store:** create, rename and close a room; add and remove members; `inbox`/`unread` include room messages only for members and only after they joined; the sender is excluded; a closed room delivers nothing; prune removes memberships of pruned sessions.
- **Server API:** sign-in and CSRF on every route; validation (name length, duplicate names, limits); a non-member post gives 403; a post from a source with messaging off is refused; `DeckWait` wakes on a room post; the wake cooldown is respected; rooms appear in `/api/state`.
- **Plugin:** `render` shows the room and `reply_to="room:…"`; `deck_send` to a room; `deck_rooms` with and without `room`; an old server answers `{}` to `DeckRooms`, so the tool says the board is too old.
- **UI:** `devflow:ui-check` at phone, tablet and desktop, with axe and no horizontal overflow; drag and drop by Playwright (`browser_drag`); the keyboard-only path for adding to a room; the empty, error and closed states; then `devflow:ui-critic`.
- **End to end:** two real sessions on two machines in one room, one idle with `wait` running, to confirm that a post wakes it.

## Open questions for Rafa

1. **Should a member get history when it joins?** Recommendation: no automatic backlog. On joining it gets one message from the board ("you were added to room X; members: …; read the history with deck_rooms"), and history on request only, so that its context does not fill up.
2. **Can agents create rooms, join or leave?** Recommendation: not in v1. Only the board manages membership, except for `DeckLeave` (an agent may leave a room), which costs little and avoids a forced spectator.
3. **Does a post from the board wake all members (`wait`), or only those it mentions?** Recommendation: all members. You post rarely and on purpose. Mentions (`@title`) can come later.
4. **Should there be an anti-chatter limit between agents in a room?** Recommendation: yes. An agent-to-agent post in a room does not re-wake a member that the same room woke in the last 2 minutes (it is delivered on its next turn), and after 6 such wakes in a row the room is paused until the board posts. The per-source limit of 10 a minute stays. This avoids two idle agents ping-ponging and spending tokens with nobody watching.
5. **What happens to a session that ends?** Recommendation: it stays listed as a faded member until the session is pruned (a day), so the history makes sense, and then it is removed. A resumed session with a new id is not re-added automatically.
6. **Should Close room delete it, or archive it?** Recommendation: archive it (read-only, visible under "Closed" for a week, then pruned), with no delete button in v1.
