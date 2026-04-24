# Aegis — Full System Manual Verification Checklist

**Date**: _______________  
**Tester**: _______________  
**App URL**: http://localhost:8000  
**Automated script result**: ___ / 42 checks passed  
**System uptime since last restart**: _______________  

---

## Part A: Phase 3 Checks

### Email Noise Filter Accuracy

- [ ] **Automated emails correctly classified**  
  Spot-checked 5 "automated" emails — are they actually automated?  
  **Observations**: 

- [ ] **Human emails correctly classified**  
  Spot-checked 5 "human" emails — are they from real people?  
  **Observations**: 

- [ ] **Misclassifications**  
  Any real emails marked automated, or newsletters marked human?  
  **Observations**: 

---

### Email Triage Quality

- [ ] **Substantive emails are truly substantive**  
  Read 3 "substantive" emails — decisions, asks, or deliverables present?  
  **Observations**: 

- [ ] **Contextual emails are truly low-value**  
  Read 3 "contextual" emails — acknowledgments or low-signal?  
  **Observations**: 

- [ ] **Nothing important missed**  
  Any substantive emails wrongly triaged as noise?  
  **Observations**: 

---

### Ask Directionality

- [ ] **"Directed at you" is accurate**  
  /asks page — are these things people actually asked YOU to do?  
  **Observations**: 

- [ ] **"You asked" is accurate**  
  Are these things YOU asked others to do?  
  **Observations**: 

- [ ] **Requester/target names correct**  
  Right people shown as requester vs target?  
  **Observations**: 

---

### Workstream Quality

- [ ] **Auto-detected workstreams match real initiatives**  
  /workstreams — do you recognize these?  
  List the auto-detected workstreams:  
  1. _______________  
  2. _______________  
  3. _______________  
  4. _______________  
  5. _______________  
  **Observations**: 

- [ ] **Items within workstreams are coherent**  
  Click the largest workstream — do items belong together?  
  **Observations**: 

- [ ] **No cross-department contamination**  
  Items from unrelated departments grouped together?  
  **Observations**: 

- [ ] **Unassigned queue reasonable**  
  Items that clearly belong to a workstream but weren't assigned?  
  **Observations**: 

---

### Org Chart & People

- [ ] **Needs-review queue accurate**  
  /people — are LLM-suggested titles and departments correct?  
  **Observations**: 

- [ ] **Approve/correct flow works**  
  Approved or corrected 3-5 people — saves correctly?  
  **Observations**: 

- [ ] **Org chart reflects reality**  
  /org — departments and managers plausible?  
  **Observations**: 

---

### Teams Data

- [ ] **Teams match your actual setup**  
  Team and channel names look right?  
  **Observations**: 

- [ ] **Teams asks appear alongside email asks**  
  /asks — both email and Teams sources visible?  
  **Observations**: 

---

## Part B: Phase 4 Checks

### Briefings

- [ ] **Morning briefing on command center**  
  Is it the default view when you open the app?  
  **Observations**: 

- [ ] **Meetings listed with suggested topics**  
  2-3 topics per meeting? Are they relevant?  
  **Observations**: 

- [ ] **Topics reference real open items**  
  Do topics mention actual pending items with the right attendees?  
  **Observations**: 

- [ ] **"Requires your action" section accurate**  
  Real pending decisions and asks directed at you?  
  **Observations**: 

- [ ] **Overnight activity accurate**  
  Reflects emails/chats that actually arrived?  
  **Observations**: 

- [ ] **Workstream health accurate**  
  Status and sentiment match your perception?  
  **Observations**: 

- [ ] **Monday brief objectives** *(skip if not Monday)*  
  LLM-identified objectives make strategic sense?  
  **Observations**: 

---

### Meeting Prep

- [ ] **Prep brief opens from meeting card**  
  Click a meeting — prep brief appears?  
  **Observations**: 

- [ ] **Attendees with interaction context**  
  Shows when you last spoke and what's open?  
  **Observations**: 

- [ ] **Open items involving attendees**  
  Action items, asks, commitments listed?  
  **Observations**: 

- [ ] **Previous meeting referenced** *(if recurring)*  
  References what was discussed last time?  
  **Observations**: 

- [ ] **Talking points relevant**  
  Address actual open items, not generic filler?  
  **Observations**: 

- [ ] **"Next up" widget works**  
  Bottom-right widget links to correct prep brief?  
  **Observations**: 

- [ ] **Back-to-back instant access**  
  After one meeting, next prep brief loads instantly?  
  **Observations**: 

---

### Voice Profile

- [ ] **Profile accurately describes your writing**  
  Admin → Communication/Voice — captures greeting, sign-off, formality, length?  
  **Observations**: 

- [ ] **Custom rules saveable**  
  Add a test rule — does it save?  
  **Observations**: 

---

### Response Workflow

- [ ] **"Respond" opens directive input**  
  Command Center → Decisions → Respond button?  
  **Observations**: 

- [ ] **Draft generates correctly**  
  Type directive, click generate — full email appears?  
  Directive tested: _______________  
  **Observations**: 

