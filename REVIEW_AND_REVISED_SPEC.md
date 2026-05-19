# CommitBot — Spec Review & Revised MVP

This document does two things:

1. **Flags every place the original spec collides with reality** (Slack API limits, contradictions, ambiguous state machines).
2. **Defines the corrected MVP** that the scaffolded code in this repo actually builds.

The spec is genuinely strong product thinking. The fixes below are not "this is bad" — they're "this won't survive contact with the Slack platform" or "this rule conflicts with that rule three sections down."

---

## Part 1 — Flaws & Fixes

### 🟥 Critical (will not work as written)

**F1. "Lightning bolt in the Slack compose toolbar" (Method A) does not exist for third-party apps.**
Slack's compose toolbar is closed. Third-party apps cannot inject custom icons next to the send button. The lightning bolt you see in Slack *is* the shortcut launcher, and clicking it shows shortcuts — it does not let you "attach hidden metadata to the message you're about to send."

- **Fix:** Replace Method A with a **slash command** `/commit <text>` that both posts the message in the current channel/DM *and* logs the commitment in one step. This preserves the intent ("one motion, sends + logs") and is the closest real Slack pattern. The modal-based **global shortcut** ("Log commitment" via the shortcut launcher) becomes Method A's secondary form, useful for commitments that don't need a public message at all.

**F2. "Commitment metadata attached invisibly — no notation in sent text" is not possible.**
There is no way for a third-party app to attach hidden metadata to a *user's own* outgoing message. You can only correlate a message you logged after-the-fact using `(channel_id, message_ts)`.

- **Fix:** The slash command posts the message *as the user* (via `chat.postMessage` with the user token, or via `response_url`), and the bot stores the resulting `(channel, ts)` as the link. No hidden metadata; the message itself is the anchor.

**F3. Bots cannot edit a user's own messages.**
The spec says "edits in dashboard update Slack message via bot." A Slack bot can only edit messages it itself posted. If the commitment was logged from a user's message (Method B notation, Method C right-click), the bot **cannot** rewrite that message.

- **Fix:** Two-way edit sync is only possible for commitments posted by the bot (slash command and global-shortcut variants). For messages owned by users (notation / message shortcut), edits in the dashboard update the commitment record only; the original Slack message stays as-is, and the bot's threaded confirmation reply is updated instead. The card UI labels this clearly.

**F4. "A private Slack channel containing only the user and the bot. No other person can enter."**
Slack channels can always be joined by workspace admins; you cannot enforce "no other person can enter" via the API. Also, the channel doesn't have "tabs" — that's not a Slack primitive.

- **Fix:** Use **Slack App Home** (the bot's Home tab in the user's DM with the app). This is the standard Slack pattern for per-user bot UIs. "Tab 1 / Tab 2" become a tab switcher built with Block Kit inside App Home — concretely, a radio button or pair of buttons that re-renders the home view. The "personal channel" mental model from the spec maps cleanly onto App Home.

**F5. Voice profile is "stored locally on device, not on server" — but Meeting AI (post-MVP) sends audio to Gemini for transcription *and* claims to identify the user's voice.**
You cannot do speaker identification in the cloud without uploading the voice profile (or computing embeddings client-side and uploading those).

