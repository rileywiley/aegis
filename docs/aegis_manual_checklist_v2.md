# Aegis — Phase 3+4 Manual Verification Checklist

**Date**: _______________  
**Tester**: _______________  
**App URL**: http://localhost:8000  
**Automated script result**: ___ / 28 checks passed  

---

## Phase 3 Checks

### Email Noise Filter Accuracy

- [ ] **Automated emails correctly classified**  
  Spot-checked 5 emails classified as "automated" — are they actually automated?  
  (JIRA notifications, CI/CD alerts, calendar accepts = correct)  
  **Observations**: 

- [ ] **Human emails correctly classified**  
  Spot-checked 5 emails classified as "human" — are they from real people?  
  **Observations**: 

- [ ] **Misclassifications noted**  
  Any real emails marked automated, or newsletters marked human?  
  **Observations**: 

---

### Email Triage Quality

- [ ] **Substantive emails are truly substantive**  
  Read 3 "substantive" emails — do they contain decisions, asks, or deliverables?  
  **Observations**: 

- [ ] **Contextual emails are truly low-value**  
  Read 3 "contextual" emails — are these acknowledgments or low-signal replies?  
  **Observations**: 

- [ ] **Nothing important missed**  
  Any substantive emails wrongly triaged as noise or contextual?  
  **Observations**: 

---

### Ask Directionality

- [ ] **"Directed at you" tab is accurate**  
  Navigate to /asks — are these actually things people asked YOU to do?  
  **Observations**: 

- [ ] **"You asked" tab is accurate**  
  Are these things YOU asked others to do?  
  **Observations**: 

- [ ] **Requester and target names correct**  
  Are the right people shown as requester vs target on each ask?  
  **Observations**: 

---

### Workstream Quality

- [ ] **Auto-detected workstreams match real initiatives**  
  Navigate to /workstreams — do you recognize these as actual work you're tracking?  
  List them:  
  **Observations**: 

- [ ] **Items within workstreams are topically coherent**  
  Click into the largest auto-detected workstream — do the items belong together?  
  **Observations**: 