- [ ] **Draft sounds like you**  
  Compare against your recent sent emails — same tone?  
  **Observations**: 

- [ ] **To/Subject/Threading correct**  
  Correct recipient, Re: prefix, would thread properly?  
  **Observations**: 

- [ ] **Edit and discard work**  
  Edit text, click Discard — closes without sending?  
  **Observations**: 

- [ ] **Teams ask → Teams response** *(if applicable)*  
  Teams-originated ask generates Teams message instead of email?  
  **Observations**: 

---

### Drafts

- [ ] **Auto-nudges generated for stale items**  
  Drafts section shows nudge drafts?  
  **Observations**: 

- [ ] **Nudge content appropriate**  
  Professional, not too aggressive, correct item and timeframe?  
  **Observations**: 

- [ ] **Meeting recap drafts exist** *(if meetings processed)*  
  Recap drafts for recently completed meetings?  
  **Observations**: 

- [ ] **Send actually works** *(optional — only if comfortable sending)*  
  Clicked Send — email appeared in Sent Items?  
  **Observations**: 

---

### Readiness

- [ ] **Readiness page shows scores**  
  /readiness — table populated?  
  **Observations**: 

- [ ] **Scores match intuition**  
  Overloaded people score highest? Light-load people score lowest?  
  **Observations**: 

- [ ] **Expandable rows show items**  
  Click a person — shows their open items, asks, workstreams?  
  **Observations**: 

- [ ] **Caveat displayed**  
  "Scores reflect workload visible through your meetings, emails, and Teams activity"?  
  **Observations**: 

---

### Sentiment & Department Health

- [ ] **Sentiment scores populated**  
  /departments — real values, not all 0 or identical?  
  **Observations**: 

- [ ] **Trend arrows visible**  
  Up/down/flat showing for departments?  
  **Observations**: 

- [ ] **Friction pairs flagged** *(if applicable)*  
  Known tension between departments flagged?  
  **Observations**: 

- [ ] **Workstream sentiment on command center**  
  Cards show sentiment dots and trend arrows?  
  **Observations**: 

---

### RAG Chat

- [ ] **Factual question answered correctly**  
  Question: _______________  
  Answer accurate? Citations correct?  
  **Observations**: 

- [ ] **Entity lookup works**  
  Question: _______________  
  Correct list returned? Matches /actions page?  
  **Observations**: 

- [ ] **Summarization works**  
  Question: _______________  
  Accurate summary with sources?  
  **Observations**: 

- [ ] **Conversation continuity**  
  Follow-up question: _______________  
  Maintained context from previous answer?  
  **Observations**: 

- [ ] **Floating widget works**  
  Opened from a different page — functions correctly?  
  **Observations**: 

---

### Notifications

- [ ] **macOS notification for briefing**  
  Appeared at configured time? Check Notification Center.  
  **Observations**: 

- [ ] **Email-to-self** *(if enabled)*  
  Briefing email arrived in inbox?  
  **Observations**: 

- [ ] **Teams-to-self** *(if enabled)*  
  Briefing message in Teams chat?  
  **Observations**: 

- [ ] **Meeting prep notification at 15 min**  
  Notification fired before a meeting?  
  **Observations**: 

---

### Dashboard Command Center

- [ ] **Zone 1: Workstream cards**  
  Horizontal scroll, pinned first, name/status/sentiment/trend/counts?  
  **Observations**: 

- [ ] **Zone 2: Requires your attention**  
  Tabbed (decisions/awaiting/stale), populated with real data?  
  **Observations**: 

- [ ] **Zone 3: Today's meetings**  
  Meetings with topics and prep brief links, clicking opens brief?  
  **Observations**: 

- [ ] **Zone 4: Drafts**  
  Pending drafts with send/edit/discard, all buttons work?  
  **Observations**: 

- [ ] **Zone 5: "Next up" widget**  
  Bottom-right, countdown, links to prep brief?  
  **Observations**: 

- [ ] **Zone 6: "Ask Aegis" panel**  
  Toggleable right sidebar, receives questions, returns answers?  
  **Observations**: 

- [ ] **Sidebar navigation**  
  All pages reachable, current page highlighted?  
  **Observations**: 

- [ ] **Dashboard refreshes**  
  New data appears after polling cycle?  
  **Observations**: 

---

## Part C: Phase 5 Checks

### Error Handling & Recovery

- [ ] **Screenpipe restart survival**  
  Stop Screenpipe, wait 2 min, restart it. Does Aegis detect the outage and recover?  
  macOS notification for Screenpipe down?  
  **Observations**: 

- [ ] **OAuth token refresh**  
  Has the app been running long enough for a token refresh cycle? Any auth errors in system_health?  
  **Observations**: 

- [ ] **Crash recovery**  
  Stop Aegis mid-polling cycle (Ctrl+C). Restart. Are stuck items reset from 'processing' to 'pending'?  
  ```sql
  SELECT COUNT(*) FROM meetings WHERE processing_status = 'processing';
  SELECT COUNT(*) FROM emails WHERE processing_status = 'processing';
  ```
  Both should be 0 after restart.  
  **Observations**: 

