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
  **Observations**: FAIL, emails from Anna Austin, Marc Leighton, and Josh Rose are being marked as "auto" but should be "human"

- [ ] **Human emails correctly classified**  
  Spot-checked 5 emails classified as "human" — are they from real people?  
  **Observations**: FAIL, emails from donotreply@adp.com are being marked as "human" but should be "auto"

- [ ] **Misclassifications noted**  
  Any real emails marked automated, or newsletters marked human?  
  **Observations**: PASS.

---

### Email Triage Quality

- [ ] **Substantive emails are truly substantive**  
  Read 3 "substantive" emails — do they contain decisions, asks, or deliverables?  
  **Observations**: FAIL, Human emails with intent have good summaries and actions items. Many human emails are missing intents and in some emails, Asks are being captured for external people. we need to update Asks to note if they are internal or external asks.

- [ ] **Contextual emails are truly low-value**  
  Read 3 "contextual" emails — are these acknowledgments or low-signal replies?  
  **Observations**: FAIL, FYI emails are properly classified but many emails remain unclassified by the llm. 

- [ ] **Nothing important missed**  
  Any substantive emails wrongly triaged as noise or contextual?  
  **Observations**: FAIL, many susbstantive emails are not being classified at all. 

---

### Ask Directionality

- [ ] **"Directed at you" tab is accurate**  
  Navigate to /asks — are these actually things people asked YOU to do?  
  **Observations**: FAIL, there is not "directed at you" tab. Also the Inbound and Outbound tabs are not filtering. We need to add the ability to filter for internal or external tasks.

- [ ] **"You asked" tab is accurate**  
  Are these things YOU asked others to do?  
  **Observations**: FAIL, there is no "you asked" tab.

- [ ] **Requester and target names correct**  
  Are the right people shown as requester vs target on each ask?  
  **Observations**: PASS. Update, when clicking through to the email from the Asks page, the email page should have back button to return to the ASKs page. the breadcrumb should be based on where the user came from and not static. 

---

### Workstream Quality

- [ ] **Auto-detected workstreams match real initiatives**  
  Navigate to /workstreams — do you recognize these as actual work you're tracking?  
  List them:  
  **Observations**: FAIL, only 1 workstream has been detected, there should be more.

- [ ] **Items within workstreams are topically coherent**  
  Click into the largest auto-detected workstream — do the items belong together?  
  **Observations**: FAIL, emails can be seen but Chat messages can not be clicked through to reveiw. 

