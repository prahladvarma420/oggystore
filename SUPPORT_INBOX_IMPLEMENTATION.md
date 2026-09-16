# Oggy Store Support Inbox Implementation

## Root cause

The original `tickets` table represented both a ticket and its entire conversation. It had only one `message` field and one `admin_reply` field. Every call to `create_ticket()` inserted a new ticket row, while `reply_ticket()` overwrote the single `admin_reply` value. There was no child message model, no user “reply to existing ticket” flow, no unread state, and no conversation-history API. Consequently, true multi-message conversations could not exist and repeated customer contact naturally became separate ticket rows.

## Existing architecture preserved

- `tickets.id` remains the canonical ticket/conversation ID.
- Existing `tickets` columns remain available for Telegram screens and old backups.
- Existing Flask authentication, session revocation, CSRF protection, rate limits, and admin permissions remain active.
- Existing `/api/tickets`, `/reply`, and `/close` route naming is preserved.
- Existing Telegram delivery remains the customer-facing notification channel.
- No existing ticket data is deleted.

## Implementation plan completed

1. Add the smallest normalized child table for messages.
2. Backfill legacy user messages and admin replies idempotently.
3. Route user and admin replies through append-only conversation functions.
4. Add summary and history APIs without loading all messages into the inbox.
5. Replace the table/modal UI with a responsive two-panel support inbox.
6. Make closure transactional and idempotent, with one server-generated resolution message.
7. Validate continuity, ownership, pagination, closure, and legacy migration.

## Files changed

- `database/db.py`
- `database/pgcompat.py`
- `handlers/user_handlers.py`
- `admin/admin_handlers.py`
- `web_admin/app.py`
- `web_admin/static/js/app.js`
- `web_admin/static/css/style.css`
- `SUPPORT_INBOX_IMPLEMENTATION.md`

## Database changes

Added `ticket_messages`:

- `id` — message ID
- `ticket_id` — existing `tickets.id`
- `sender_type` — `user`, `admin`, or `system`
- `sender_id` — Telegram/admin identifier when available
- `message` — encrypted through the existing encryption helper
- `created` — message timestamp
- `read_by_admin` — server-side unread state
- `legacy_slot` — idempotent legacy/resolution marker
- unique constraint on `(ticket_id, legacy_slot)`

Added indexes for `(ticket_id, id)`, unread lookups, and ticket update ordering. PostgreSQL ID translation now recognizes `ticket_messages`.

On initialization, legacy `tickets.message` and `tickets.admin_reply` values are copied into the message table exactly once. The source columns remain unchanged.

## API changes

### `GET /api/tickets`

Returns conversation summaries only: customer, subject, status, latest message/sender/time, message count, and unread count. Supports `status`, `search`, `page`, and `per_page`.

### `GET /api/tickets/<ticket_id>`

Returns the ticket header and chronological messages. Supports `limit` and `before_id` for loading older messages. Opening the ticket marks user messages read on the server.

### `POST /api/tickets/<ticket_id>/reply`

Appends an admin message to the existing ticket. It never inserts a ticket. Empty, missing, and closed tickets are rejected. The new message is returned for immediate UI insertion and sent through Telegram.

### `POST /api/tickets/<ticket_id>/close`

Transitions the existing ticket to `closed`. On the first transition only, the backend appends and sends:

> Your support has been resolved. If you have any query contact admin @prahladvarma

Repeated close requests return success but do not append or send the message again.

## User-side support behavior

“My Tickets” now provides a reply action for active tickets. The selected ticket ID is stored in the existing user state mechanism. The next message calls `add_user_ticket_reply(ticket_id, user_id, message)`, which verifies ownership and rejects closed tickets. It appends to the same conversation and notifies the admin. It never calls `create_ticket()`.

Closed tickets remain visible. A customer attempting to reply is told to create a new support request, matching the existing business model rather than silently reopening or duplicating the closed conversation.

## Web Admin UI

- Two-panel desktop inbox: conversation list and selected chat.
- Mobile list-to-chat navigation with a back control.
- Customer initials, ticket ID, subject, preview, timestamp, count, unread count, and status.
- All/Open/Answered/Closed/Unread filters.
- Search by ticket ID, user ID, name, username, subject, or latest-message preview.
- Chronological left/right message bubbles with sender and timestamp.
- Paginated “Load older messages” for long histories.
- Sticky composer with Enter-to-send and Shift+Enter for a newline.
- Empty-message prevention, disabled/sending state, clear errors, and immediate message insertion.
- Close confirmation explaining the automatic customer notification.
- Closed conversations remain readable and the composer becomes read-only.
- Existing dark/purple Oggy theme and global navigation are preserved.

## Security

- Every web support route remains protected by the existing `login_required` guard.
- Ticket IDs are resolved and validated server-side.
- User replies verify `tickets.user_id` ownership.
- Closed tickets reject normal replies in backend code.
- Message content remains encrypted at rest when `DATA_ENCRYPTION_KEY` is configured.
- Telegram HTML content from admin replies is escaped before delivery.
- Resolution text is generated server-side.
- Unread state is updated server-side.

## Performance

- Inbox queries conversation summaries, not histories.
- Latest-message/count/unread metadata uses one aggregate query, avoiding N+1 reads.
- Message history loads at most 100 entries per request and pages backward by message ID.
- Search operates on a bounded summary set because encrypted message/subject text cannot be safely indexed as plaintext.
- The conversation moves to the top using its latest message timestamp.

## Testing performed

Passed:

- Python compilation for the entire repository.
- JavaScript syntax validation.
- Existing `verify_patch.py` checks.
- New ticket creates one conversation with one message.
- Multiple user/admin replies remain on the same ticket ID.
- Three customers remain three separate conversations.
- 207-message conversation paginates in correct chronological order.
- Admin unread counts and server-side mark-as-read.
- Cross-user ticket reply rejection.
- Closed ticket rejects user/admin replies.
- First close appends one resolution message.
- Repeated close does not duplicate the resolution message.
- Legacy ticket migration is idempotent across repeated startup runs.
- Desktop and 390 px mobile visual inspection; no chat-content overlap after responsive correction.

The sandbox lacks Flask and Telegram packages, so live HTTP/Telegram integration could not be launched here. Domain/database behavior was executed against a temporary real SQLite database. Run staging checks with installed requirements and a test Telegram bot before production deployment.

## Remaining issues and recommendations

- The inbox search is bounded to the latest 500 summaries because message text is encrypted. At very large scale, add a privacy-reviewed search index rather than storing plaintext.
- There is no WebSocket/SSE infrastructure. The UI updates immediately after local actions; external customer replies appear on refresh. A light polling interval can be added later if operational load permits.
- Support attachments did not exist in the original ticket system, so no fake attachment model was introduced.
- Add Flask test-client and Telegram integration tests in CI once dependencies are available.