---

### Security

- [ ] **Token cache permissions**  
  ```bash
  ls -la ~/.aegis/msal_token_cache.json
  ```
  Should show `-rw-------` (600). Not readable by group/other.  
  **Observations**: 

- [ ] **No PII in logs**  
  ```bash
  grep -i "body_text\|transcript_text\|body_preview" logs/*.log | head -5
  ```
  Should return nothing. If it returns lines, PII is leaking.  
  **Observations**: 

- [ ] **.env not in git**  
  ```bash
  git status | grep .env
  ```
  Should not appear. Check .gitignore includes .env.  
  **Observations**: 

---

### Backup & Data Management

- [ ] **Backup exists and is recent**  
  ```bash
  ls -la ~/.aegis/backups/
  ```
  Most recent backup < 28 hours old? File size > 100 KB?  
  **Observations**: 

- [ ] **Backup rotation working**  
  How many backup files? Should be ≤ 30 (30-day rotation).  
  Count: ___  
  **Observations**: 

- [ ] **Database size reasonable**  
  ```sql
  SELECT pg_size_pretty(pg_database_size('aegis'));
  ```
  Size: ___  
  **Observations**: 

---

### Admin Settings

- [ ] **Admin page loads**  
  Navigate to /admin — does it render with collapsible sections?  
  **Observations**: 

- [ ] **Settings categories present**  
  Check for sections: Connections, Polling, Triage, Workstream Detection, Meeting Processing, Intelligence Schedule, Notifications, Communication/Voice, Org Chart, Sentiment, Data Retention, LLM Config, System  
  Missing sections: _______________  
  **Observations**: 

- [ ] **Changing a value takes effect**  
  Change a value (e.g., stale item threshold from 7 to 10 days). Verify it saves. Verify the system uses the new value (check if stale item count changes on the dashboard).  
  Setting changed: _______________  
  **Observations**: 

- [ ] **HTMX auto-save works**  
  Change a value — does it save without page reload?  
  **Observations**: 

---

### Search

- [ ] **Search page works**  
  Navigate to /search (or wherever search lives).  
  Search for a term you know exists across meetings, emails, and chats.  
  Search term: _______________  
  **Observations**: 

- [ ] **Results span multiple source types**  
  Does search return meetings AND emails AND chat messages?  
  **Observations**: 

- [ ] **Results are relevant**  
  Are the top results actually related to your query?  
  **Observations**: 

---

### Startup & Operations

- [ ] **Startup script exists**  
  ```bash
  ls scripts/aegis* || ls aegis
  ```
  Can you start/stop Aegis with a single command?  
  **Observations**: 

- [ ] **LaunchAgent exists** *(optional)*  
  ```bash
  ls ~/Library/LaunchAgents/com.aegis*
  ```
  Does Aegis auto-start on boot?  
  **Observations**: 

- [ ] **Log rotation configured**  
  ```bash
  ls -la logs/
  ```
  Are there rotated log files (e.g., .log.1, .log.gz)?  
  **Observations**: 

---

### Mobile Responsiveness

- [ ] **Dashboard at 375px width**  
  Resize browser to phone width. Is the command center usable?  
  **Observations**: 

- [ ] **Morning briefing at 375px**  
  Does the briefing content reflow correctly on narrow screens?  
  **Observations**: 

- [ ] **Meeting prep brief at 375px**  
  Is the prep brief readable on a phone-width viewport?  
  **Observations**: 

- [ ] **Readiness page at 375px**  
  Does the table scroll horizontally or reformat for mobile?  
  **Observations**: 

---

### Docker & Infrastructure

- [ ] **Docker container healthy**  
  ```bash
  docker ps | grep aegis-db
  ```
  Container running with status "Up"?  
  **Observations**: 

- [ ] **Database connection pool healthy**  
  ```sql
  SELECT COUNT(*) FROM pg_stat_activity WHERE datname = 'aegis';
  ```
  Active connections: ___ (should be < 20 for single-user app)  
  **Observations**: 

- [ ] **pgvector working**  
  ```sql
  SELECT extversion FROM pg_extension WHERE extname = 'vector';
  ```
  Version: ___  
  **Observations**: 

---

## Summary

| Part | Passed | Total | Score |
|------|--------|-------|-------|
| Phase 3 | ___ | 17 | __% |
| Phase 4 | ___ | 35 | __% |
| Phase 5 | ___ | 22 | __% |
| **Total** | ___ | **74** | __% |

### Issues Found

| # | Phase | Section | Severity | Description | Fix Notes |
|---|-------|---------|----------|-------------|-----------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| 6 | | | | | |
| 7 | | | | | |
| 8 | | | | | |
| 9 | | | | | |
| 10 | | | | | |

### Overall Assessment

**System Status**: PRODUCTION READY / NEEDS FIXES / CRITICAL ISSUES

Blocking issues: _______________

Phase 5 specific concerns: _______________

Ready to run as daily driver? **YES / NO**

Notes: _______________