- [ ] **No cross-department contamination**  
  Are items from unrelated departments grouped together? (They shouldn't be)  
  **Observations**: PASS

- [ ] **Unassigned items queue is reasonable**  
  Are there items that clearly belong to a workstream but weren't assigned?  
  **Observations**: FAIL, no unassigned items visibile. Manually created workstream is not being autopopulated by LLM.

---

### Org Chart & People

- [ ] **Needs-review queue shows new people with LLM suggestions**  
  Navigate to /people — are suggested titles and departments correct?  
  **Observations**: FAIL, each card reads "No LLM suggestins avialable yet". The system is also being up external email addressed and people.

- [ ] **Approve/correct flow works**  
  Approve or correct 3-5 people — does it save correctly?  
  **Observations**: PASS, 

- [ ] **Org chart reflects reality**  
  Navigate to /org — are departments and manager assignments plausible?  
  **Observations**: FAIL, there is no way to assign people to a department or to manually create/edit auto-generated departments.

---

### Teams Data

- [ ] **Teams and channels match your actual Teams setup**  
  Navigate to the system — do team names and channel names look right?  
  **Observations**: FAIL, no Teams data visibile

- [ ] **Teams-originated asks appear alongside email asks**  
  Navigate to /asks — do you see asks from both email and Teams sources?  
  **Observations**: FAIL, no chat data is visibile in /asks.

---

## Phase 4 Checks

### Briefings

- [ ] **Morning briefing displays on command center**  
  Open the Command Center — is the morning briefing the default view?  
  **Observations**: PASS

- [ ] **Today's meetings listed with suggested topics**  
  Are 2-3 topics shown per meeting? Are they relevant?  
  **Observations**: FAIL, topics are not revelent enough, they are too vauge, the LLM needs to do more to pick up themes from messages and emails.

- [ ] **Suggested topics reference real open items**  
  Do topics mention actual pending decisions, asks, or action items with the right attendees?  
  **Observations**: FAIL, topics are generic based on the title of the meeting.

- [ ] **"Requires your action" section accurate**  
  Does it show real pending decisions and asks directed at you?  
  **Observations**: FAIL, the topics are heavily weighted on the "fake" meeting transcripts we loaded at the start, they are not reflecting meeting or email actions. this maybe becasue screenpipe is not active.

- [ ] **Overnight activity section accurate**  
  Does it reflect emails/chats that actually arrived recently?  
  **Observations**: PASS

- [ ] **Workstream health section accurate**  
  Do status and sentiment indicators match your perception?  
  **Observations**: PASS

- [ ] **Monday brief shows weekly objectives** *(skip if not Monday)*  
  Do the LLM-identified objectives make strategic sense? Are they prioritized well?  
  (Should identify priorities from deadlines, stale items, and momentum — not just calendar)  
  **Observations**: SKIP

---

### Meeting Prep

- [ ] **Prep brief opens when clicking a meeting**  
  Click a meeting on today's calendar — does the prep brief appear?  
  **Observations**: PASS

- [ ] **Attendees listed with interaction context**  
  Does it show when you last spoke and what's open between you?  
  **Observations**: PASS

- [ ] **Open items involving attendees shown**  
  Action items, asks, and commitments involving meeting attendees?  
  **Observations**: FAIL, the LLM seems to confuse people with the same first name, for example Kim Buck and Kim DiCamillo.

- [ ] **Previous meeting in series referenced** *(if recurring)*  
  Does it reference what was discussed last time?  
  **Observations**: PASS

- [ ] **Suggested talking points relevant**  
  Do they address actual open items, not generic filler?  
  **Observations**: PASS.

- [ ] **"Next up" widget works**  
  Click the floating widget (bottom-right) — does it link to the correct prep brief?  
  **Observations**: PASS.

- [ ] **Back-to-back prep access is instant**  
  After one meeting ends, can you immediately view the next prep brief? (No loading delay — should be pre-computed)  
  **Observations**: PASS

---

### Voice Profile

- [ ] **Auto-generated profile accurately describes your writing style**  
  Go to Admin → Communication/Voice. Read the profile.  
  Does it capture: greeting style, sign-off, formality, typical length, patterns?  
  **Observations**: FAIL, admin panel is not yet implemented.

- [ ] **Custom rules can be added**  
  Add a test rule (e.g., "Never use 'Hope this helps'") — does it save?  
  **Observations**: FAIL, admin panel is not yet implemented.

---

### Response Workflow

- [ ] **"Respond" button opens directive input**  
  Go to Command Center → Requires Your Attention → Decisions tab → click "Respond"  
  **Observations**: FAIL, the button navigates to /respond but it does not bring over information (like source ID) of the item we are responding to so drafts are not autogenerated. when source id's are manually added and a draft is requested, the screen seems to repeat itself under the generatae draft modal.

- [ ] **Draft generation produces a full email**  
  Type a short directive (e.g., "Approved with condition to cap at $280K").  
  Click "Generate draft." Does a complete email appear?  
  **Observations**: PASS

- [ ] **Draft sounds like you**  
  Compare the generated email against your recent sent emails. Same tone, formality, sign-off?  
  **Observations**: PASS

- [ ] **To/Subject/Threading correct**  
  Is the To field correct? Subject starts with "Re:"? Would it thread correctly in Outlook?  
  **Observations**: PASS

- [ ] **Edit and discard work**  
  Edit the draft text. Then click Discard. Does it close without sending?  
  **Observations**: PASS

- [ ] **Teams-originated ask generates Teams message** *(if applicable)*  
  Find an ask that came from Teams. Does the response workflow generate a Teams message instead of an email?  
  **Observations**: FAIL, no TEAMS data is available. 

---

### Drafts

- [ ] **Auto-nudges generated for stale items**  
  Check the Drafts section on the command center. Are there nudge drafts?  
  **Observations**: FAIL, no nudge drafts are being generated.

- [ ] **Nudge content is appropriate**  
  Read one — is it professional, not too aggressive, references the right item and timeframe?  
  **Observations**: FAIL, no nudge drafts are being generated.

- [ ] **Meeting recap drafts generated** *(if meetings have been processed)*  
  Do recap drafts exist for recently completed meetings?  
  **Observations**: SKIP, meeting capture not yet implemented.

- [ ] **Send actually works**  
  Click "Send" on a draft you're comfortable with. Check Sent Items in Outlook.  
  *(Only test this if you're comfortable sending a real email)*  
  **Observations**: SKIP. 

---

### Readiness

- [ ] **Readiness page shows people with scores**  
  Navigate to /readiness — does the table populate?  
  **Observations**: FAIL, the readiness page is showing people outside of the organization.

- [ ] **Scores match your intuition**  
  Is the person you know is overloaded scoring highest? Is someone with a light load scoring low?  
  **Observations**: PASS.

- [ ] **Expandable rows show specific items**  
  Click a person's row — does it show their open action items, asks, and workstreams?  
  **Observations**: PASS.
  - TODO: The actions and asks need a way to be closed out from the readiness page that also affects how they appear on other pages too. The user also needs to be able to click into the ASK or Action and deteremine where it came from and with who.
  - TODO: Not chat data is being loaded.  
- [ ] **Caveat displayed**  
  "Scores reflect workload visible through your meetings, emails, and Teams activity" — is this shown?  
  **Observations**: PASS

---

### Sentiment & Department Health

- [ ] **Department sentiment scores populated**  
  Navigate to /departments — do scores have real values (not all 0 or identical)?  
  **Observations**: PASS
  - TODO: We need a way to manually add and edit departments. 

- [ ] **Trend arrows visible**  
  Are up/down/flat trend arrows showing for departments?  
  **Observations**: PASS.

- [ ] **Friction pairs flagged** *(if applicable)*  
  If you know of tension between two departments, is it flagged?  
  **Observations**: FAIL, the current autogenerated departments are not accurate to flag tension.

- [ ] **Workstream sentiment on command center**  
  Do workstream cards show sentiment dots and trend arrows?  
  **Observations**: FAIL, they do not she dots or trends. 

---

### RAG Chat

- [ ] **Chat answers factual questions correctly**  
  Ask: "What did we decide about [something you know was decided]?"  
  Does the answer cite the correct meeting or email?  
  **Question asked**: "what's the last update on novavax enrollment?"
  **Observations**: FAIL, error: "Sorry, I encountered an error processing your question. Please try again. (DBAPIError)"

- [ ] **Chat handles entity lookups**  
  Ask: "What are [person name]'s open action items?"  
  Does it return a correct list? (Compare against /actions page)  
  **Question asked**: _______________  
  **Observations**: FAIL, error: "Sorry, I encountered an error processing your question. Please try again. (DBAPIError)"

- [ ] **Chat handles summarization**  
  Ask: "Summarize the [workstream name] this week"  
  Is the summary accurate with sources?  
  **Question asked**: _______________  
  **Observations**: FAIL, error: "Sorry, I encountered an error processing your question. Please try again. (DBAPIError)"

- [ ] **Conversation continuity works**  
  Ask a follow-up referencing the previous answer. Does it maintain context?  
  **Follow-up asked**: _______________  
  **Observations**: FAIL, error: "Sorry, I encountered an error processing your question. Please try again. (DBAPIError)"

- [ ] **Floating widget works**  
  Open the chat widget from a page other than /ask. Does it function correctly?  
  **Observations**: FAIL, error: "Sorry, I encountered an error processing your question. Please try again. (DBAPIError)"

---

### Notifications

- [ ] **macOS notification for morning briefing**  
  Did a notification appear at the configured briefing time? Check Notification Center.  
  **Observations**: PASS

- [ ] **Email-to-self briefing** *(if enabled in admin)*  
  Check your inbox for the briefing email.  
  **Observations**: FAIL, admin  not yet implemented.

- [ ] **Teams-to-self briefing** *(if enabled in admin)*  
  Check your Teams chat for the briefing message.  
  **Observations**: FAIL, admin  not yet implemented.

- [ ] **Meeting prep notification at 15 minutes**  
  Wait for a meeting to be 15 minutes away. Does a notification fire?  
  **Observations**: PASS.

---

### Dashboard Command Center

- [ ] **Zone 1: Workstream cards**  
  Horizontal scroll, pinned first, shows name/status/sentiment/trend/source counts/open items  
  **Observations**: PASS

- [ ] **Zone 2: Requires your attention**  
  Tabbed (decisions / awaiting / stale), each tab populated with real data  
  **Observations**: PASS

- [ ] **Zone 3: Today's meetings**  
  Shows meetings with topics and prep brief links. Clicking opens prep brief.  
  **Observations**: PASS

- [ ] **Zone 4: Drafts ready for review**  
  Shows pending drafts with send/edit/discard buttons. All three buttons work.  
  **Observations**: PASS

- [ ] **Zone 5: "Next up" floating widget**  
  Bottom-right, shows next meeting countdown, links to prep brief  
  **Observations**: PASS

- [ ] **Zone 6: "Ask Aegis" chat panel**  
  Toggleable right sidebar, receives questions and returns answers  
  **Observations**: PASS

- [ ] **Sidebar navigation**  
  All pages reachable from sidebar. Current page highlighted.  
  **Observations**: PASS

- [ ] **Dashboard refreshes**  
  After a polling cycle completes, does the dashboard reflect new data?  
  **Observations**: PASS

- [ ] **Mobile responsive** *(test at 375px viewport width)*  
  Resize browser to phone width. Do briefings and prep briefs display correctly?  
  **Observations**: PASS

---

## Summary

Ready for Phase 5?: NO
