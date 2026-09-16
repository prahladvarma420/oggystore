# Ticket System Upgrade — Implementation Notes (Round 2)

## Root cause of the InlineKeyboardButton crash

`buttons()` in `handlers/user_handlers.py` spans ~800 lines and contained
function-local imports of `InlineKeyboardMarkup` / `InlineKeyboardButton`
inside three later branches (gift codes ~L820, warranty ~L887, orders
~L1019–1020). Python treats any name imported anywhere inside a function as
function-local for the ENTIRE function scope, so the module-level imports at
the top of the file were shadowed. When `my_tickets` executed at ~L1203 before
any of those branches ran, `InlineKeyboardButton` was an unbound local →
UnboundLocalError. The same latent pattern existed in `message_handler()`
(search branch, ~L1626–1627).

Fix: removed those four redundant function-local imports. The module-level
project-standard imports now apply consistently:
`from telegram import InlineKeyboardMarkup` and
`from utils.button_styles import AutoStyledButton as InlineKeyboardButton`
(AutoStyledButton subclasses telegram.InlineKeyboardButton — the project-wide
standard also used by menus.py and admin_handlers.py). Local imports inside
OTHER self-contained functions (join-gate helper, order detail, nested search
fallback) are function-scoped, execute before use, and were left untouched.

## Status model (no rewrite — mapped onto existing statuses)

- `open`     = OPEN — customer created/replied before any admin response
- `answered` = UNRESOLVED — an admin/system message exists, ticket not closed
- `closed`   = CLOSED/RESOLVED

The web "Unresolved" filter uses a derived SQL condition (EXISTS admin/system
message AND status <> 'closed'), not UI text, so it is robust for legacy rows.
A customer follow-up no longer resets `answered` back to `open`
(append_ticket_message user branch now only bumps `updated`).

## Customer flows

- Ticket-created confirmations now carry [💬 Reply Ticket] [📂 My Tickets]
  [✅ Close Ticket] — a customer can add information immediately without
  creating a new ticket.
- `support_reply_<id>` appends to the same ticket (ownership + open-status
  verified server-side).
- `support_view_<id>` renders the ticket detail with complete chronological
  history (newest 100 messages, Telegram-length-safe truncation with an
  omitted-count note).
- `support_close_<id>` closes only the caller's own ticket, idempotently,
  with confirmation + resolution text; admin is notified.
- My Tickets shows ALL tickets (open/unresolved/closed), paginated 5/page via
  `my_tickets_page_<n>`.
- Every admin reply notification (Telegram `/replyticket`, Telegram Ticket
  Manager state reply, and Web Admin reply) now attaches the CUSTOMER buttons
  directly under the reply message.

## Admin reminder (24h)

`tasks/ticket_reminders.py` runs on the existing python-telegram-bot JobQueue
(hourly tick, first run 3 min after boot — no new scheduler). A reminder fires
when a ticket has an admin/system reply, is not closed, and both last activity
and last reminder are ≥ 24h old. `tickets.last_reminder_at` persists dedupe
across restarts; the mark happens only after a successful Telegram send.
Reminder buttons reuse the existing `admin_ticket_reply_<id>` /
`admin_ticket_close_<id>` callbacks (guarded by is_admin).

## Web admin

- Support inbox filters: All / Open / Answered / Unresolved / Closed / Unread.
- Web replies save to the same shared history and deliver the Telegram message
  with customer Reply/Close buttons attached.

## Database migration (safe, additive)

- `tickets.last_reminder_at TEXT` via `_safe_add_column` (idempotent,
  no data touched).
- No drops, no deletes, no resets. All prior ticket data and the round-1
  `ticket_messages` backfill remain intact.

## Security

- All customer callbacks re-verify `ticket.user_id == current user`.
- Closed tickets reject replies in `add_user_ticket_reply` and
  `reply_ticket` (both Telegram and web paths).
- Admin callbacks pass through the existing `is_admin` guard.
- Message bodies remain encrypted at rest via the existing crypto helper.