- **Fix:** (Post-MVP only — not in this MVP.) Compute a voice embedding on-device, store the embedding on the server (it's a vector, not the raw audio), and use it for cloud-side diarization. Raw audio still never leaves the device. Document this explicitly in the privacy policy.

**F6. "Personal DM-only mode: zero admin permissions required."**
Even DM-only operation requires a Slack admin to install the app to the workspace (Slack does not let individual users install third-party apps without org approval on most plans).

- **Fix:** Reword to "minimal admin permissions" and list the actual scopes needed (`im:history`, `chat:write`, `commands`, `app_mentions:read`). Don't promise "zero" — it's false and will burn trust on day one.

### 🟧 Significant (the rules contradict each other)

**F7. Two "auto-delete after X days" policies.**
The "Additional Behaviours" section says auto-delete is a per-user toggle with a user-set X. The "Data Retention Policy" section says completed commitments are auto-deleted after a user-configured window, default 30 days. These are the same feature with slightly different language — pick one.

- **Fix:** Single setting, `auto_delete_completed_after_days`, per user, default 30, with `0` meaning "never auto-delete."

**F8. On-Hold has three resume paths with no precedence rule.**
Resume date, linked Tab 2 fulfillment, and manual resume can all fire. What if a user manually resumes before the linked dependency is fulfilled? What if the resume date arrives but the dependency is still blocking?

- **Fix:** Defined precedence: manual resume wins. If a linked dependency is still unfulfilled, the card returns to Active *with* a "still blocked by X" badge — user explicitly chose to override. Resume date and dependency fulfillment trigger Active automatically only if neither has been manually overridden.

**F9. "Last-write-wins by timestamp" with millisecond timestamps will collide.**
Two devices syncing within the same millisecond is unlikely but possible, and clock skew across servers makes raw timestamp comparison unsafe.

- **Fix:** Use a `version` integer that increments on every write, plus the server's monotonic UTC timestamp. Conflict resolution: higher `version` wins; ties broken by lexicographic `(timestamp, writer_id)`. Stored in `Commitment.version` and `Commitment.last_writer`.

**F10. The 🔖 reaction as a "team signal" is a privacy leak.**
The user may have logged a personal commitment they don't want their team to know about. Default-on visibility is the wrong default.

- **Fix:** Make the reaction **opt-in per user**, default off. The threaded "✅ Logged" reply is also optional and can be set to ephemeral-only (visible to the user only).

**F11. Custom notation delimiters `?...?` will misfire constantly.**
"Did you ask @rahul?" is a normal Slack sentence and would be falsely captured.

- **Fix:** Disallow `?` as a delimiter. Require delimiters that are unusual in prose (`[[ ]]`, `>> <<`, or a single-token prefix like `!commit:`). Validate at notation-creation time. Default suggested notation: `[[commit @person]]`.

**F12. Workspace silos vs "set channels (opt-in per channel, requires workspace admin approval)" vs "all configuration is per-user."**
Channel opt-in inherently requires the bot to be invited to the channel, which is a workspace-level action, not a per-user one. The spec mixes scopes.

- **Fix:** Clear separation:
  - **Workspace-scoped (admin):** which channels the bot is in, OAuth installation.
  - **User-scoped:** notations, priority levels, digest preferences, voice profile, pause.
  - Documented explicitly in the dashboard's Settings page.

**F13. "Message-ID level dedup" is under-specified.**
Slack message identity is `(workspace_id, channel_id, message_ts)`, not just `message_ts`. A bare `ts` can repeat across channels.

- **Fix:** Dedup key is the tuple. Stored as a `UNIQUE(workspace_id, channel_id, slack_message_ts)` constraint.

### 🟨 Minor (clarifications, UX, scope)

**F14.** Daily digest "per priority level" produces fragmented digests. Default to one digest per user per day, with an advanced toggle to split by priority.

**F15.** "Send Slack DM notification for each new card" (Meeting AI) will spam users after a 1-hour meeting yielding 8 commitments. Batch into one summary DM with all extractions.

**F16.** No mention of dashboard authentication. Use **Sign in with Slack** (OAuth); the Slack `user_id` is the identity primary key.

**F17.** CSV export is fine for MVP, but add a JSON export for completeness — CSV flattens nested data (recipients, edit history) badly.

**F18.** "MVP Feature List" footer says "No Meeting AI, Jira, Zendesk, or voice capture" but the top-of-document "Platform" row still includes "macOS desktop app." Drop the macOS app from MVP scope explicitly — without voice capture there's nothing the desktop app does that the web dashboard doesn't.

**F19.** The "Race condition handling" section talks about macOS notification vs Slack DM, but MVP has no macOS app — so this whole subsection isn't relevant until voice capture lands.

**F20.** Onboarding: "one-time onboarding message sent to user when bot first joins a channel" — should be sent once *per user per workspace*, not once per channel-join (which would spam users in active workspaces).

---

## Part 2 — Revised MVP

### Scope (what this codebase builds)

- **Slack bot only** — no desktop app, no voice, no Meeting AI, no Jira/Zendesk.
- **Three capture methods**, redesigned to fit Slack reality:
  - **A. Slash command** `/commit <text>` — posts the message and logs it in one motion.
  - **B. Custom notation** — user defines up to 5 patterns (default `[[commit @person]]`), bot detects via Events API.
  - **C. Message shortcut** — right-click any message → "Mark as commitment." Own message → Tab 1; someone else's → Tab 2.
- **App Home** as the per-user surface (replaces the "personal commitments channel" idea).
- **Pinging system** with user-defined priority levels, escalation, snooze, daily digest, global pause.
- **Web dashboard** for configuration and full commitment management.

### Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Asked for. |
| Web framework | FastAPI | Async, plays well with both Slack webhooks and dashboard APIs. |
| Slack SDK | `slack-bolt` (async) | Official, handles signature verification, retries, lazy listeners. |
| DB | SQLite (SQLAlchemy 2.0) | Zero setup for MVP; trivial swap to Postgres later. |
| Migrations | Alembic | Lightweight, plays with SQLAlchemy. |
| Scheduler | APScheduler (in-process) | Simple; runs pings without a separate worker process. |
| Dashboard UI | Jinja2 templates + HTMX | No JS build step; dashboard is fast to iterate on. |
| Auth | Sign in with Slack (OAuth) | Same identity as the bot. |
| Hosting (later) | Single-process container | One `uvicorn` for both web + bot + scheduler. |

### Core entities (database)

`User`, `Workspace`, `PriorityLevel`, `Notation`, `Commitment`, `CommitmentRecipient`, `CommitmentEdit`, `Reassignment`, `Ping`. Each table mirrors a real responsibility in the spec; nothing speculative.

### What's deferred to Phase 2

- Real Slack OAuth flow (scaffolded in code but the install handshake is stubbed).
- Reassignment full state machine — schema and stub handlers exist; 24-hour timer logic is a TODO.
- Daily digest delivery — scheduler hook exists; per-user digest assembly is a TODO.
- Dashboard polish — current UI is functional, not styled. Tailwind/Pico can be layered later.
- Tests — one smoke test covering app boot + model creation. Real test coverage comes after the Slack handlers are validated against a real workspace.

### How to run

```bash
cp .env.example .env       # fill in Slack credentials when ready
pip install -r requirements.txt
python -m app.init_db      # create SQLite tables + seed default priority levels
uvicorn app.main:app --reload --port 8000
```

Then:

- Dashboard: `http://localhost:8000/`
- Slack events webhook: `http://<your-tunnel>/slack/events`
- Slash command: `http://<your-tunnel>/slack/events` (Bolt routes them together)

Use `ngrok http 8000` (or Cloudflare tunnel) to expose the local server to Slack during dev.
