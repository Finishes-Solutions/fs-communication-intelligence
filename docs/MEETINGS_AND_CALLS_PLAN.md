# Meetings and calls: Fireflies backfill, Teams meeting transcripts, Teams Phone

Written 2026-11-04 (Matthew: "Also meetings, teams phone calls for people with teams phone subscriptions? How would we handle all of that? Additionally we should probably go ahead and create a fireflies complete meeting backfill too"). Status: **proposed**; nothing built. Facts marked "verified" were read on learn.microsoft.com or from the Fireflies account on 2026-11-04. The Fireflies pipeline design from September (`docs/FIREFLIES_PIPELINE.md`) still holds; this file decides what to do with it now and adds Teams meetings and calls.

---

## 1. Summary and recommendation

There are three ways spoken conversations can reach the platform. They are not equal.

| Source | What it holds | History available | Microsoft or vendor approvals | Verdict |
|---|---|---|---|---|
| **Fireflies** | Full transcripts with speaker names, AI summaries, action items, attendees, for every meeting the notetaker joined (Teams, Zoom, Google Meet, phone uploads) | **All of it**: 339 transcripts since 2025-01-01 in the account today (verified) | None: an API key from the Fireflies account; the account is already paid for and in use | **Build first.** The complete backfill Matthew asked for is small and needs no Microsoft change. |
| **Teams meeting transcripts** (Graph) | Transcripts of meetings where someone turned transcription or recording on; speaker names only if an admin allows it | Only transcripts that still exist in the organizer's OneDrive; transcripts and recordings can be set to expire | Tenant toggle "Transcript API access" (off by default), speaker-attribution toggle (off by default), an application access policy, plus `OnlineMeetingTranscript.Read.All` | **Second.** Fills gaps where Fireflies did not join. About half of the Fireflies meetings sampled already carry a Teams join link, so the overlap is large and must be deduplicated, never merged. |
| **Teams Phone and 1:1 calls** | Call records (who, when, how long, which numbers) always; transcript only when the call was transcribed, and only delivered by change notification while it happens | **No history** for call transcripts; call records are kept 30 days | `CallRecords.Read.All` (metadata), `CallTranscripts.Read.All` (content), calling-policy change so people can transcribe calls | **Last, and only the metadata is cheap.** Without a transcript a call record says that Chuck called a number for 14 minutes, not what about. |

Recommended order: **M1 Fireflies backfill and live capture → M2 meeting windows and retrieval (shared with Teams chat) → M3 Teams meeting transcripts with deduplication → M4 call records, and call transcripts only if the calling policy and people's habits make them exist.**

---

## 2. Fireflies: what the account holds (verified 2026-11-04 through the connected account, counts only)

| | Value |
|---|---|
| Transcripts since 2025-01-01 | **339** (earliest in the sample: February 2025) |
| Sampled duration (150 of them) | average 50 minutes, so roughly **280 hours** in total |
| Seats on the team | 29 listed; 15 with at least one meeting in the last 30 days |
| Last 30 days | 28 meetings, about 25 hours |
| Organizers | concentrated: two people organised about two thirds of the sample |
| Meeting platform | about half of the sample has a Teams join link; a fifth has no link (uploads, phone or in-person recordings); the rest Zoom and others |
| Matthew's account | admin; API access available; an active Zapier integration exists (inventory it before launch, FF-D7) |

Months in the sample cluster in spring 2025 and autumn 2025 and 2026, so usage is uneven; the backfill should not assume a steady rate.

**Size and cost.** 280 hours is roughly 2.5 million words, about 3.5 million tokens of transcript. Windowed as in §4 that is on the order of 3,000 to 4,000 classification calls plus embeddings: **tens of dollars at unverified list prices** for the whole backfill, and a few dollars a month after. The Fireflies API allows 60 requests a minute on Business and Enterprise (`docs/FIREFLIES_PIPELINE.md` §2; re-verify the plan tier); 339 transcripts fetch in under an hour.

**What the backfill is.** `ci fireflies backfill plan --since 2025-01-01 --days 30`: the same `ops_backfill_windows` mechanism as email and Teams with `source = 'fireflies'`, listing transcripts by date window (`transcripts(fromDate, toDate, limit 50, skip)`, splitting a full page), then fetching each transcript's sentences, speakers, attendees, summary and action items. Everything in `docs/FIREFLIES_PIPELINE.md` §3 applies: scope by Fireflies user, exclusions by meeting id, HMAC-verified webhook plus a 30-minute reconcile for live capture, deletion sweeps, summaries stored as a provider artifact labelled AI output.

**Who may read a meeting.** The attendee list (`meeting_attendees`, `participants`) is the meeting's own access list: a `Communications.Reader.Own` reader sees meetings they attended; `Reader.All` sees all; reader limits apply (Will does not see a meeting Chuck attended unless Will attended it too, under his existing `person` limit). Meetings with no attendee list (uploads) are visible to `Reader.All` only until an operator assigns them.

