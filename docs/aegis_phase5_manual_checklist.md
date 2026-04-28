# Aegis — Phase 5 Manual Verification Checklist

**Date**: _______________  
**Tester**: _______________  
**App URL**: http://localhost:8000  
**Automated script result**: ___ / 28 checks passed (verify_phase4.py)

---

## Phase 5 Scope: Polish + Hardening

Phase 5 adds: Admin settings page, hybrid search page, error handling/retry logic, token security, backup scripts, data retention, mobile-responsive audit, voice profile management, and startup/shutdown scripts.

---

## Admin Settings Page (/admin)

- [ ] **Admin page loads and displays all setting categories**  
  Navigate to /admin — are there collapsible sections for each category?  
  Expected categories: Connections, Polling, Triage, Workstream Detection, Meeting Processing, Intelligence Schedule, Notifications, Communication/Voice, Org Chart, Sentiment, Data Retention, LLM Config, System  
  **Observations**: PASS. 

- [ ] **Settings show current values from .env defaults**  
  Do polling intervals, thresholds, and schedule times display their current values?  
  **Observations**: PASS.

- [ ] **Settings can be edited and saved**  
  Change a setting (e.g., `polling_email_seconds` from 900 to 600). Does it save?  
  Refresh the page — does the new value persist?  
  **Observations**: PASS.

- [ ] **Settings take effect at runtime**  
  After changing a polling interval, does the next poll cycle use the new interval?  
  (Check logs for the updated interval)  
  **Observations**: PASS. 
- 
- [ ] **Voice profile section displays auto-generated profile**  
  Navigate to Admin → Communication/Voice. Is the auto-generated voice profile shown?  
  Does it accurately describe your writing style (tone, greetings, sign-offs, formality)?  
  **Observations**: PASS.

- [ ] **Custom voice rules can be added**  
  Add a test rule (e.g., "Never use 'Hope this helps'"). Does it save?  
  Generate a draft — is the rule respected?  
  **Observations**: PASS.
- [ ] **Voice profile can be regenerated**  
  Click "Regenerate profile" — does it re-analyze sent emails and update?  
  **Observations**: PASS.

- [ ] **Notification toggles work**  
  Toggle macOS notifications off → verify no notification fires at next briefing time.  
  Toggle email-to-self on → verify briefing arrives in your inbox.  
  **Observations**: PASS.

- [ ] **HTMX auto-save works (no page reload needed)**  
  Change a setting — does it save without a full page reload?  
  Is there a visual confirmation (checkmark, flash)?  
  **Observations**: PASS. 

---

## Search Page (/search)

- [ ] **Search page loads with input field**  
  Navigate to /search — is there a search input and filter options?  
  **Observations**: PASS. 

