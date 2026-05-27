# Command Center — "Requires Attention" Logic

This document explains how Aegis determines what appears in the three tabs of the Requires Attention section on the Command Center (dashboard).

---

## Overview

The Requires Attention section has three tabs: **Decisions**, **Awaiting**, and **Stale**. Each tab is populated by a separate query that runs on page load (or from a cached result with a 15-minute TTL). When you change an item's status (complete an action, resolve a decision, etc.), the cache is invalidated and the next page load reflects the change.

---

## Decisions Tab

**What it shows:** Unresolved decisions extracted from recent meetings and emails.

**Definition:** A "decision" is a specific choice or resolution identified by the LLM during extraction from a meeting transcript or email. Examples: "Approved the Q3 budget at $280K", "Decided to postpone the site visit until May".
**USER COMMENT** i see that ASKs have a "decision" tag but do actions have a "decision" tag too? can we add that tag to the items /actions page so that we can trace decision back to all their origins. Are all of these decisions that have been directed to me specifically?

**Query logic:**
- Source table: `decisions`
- Filters: `status = 'open'` (or NULL) AND `datetime >= 30 days ago`
- Sorted by: most recent first
- Limit: 20 items

**How items leave this tab:**
- Click the "Resolve" button on the dashboard — sets `status = 'resolved'`
- Items older than 30 days automatically age out of the query window

**Where decisions come from:** The extraction pipeline (`meeting_extractor.py`, `email_extractor.py`) identifies decisions in meeting transcripts and substantive emails. Each decision is stored with the source meeting or email ID so it can be traced back.
---

## Awaiting Tab

**What it shows:** Open asks (requests from others) that haven't been addressed yet.

**Definition:** An "ask" is a specific request extracted from an email or Teams chat message, with a requester and a target. Examples: "Please send the updated trial protocol by Friday" (deliverable), "Can you approve the vendor contract?" (approval), "What's the status of the enrollment?" (question).
**USER COMMENT** to confirm, "awaiting" is only for ASKs that are directed to other team members, not me? If so, i would like this updated to reflect only ASKs that I have made to other team members.

**Query logic:**
- Source tables: `email_asks` + `chat_asks` (combined)
- Filters: `status = 'open'` only (excludes in_progress, completed, stale)
- Sorted by: most recently created first
- Limit: 20 items

**How items leave this tab:**
- Change status to "completed" on the /asks page (click the status badge)
- Change status to "in_progress" (moves it out of "open" filter)
- The system can auto-close asks when it detects a reply in the email thread (`thread_analyzer.py`)

**Ask types:** deliverable, decision, follow_up, question, approval, review, info_request

**Ask urgency levels:** high, medium, low (extracted by the LLM based on language cues and deadlines)

---

## Stale Tab

**What it shows:** Action items that have been open for too long without being completed.

**Definition:** An "action item" is a task or follow-up identified during meeting extraction. Examples: "Schedule a follow-up with the Novavax team", "Send the updated site evaluation report to Dr. Smith". An action item becomes "stale" when it has been in `open` or `in_progress` status for longer than the configured threshold.

**Query logic:**
- Source table: `action_items`
- Filters: `status IN ('open', 'in_progress')` AND `created <= (now - stale_action_item_days)`
- Sorted by: oldest first (most overdue at top)
- Limit: 20 items

**Stale threshold:** Configurable in Admin Settings under "Stale Thresholds":
- `stale_action_item_days`: default **7 days** — action items older than this appear here
- `stale_ask_hours`: default **72 hours** — used for nudge draft generation (not the Awaiting tab)

**How items leave this tab:**
- Mark as "completed" on the /actions page
- The item is automatically removed from the tab when status changes to completed

---

## Caching Behavior

All three tabs are backed by the `dashboard_cache` table with these keys:
- `pending_decisions`
- `awaiting_response`
- `stale_items`

Cache TTL is **15 minutes** (configurable via `dashboard_cache_ttl_seconds` in Admin Settings).

The cache is **invalidated immediately** when:
- An action item status is changed (clears `stale_items` + `drafts_pending`)
- An ask status is changed (clears `awaiting_response` + `stale_items` + `drafts_pending`)
- A decision is resolved (clears `pending_decisions`)
- A draft is sent or discarded (clears `drafts_pending`)

The cache is **refreshed on a schedule** every 15 minutes by the `refresh_dashboard_cache` background job.

---

## Related Admin Settings

| Setting | Default | Effect |
|---------|---------|--------|
| `stale_action_item_days` | 7 | Days before an action item appears in the Stale tab |
| `stale_ask_hours` | 72 | Hours before an ask triggers a nudge draft |
| `stale_nudge_threshold_days` | 3 | Days before auto-generating a nudge draft |
| `dashboard_cache_ttl_seconds` | 900 | How long cached dashboard data is considered fresh |

 **USER COMMENT** the requires attention section seems to be missing the mark. I need to be able to see things that are expected of me - decisions, ask, action items that have been directed to me by others AND things that others owe me, that i have asked of them. 