- [ ] **No cross-department contamination**  
  Are items from unrelated departments grouped together? (They shouldn't be)  
  **Observations**: 

- [ ] **Unassigned items queue is reasonable**  
  Are there items that clearly belong to a workstream but weren't assigned?  
  **Observations**: 

---

### Org Chart & People

- [ ] **Needs-review queue shows new people with LLM suggestions**  
  Navigate to /people — are suggested titles and departments correct?  
  **Observations**: 

- [ ] **Approve/correct flow works**  
  Approve or correct 3-5 people — does it save correctly?  
  **Observations**: 

- [ ] **Org chart reflects reality**  
  Navigate to /org — are departments and manager assignments plausible?  
  **Observations**: 

---

### Teams Data

- [ ] **Teams and channels match your actual Teams setup**  
  Navigate to the system — do team names and channel names look right?  
  **Observations**: 

- [ ] **Teams-originated asks appear alongside email asks**  
  Navigate to /asks — do you see asks from both email and Teams sources?  
  **Observations**: 

---

## Phase 4 Checks

### Briefings

- [ ] **Morning briefing displays on command center**  
  Open the Command Center — is the morning briefing the default view?  
  **Observations**: 

- [ ] **Today's meetings listed with suggested topics**  
  Are 2-3 topics shown per meeting? Are they relevant?  
  **Observations**: 

- [ ] **Suggested topics reference real open items**  
  Do topics mention actual pending decisions, asks, or action items with the right attendees?  
  **Observations**: 

- [ ] **"Requires your action" section accurate**  
  Does it show real pending decisions and asks directed at you?  
  **Observations**: 

- [ ] **Overnight activity section accurate**  
  Does it reflect emails/chats that actually arrived recently?  
  **Observations**: 

- [ ] **Workstream health section accurate**  
  Do status and sentiment indicators match your perception?  
  **Observations**: 

- [ ] **Monday brief shows weekly objectives** *(skip if not Monday)*  
  Do the LLM-identified objectives make strategic sense? Are they prioritized well?  
  (Should identify priorities from deadlines, stale items, and momentum — not just calendar)  
  **Observations**: 

---

### Meeting Prep

- [ ] **Prep brief opens when clicking a meeting**  
  Click a meeting on today's calendar — does the prep brief appear?  
  **Observations**: 

- [ ] **Attendees listed with interaction context**  
  Does it show when you last spoke and what's open between you?  
  **Observations**: 

- [ ] **Open items involving attendees shown**  
  Action items, asks, and commitments involving meeting attendees?  
  **Observations**: 

- [ ] **Previous meeting in series referenced** *(if recurring)*  
  Does it reference what was discussed last time?  
  **Observations**: 

- [ ] **Suggested talking points relevant**  
  Do they address actual open items, not generic filler?  
  **Observations**: 

- [ ] **"Next up" widget works**  
  Click the floating widget (bottom-right) — does it link to the correct prep brief?  
  **Observations**: 

- [ ] **Back-to-back prep access is instant**  
  After one meeting ends, can you immediately view the next prep brief? (No loading delay — should be pre-computed)  
  **Observations**: 

---

### Voice Profile

- [ ] **Auto-generated profile accurately describes your writing style**  
  Go to Admin → Communication/Voice. Read the profile.  
  Does it capture: greeting style, sign-off, formality, typical length, patterns?  
  **Observations**: 

- [ ] **Custom rules can be added**  
  Add a test rule (e.g., "Never use 'Hope this helps'") — does it save?  
  **Observations**: 

---

### Response Workflow

- [ ] **"Respond" button opens directive input**  
  Go to Command Center → Requires Your Attention → Decisions tab → click "Respond"  
  **Observations**: 

- [ ] **Draft generation produces a full email**  
  Type a short directive (e.g., "Approved with condition to cap at $280K").  
  Click "Generate draft." Does a complete email appear?  
  **Observations**: 

- [ ] **Draft sounds like you**  
  Compare the generated email against your recent sent emails. Same tone, formality, sign-off?  
  **Observations**: 

- [ ] **To/Subject/Threading correct**  
  Is the To field correct? Subject starts with "Re:"? Would it thread correctly in Outlook?  
  **Observations**: 

- [ ] **Edit and discard work**  
  Edit the draft text. Then click Discard. Does it close without sending?  
  **Observations**: 

- [ ] **Teams-originated ask generates Teams message** *(if applicable)*  
  Find an ask that came from Teams. Does the response workflow generate a Teams message instead of an email?  
  **Observations**: 

---

### Drafts

- [ ] **Auto-nudges generated for stale items**  
  Check the Drafts section on the command center. Are there nudge drafts?  
  **Observations**: 

- [ ] **Nudge content is appropriate**  
  Read one — is it professional, not too aggressive, references the right item and timeframe?  
  **Observations**: 

- [ ] **Meeting recap drafts generated** *(if meetings have been processed)*  
  Do recap drafts exist for recently completed meetings?  
  **Observations**: 

- [ ] **Send actually works**  
  Click "Send" on a draft you're comfortable with. Check Sent Items in Outlook.  
  *(Only test this if you're comfortable sending a real email)*  
  **Observations**: 

---

### Readiness

- [ ] **Readiness page shows people with scores**  
  Navigate to /readiness — does the table populate?  
  **Observations**: 

- [ ] **Scores match your intuition**  
  Is the person you know is overloaded scoring highest? Is someone with a light load scoring low?  
  **Observations**: 

- [ ] **Expandable rows show specific items**  
  Click a person's row — does it show their open action items, asks, and workstreams?  
  **Observations**: 

- [ ] **Caveat displayed**  
  "Scores reflect workload visible through your meetings, emails, and Teams activity" — is this shown?  
  **Observations**: 

---

### Sentiment & Department Health

- [ ] **Department sentiment scores populated**  
  Navigate to /departments — do scores have real values (not all 0 or identical)?  
  **Observations**: 

- [ ] **Trend arrows visible**  
  Are up/down/flat trend arrows showing for departments?  
  **Observations**: 

- [ ] **Friction pairs flagged** *(if applicable)*  
  If you know of tension between two departments, is it flagged?  
  **Observations**: 

- [ ] **Workstream sentiment on command center**  
  Do workstream cards show sentiment dots and trend arrows?  
  **Observations**: 

---

### RAG Chat

- [ ] **Chat answers factual questions correctly**  
  Ask: "What did we decide about [something you know was decided]?"  
  Does the answer cite the correct meeting or email?  
  **Question asked**: _______________  
  **Observations**: 

- [ ] **Chat handles entity lookups**  
  Ask: "What are [person name]'s open action items?"  
  Does it return a correct list? (Compare against /actions page)  
  **Question asked**: _______________  
  **Observations**: 

- [ ] **Chat handles summarization**  
  Ask: "Summarize the [workstream name] this week"  
  Is the summary accurate with sources?  
  **Question asked**: _______________  
  **Observations**: 

- [ ] **Conversation continuity works**  
  Ask a follow-up referencing the previous answer. Does it maintain context?  
  **Follow-up asked**: _______________  
  **Observations**: 

- [ ] **Floating widget works**  
  Open the chat widget from a page other than /ask. Does it function correctly?  
  **Observations**: 

---

### Notifications

- [ ] **macOS notification for morning briefing**  
  Did a notification appear at the configured briefing time? Check Notification Center.  
  **Observations**: 

- [ ] **Email-to-self briefing** *(if enabled in admin)*  
  Check your inbox for the briefing email.  
  **Observations**: 

- [ ] **Teams-to-self briefing** *(if enabled in admin)*  
  Check your Teams chat for the briefing message.  
  **Observations**: 

- [ ] **Meeting prep notification at 15 minutes**  
  Wait for a meeting to be 15 minutes away. Does a notification fire?  
  **Observations**: 

---

### Dashboard Command Center

- [ ] **Zone 1: Workstream cards**  
  Horizontal scroll, pinned first, shows name/status/sentiment/trend/source counts/open items  
  **Observations**: 

- [ ] **Zone 2: Requires your attention**  
  Tabbed (decisions / awaiting / stale), each tab populated with real data  
  **Observations**: 

- [ ] **Zone 3: Today's meetings**  
  Shows meetings with topics and prep brief links. Clicking opens prep brief.  
  **Observations**: 

- [ ] **Zone 4: Drafts ready for review**  
  Shows pending drafts with send/edit/discard buttons. All three buttons work.  
  **Observations**: 

- [ ] **Zone 5: "Next up" floating widget**  
  Bottom-right, shows next meeting countdown, links to prep brief  
  **Observations**: 

- [ ] **Zone 6: "Ask Aegis" chat panel**  
  Toggleable right sidebar, receives questions and returns answers  
  **Observations**: 

- [ ] **Sidebar navigation**  
  All pages reachable from sidebar. Current page highlighted.  
  **Observations**: 

- [ ] **Dashboard refreshes**  
  After a polling cycle completes, does the dashboard reflect new data?  
  **Observations**: 

- [ ] **Mobile responsive** *(test at 375px viewport width)*  
  Resize browser to phone width. Do briefings and prep briefs display correctly?  
  **Observations**: 

---

## Summary

**Phase 3 checks**: ___ / 17 passed  
**Phase 4 checks**: ___ / 35 passed  
**Total**: ___ / 52 passed  

### Issues Found

| # | Section | Severity | Description | Fix Notes |
|---|---------|----------|-------------|-----------|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |
| 6 | | | | |
| 7 | | | | |
| 8 | | | | |
| 9 | | | | |
| 10 | | | | |

### Overall Assessment

Ready for Phase 5?  **YES / NO**

Blocking issues: _______________

Notes: _______________