- [ ] **Keyword search returns results across all content types**  
  Search for a known term (e.g., a person's name, a project name).  
  Do results include meetings, emails, and chat messages?  
  **Observations**: PASS. 

- [ ] **Semantic search returns contextually relevant results**  
  Search for a concept (e.g., "budget concerns" or "migration timeline").  
  Are results semantically relevant, not just keyword matches?  
  **Observations**: PASS.

- [ ] **Results show source type badges**  
  Does each result show whether it's from a meeting, email, or chat?  
  **Observations**: PASS.

- [ ] **Results are clickable**  
  Click a result — does it navigate to the source (meeting detail, email detail, etc.)?  
  **Observations**: PASS.
  TODO: When cicking into an Email, the view should include a collapsed section with the entire email body for reveiw.

- [ ] **Search is fast (<2 seconds)**  
  Does the search return results quickly? No spinning for 5+ seconds?  
  **Observations**: PASS.

---

## Error Handling & Retry Logic

- [ ] **Screenpipe restart recovery**  
  Stop Screenpipe, wait 1 minute, restart it.  
  Does Aegis detect the outage and resume capture? (Check system_health status)  
  **Observations**: SKIP. Screenpipe not implemented yet.

- [ ] **OAuth token refresh**  
  Delete `~/.aegis/msal_token_cache.json` while the server is running.  
  Does the next Graph API call trigger re-authentication gracefully?  
  Does it log a warning (not crash)?  
  **Observations**: SKIP.

- [ ] **Graph API rate limit handling**  
  (Hard to test manually — verify code exists)  
  Check `aegis/ingestion/graph_client.py` — is there retry logic with `Retry-After` header handling?  
  **Observations**: PASS.

- [ ] **Database connection recovery**  
  Restart the Docker PostgreSQL container while Aegis is running.  
  Does Aegis reconnect automatically on the next poll cycle?  
  **Observations**: SKIP.

- [ ] **LLM API overload recovery**  
  When Anthropic returns 529 (Overloaded), does the system retry or log and continue?  
  (Check logs for any 529 errors and how they were handled)  
  **Observations**: SKIP.

- [ ] **Exponential backoff with jitter**  
  Are retries spaced with increasing delay? (Check code or logs)  
  **Observations**: SKIP.

---

## Token Security

- [ ] **MSAL token cache has restricted permissions**  
  Run: `ls -la ~/.aegis/msal_token_cache.json`  
  Should show `-rw-------` (chmod 600). No group/world read access.  
  **Observations**: result: "-rw-------@ 1 rickydelemos  staff  8943 Apr 27 20:19 /Users/rickydelemos/.aegis/msal_token_cache.json"

- [ ] **No secrets in logs**  
  Search server logs for API keys or tokens:  
  `grep -i "sk-ant\|sk-proj\|Bearer\|access_token" /path/to/logs`  
  Should return zero matches.  
  **Observations**: "grep: /path/to/logs: No such file or directory"

- [ ] **.env is gitignored**  
  Run: `git status` — `.env` should NOT appear in tracked or untracked files.  
  **Observations**: result "On branch main Your branch is up to date with 'origin/main'."
---

## Database Backup & Retention

- [ ] **Backup script creates valid pg_dump**  
  Run the backup script (if implemented).  
  Verify the output file is a valid PostgreSQL dump:  
  `pg_restore --list /path/to/backup.dump | head`  
  **Observations**: FAIL. Not implemented yet.

- [ ] **Backup rotation works (30-day)**  
  Are old backups being cleaned up? Check the backup directory for files older than 30 days.  
  **Observations**: FAIL. Not implemented yet.

- [ ] **Data retention tiers work**  
  Hot (90d): all data fully accessible.  
  Warm (365d): older data still searchable but not in active pipeline.  
  Verify: are items older than 90 days excluded from dashboard cache but still in search?  
  **Observations**: SKIP.

---

## Startup & Shutdown

- [ ] **LaunchAgent auto-starts Aegis on macOS boot**  
  Restart your Mac (or log out/in).  
  Does Aegis start automatically? (Check: `curl http://localhost:8000`)  
  **Observations**: SKIP.

- [ ] **`aegis start` command works**  
  Run `aegis start` — does the server start?  
  **Observations**: FAIL. zsh: command not found: aegis

- [ ] **`aegis stop` command works**  
  Run `aegis stop` — does the server stop gracefully?  
  Are all background tasks cancelled cleanly? (No error in logs)  
  **Observations**: FAIL. zsh: command not found: aegis

- [ ] **Crash recovery on startup**  
  Before starting, manually set a meeting's processing_status to 'processing':  
  `psql -h localhost -p 5434 -U postgres -d aegis -c "UPDATE meetings SET processing_status = 'processing' WHERE id = (SELECT id FROM meetings LIMIT 1);"`  
  Start Aegis — does the log show "Reset X stuck processing items back to pending"?  
  **Observations**: PASS.

---

## Logging

- [ ] **Log rotation is configured**  
  Are log files being rotated? Check for numbered/dated log files.  
  **Observations**: FAIL. Could not find log files.

- [ ] **Log level can be changed via admin**  
  Set LOG_LEVEL to DEBUG in admin settings.  
  Are debug-level messages now appearing? (e.g., entity resolution details)  
  **Observations**: FAIL. LOG_LEVEL not avialable in admin settings.

- [ ] **No PII in logs**  
  Review recent log output — are there raw email bodies, message content, or personal info?  
  Only metadata (IDs, counts, status) should appear.  
  **Observations**: FAIL. Could not find log files.

---

## Mobile Responsive Audit

Test at 375px viewport width (iPhone SE) for each page:

- [ ] **Dashboard / Command Center**: All 6 zones stack vertically, readable  
  **Observations**: PASS.

- [ ] **Meetings list**: Table scrolls horizontally or switches to card layout  
  **Observations**: PASS.

- [ ] **Meeting detail**: Transcript and prep brief display correctly  
  **Observations**: PASS.

- [ ] **Emails list**: Readable, filter dropdowns accessible  
  **Observations**: PASS.

- [ ] **Asks page**: Tabs and table work at narrow width  
  **Observations**: PASS.

- [ ] **People directory**: Table or card layout, search works  
  **Observations**: PASS

- [ ] **Readiness page**: Score table readable with expand/collapse  
  **Observations**: PASS.

- [ ] **Workstream detail**: Timeline items and sidebar stack  
  **Observations**: PASS.

- [ ] **RAG Chat (/ask)**: Input field and message bubbles fit  
  **Observations**: PASS.

- [ ] **Respond page**: Draft form and preview are usable  
  **Observations**: PASS.

- [ ] **Sidebar navigation**: Collapses to hamburger menu on mobile  
  **Observations**: PASS.

---

## Carryover Items from Phase 3+4 Reviews

These items were identified in manual reviews but deferred to Phase 5:

- [ ] **Teams data visible in UI**  
  Is there a way to browse Teams channels and messages in the app?  
  Do Teams-originated asks show source badges on /asks?  
  **Observations**: PASS.

- [ ] **Chat messages clickable from workstream timeline**  
  Can chat messages in workstream timelines be clicked to view source?  
  **Observations**: FAIL. Clicking on a chat takes the user to /asks not the source message.

- [ ] **Department management (create/edit/delete)**  
  Can departments be manually created, renamed, merged, or deleted?  
  Can people be reassigned between departments?  
  **Observations**: PASS.

- [ ] **Unassigned items queue visible**  
  Is there a view showing items not assigned to any workstream?  
  Can items be manually assigned from this view?  
  **Observations**: FAIL. No view for unassigned items.

- [ ] **Internal vs external ask filtering**  
  Can asks be filtered to show only internal (within org) or external asks?  
  **Observations**: FAIL. No flag or filter for external asks.

- [ ] **Dynamic breadcrumbs**  
  When navigating from Asks → Email detail → back, does the breadcrumb reflect the path?  
  **Observations**: PASS. 

- [ ] **Close-out actions/asks from readiness page**  
  Can items be marked complete directly from the readiness detail view?  
  **Observations**: PASS.

- [ ] **LLM suggestions for people (from email signatures)**  
  Do needs-review people cards show LLM-suggested titles and departments?  
  Are suggestions extracted from email signatures?  
  **Observations**: FAIL. Still see "No LLM suggestion available yet."

- [ ] **Nudge draft auto-generation working**  
  Are nudge drafts being auto-generated for stale action items and asks?  
  Are they professional and reference the correct items?  
  **Observations**: PASS.

- [ ] **Workstream sentiment dots and trend arrows on dashboard cards**  
  Do workstream cards on the command center show sentiment indicators?  
  **Observations**: FAIL. No trend arrows or sentiment dots.

- [ ] **Respond button carries source context**  
  When clicking "Respond" on a decision/ask in the dashboard, does /respond pre-fill the source item?  
  **Observations**: PASS.

---

**Ready for production?**: NO  