**Decisions carried over** (`docs/FIREFLIES_PIPELINE.md` §5): FF-D1 is settled by D-M2 (the automation account's key); FF-D2 whether HR, board or 1:1 meetings are held for review by title keyword rather than indexed silently; FF-D5 index the Fireflies summary and action items as their own labelled chunks (recommend yes); FF-D6 scope = all seats, backfill from 2025-01-01.

---

## 3. Teams meeting transcripts and calls: what Microsoft Graph offers (verified unless marked)

### 3.1 Meeting transcripts

- API: `GET /users/{id}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{id}', startDateTime, endDateTime)` ([doc](https://learn.microsoft.com/graph/api/onlinemeeting-getalltranscripts?view=graph-rest-1.0)), per **organizer**, with a `delta` function for incremental sync. Content as WebVTT with `<v Speaker>` tags, or a speaker-less text format. Channel meetings are **not** returned by this call (the per-meeting `Get callTranscript` can read them).
- Permission: application `OnlineMeetingTranscript.Read.All`.
- Three tenant-admin steps, all off by default ([doc](https://learn.microsoft.com/microsoftteams/meeting-transcript-api-access)):
  1. Teams admin center → Meetings → Meeting settings → **Transcript API access → Microsoft Graph access: On**. Otherwise every transcript call returns `403 GraphAccessToTranscriptsDisabled`.
  2. **Include speaker attribution: On.** Otherwise transcripts come back without speaker names, which makes "who said what" impossible. PowerShell: `Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true -EnableAttributedTranscripts $true -Identity Global`.
  3. An **application access policy** (`New-CsApplicationAccessPolicy` / `Grant-CsApplicationAccessPolicy`) naming the identity, granted to the organizers whose meetings it may read ([doc](https://learn.microsoft.com/graph/cloud-communication-online-meeting-application-access-policy)). This is the only per-person scope Microsoft offers here, and it is a real one: grant it to all users, or only to named organizers.
- A transcript exists only if someone turned on transcription or recording in that meeting. Nothing in Graph transcribes after the fact.
- Transcripts live in the organizer's OneDrive and follow its retention and expiry settings; how far back any still exist must be checked in the tenant (not verified). Meeting chats (captured by the Teams chat pipeline) are the only trace of meetings that were never transcribed.
- Copilot "AI insights" (summaries, action items) for meetings exist as a separate API and need Copilot licences; not planned.

### 3.2 Teams Phone, 1:1 and group calls

- **Call records** (`CallRecords.Read.All`; PSTN log via `CallRecord-PstnCalls.Read.All`, [doc](https://learn.microsoft.com/graph/api/resources/callrecords-api-overview?view=graph-rest-1.0)): participants, start and end, modality, phone numbers, call quality. Kept **30 days**; delivered by change notification after each call. Microsoft's own warning: these records "can provide insights into the operation of your business" and should be granted with discretion. No content.
- **Call transcripts** (`CallTranscripts.Read.All`, "ad hoc calls" including PSTN, 1:1 and group calls, [doc](https://learn.microsoft.com/graph/api/calltranscript-get?view=graph-rest-1.0)): only when the call was transcribed, and only reachable through change notifications at the time of the call; there is no "get all" listing for calls, so **no backfill is possible**. Users need a calling policy that allows transcription, and must start it (or have Copilot in calls). Whether any policy transcribes calls automatically without the user pressing a button is **not verified**.
- The same admin transcript toggles as §3.1 apply.

So for calls: the metadata feed is a day's work and cheap, the content feed depends on habits that do not exist yet. The honest expectation is that most Teams Phone calls will stay invisible.

---

## 4. Design: one meetings pipeline for Fireflies and Teams transcripts

Reuses the source-neutral foundation exactly as the Fireflies plan intended: `communications.kind = 'meeting'`, `transcript_segments` (speaker, start and end ms, text), `chunks.chunk_kind = 'transcript_window'` and `'provider_summary'`, `relationships.kind = 'same_meeting'`.

- **Scopes.** `fireflies_user` (already in `0001`) and, for Graph, `teams_organizer` (new kind, native id = Entra object id). Exclusions by meeting id and by title keyword hold-for-review (FF-D2).
- **Identity.** A Fireflies transcript and a Teams transcript of the same meeting are **two communications** linked by `same_meeting` (matched on Teams join URL plus start time within 10 minutes), each with its own provenance and access list, collapsed in results after authorization (ADR-002, `docs/DATABASE.md` §4). Never union permissions.
- **Windows** (the same idea as Teams chat windows, `docs/TEAMS_PLAN.md` §5.4): a transcript is cut into windows of about 8 to 12 minutes or 1,500 words at speaker-turn boundaries, each with a header (title, date, attendees, organizer, source, and the previous window's one-line AI summary for continuity). Windows are classified (same labels, meeting prompt variant), embedded, and cited with timestamps: Fireflies deep links (`https://app.fireflies.ai/view/{id}?t={seconds}`, verified from the connector) and Teams recap links.
- **Summaries and action items from the vendor** are stored as `provider_summary` artifacts and chunks labelled "Fireflies AI summary (unverified)"; our own classifier still runs on the transcript, so action items on the Action Items page come from our labels with the vendor's list shown beside them, never merged.
- **Masking and screening** unchanged (PII masking before classification; Prompt Shields on the classifier input).
- **Console.** Three top-level tabs (Matthew, 2026-11-04): **Emails** (today's Messages page, renamed), **Messages** (Teams chat, later other message sources; `docs/TEAMS_PLAN.md` §6.1) and **Meetings** (Fireflies now, Teams transcripts later).
- **Retrieval.** MCP tools `search_meetings`, `get_meeting`, `get_transcript_window`, `list_meetings` (the names ADR-013 planned), all through the same authorized code; `list_actionable`, `email_stats`, `risk_overview`, `find_knowledge` take `source = meetings`. Console: a Meetings page and the read page `/read/m/<id>`.
- **Links to chat and email.** `same_meeting` to the Teams meeting chat; the knowledge base's `meeting` kind (migration 0019) gets real meetings behind it.

---

## 5. Decisions needed

| ID | Question | Default until decided |
|---|---|---|
| D-M1 | Fireflies is the primary meeting source; backfill all 339 transcripts from 2025-01-01, all seats, exclusions by title or id. | Yes, as asked on 2026-11-04. |
| D-M2 (decided 2026-11-04) | Which Fireflies seat owns the API key and webhook. | **The automation account's seat** (`svc_automation@finishessolutions.com`) already has a Fireflies API key; it goes into Key Vault as `fireflies-api-key`, read by the ingestion identity through an app-setting reference, never into the repo, logs or chat. M1 must verify that this seat's key lists the whole team's transcripts (the count should match the 339 seen from Matthew's admin seat); if it lists only its own meetings, the seat needs the Fireflies admin role. Matthew's personal key is not used. |
| D-M3 | Turn on Microsoft Graph transcript access and speaker attribution for the whole tenant, and grant the application access policy to all users or named organizers. | Not turned on until decided; Fireflies does not need it. |
| D-M4 | Allow call transcription in the calling policy and ask people to use it; capture call records (metadata) at all. | Neither; revisit after M3. |
| D-M5 | Who may read meetings: same roles as email with attendee-based "own" access, reader limits applied. | Same roles. |
| D-M6 | Index vendor summaries and action items as labelled chunks. | Yes (FF-D5). |

---

## 6. PR sequence

| PR | Content | Done when |
|---|---|---|
| **M1 Fireflies capture and backfill** (built 2026-11-04, not yet run against the real account) | `ci_fireflies`: GraphQL client (paced, 429 retry), fixture client with seven synthetic transcripts, migration `0027_meetings.sql` (`fireflies_users`, `fireflies_meetings`, transcript and vendor-summary texts, `ops_backfill_windows.source`, `published_meetings`), inventory and scope (`ci fireflies check|init|inventory|scope|capture`), windowed capture with edits as new versions and deletions withdrawn, backfill plans per source run by the scheduler, an hourly rolling capture (`FIREFLIES_CAPTURE_ENABLED`), the key in Key Vault (`scripts/azure/11-fireflies.sh`). No webhook yet: the rolling capture is the reconcile. Steps in `docs/DEPLOY_AZURE.md` §14. | Fixture tests pass (unit and database). Still to do: `ci fireflies check` with the real key, the 339 transcripts captured into dev, counts per organizer compared with the Fireflies listing. |
| **M2 Meeting windows, processing, retrieval** | Windowing at speaker turns, meeting prompt variant, provider-summary chunks, the four MCP tools, console Meetings page and read page, `source` parameters, reader limits. | Authorization negatives (wrong audience, non-attendee with Reader.Own, excluded meeting, paused mode, forged filter) return nothing; Claude, ChatGPT and Copilot answer one meeting question each with timestamped citations. |
| **M3 Teams meeting transcripts** | Admin toggles and application access policy (`scripts/azure/11-meeting-transcripts.sh` prints the Teams PowerShell), `OnlineMeetingTranscript.Read.All` on `id-fs-ci-teams-dev`, `getAllTranscripts` per organizer with delta, WebVTT parsing, `same_meeting` matching against Fireflies. | One meeting present in both sources shows once in results with two citations; a meeting only in Teams shows once. |
| **M4 Calls** (only if D-M4 = yes) | Call-record notifications into a metadata-only `calls` table (who, when, duration, number), optional call-transcript notifications into the meetings pipeline. | Call records appear within minutes of a call; a transcribed test call is searchable. |

---

## 7. Risks and unknowns

1. **Fireflies API behaviour at scale** (page caps, unstable `skip` ordering) is reported, not verified; the backfill splits windows and dedupes by id to be safe.
2. **Duplicate meetings** across Fireflies and Teams, and duplicate Fireflies entries (the sample shows the same meeting recorded twice under two titles): collapse by join link and start time, show both citations, never merge access.
3. **Teams transcript history** may be mostly expired; M3 may add little beyond live capture.
4. **Call content** will mostly not exist (§3.2).
5. **Privacy.** Transcripts of 1:1 meetings and calls are the most sensitive text in the company; D-M5 and FF-D2 belong in Chuck's access review before M2 ships beyond Matthew.
6. **Window and summary quality** is unmeasured until the Label Review sample and the retrieval test set include meetings.
