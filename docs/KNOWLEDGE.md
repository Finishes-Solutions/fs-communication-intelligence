# Knowledge Base

Matthew asked on 2026-10-28 for the system to "get smarter, start understanding relationships, projects, properties, terms etc over time". He also decided there would be **no seeded list and no approval step**. This page describes what was built, how it learns, and its limits. ADR-020 records the decision.

The model itself does not learn: Azure OpenAI keeps nothing between calls, and nothing is fine-tuned. What grows is a database of what emails said. Each new email's classification uses it, and agents read it through two MCP tools.

## What it records

The classifier's existing call returns a `knowledge` block alongside the labels, so there is no extra model call:

| Part | What | Identified by |
|---|---|---|
| names | properties, projects, developments, assets (`property`); companies, legal entities and other organizations (`organization`) | the name, normalized (`name_key`: case, spacing and punctuation ignored; "Company" = "Co."; "LLC" and "LP" stay different) |
| people | a person named together with their email address, plus title and company as written | their email address |
| relations | facts the email states: `works_for` (person → organization), `role_on` (person or organization → property, with a role such as `general_contractor`, `lender`, `architect`, `property_manager`, `tenant`), `owns` (organization → property), `affiliate_of` (organization → organization) | subject, relation, role, object |
| terms | abbreviations, project codes and industry terms the email explains, with their meaning | the term, normalized |

Processing stores this per message version in `kg_mentions` and `kg_relations` (migration `0016_knowledge.sql`), in the same transaction that publishes the message. A message that is superseded, withdrawn, restricted or excluded therefore drops out of the knowledge base, just as it drops out of search.

## How it gets smarter

1. **Accumulation.** Profiles are computed from every message that mentions an entry. The more emails mention a property, the more the profile shows: who has which role on it, which companies and people come up with it, how risks and action items trend, and the latest emails.
2. **Names fed back into classification.** Names seen in at least two messages over the last year (up to 150, most frequent first) are sent with each new email as `learned_names`. The model can then say an email's "WS Retail" is the same property as "Waller Station Retail". It may link only to a name it was shown, exactly as listed. The links it makes become the "also written" spellings shown in profiles.
3. **Your corrections.** Merging, renaming or hiding an entry changes what every reader sees and what the classifier is shown from then on.

## Safeguards without approval

Since nobody approves entries, these rules apply automatically:

- **Checked against the email.** A name must be written in the email or its attachments. A person's address must be one of the email's participants or written in it. A fact may only connect names found in the same email, with a fixed fact type and role. Anything else is dropped.
- **Phishing teaches nothing.** Mail with phishing indicators (any answer but "no") adds nothing. Neither does mail that is restricted, held, skipped as bulk, or not indexed. This keeps the fixture's look-alike "updated bank details" sender out of the base.
- **No payment details.** The prompt forbids bank, account and card numbers and payment instructions, and the schema has no field for them. Sensitive values are already masked before classification.
- **Learned names are data, not instructions.** They go to the model in a separate message marked as untrusted reference data, limited to short plain names (no line breaks, at most 80 characters).
- **Every fact shows its support:** how many messages and how many different senders state it, first and last seen, the latest wording, and a link to the email. One message from one sender is marked as unconfirmed in the console, and the MCP tool descriptions tell agents to say so.
- **Single roles over time.** For roles a property normally has one of at a time (owner, developer, general contractor, architect, lender, property manager), the most recently mentioned holder is marked `current` and the others `earlier`. Nothing is deleted.

**Known weakness.** Someone who sends several consistent emails can still plant a fact, for example "X is the lender on Y". Support counts make that visible, but they don't prevent it. Payment details are kept out entirely for this reason. Check any fact you will act on against its emails.

## Who sees what

Everything is computed per caller from the messages their grants allow, using the same SQL grant check as search, before anything is counted:

- A `Communications.Reader.Own` user sees only entries, spellings, counts and facts from their own mailbox. An entry seen only in someone else's mailbox doesn't exist for them, and its id finds nothing.
- A person whose address or domain is excluded as a participant (`ci scope exclude --participant`) is never shown, even when their name appears in another email's body.
- `ci_search` can read only the `published_kg_*` views, never the base tables.
- Copies of one message in several mailboxes count once.

## Using it

**Agents (MCP).**

- `find_knowledge(query, kind)` looks up a property, organization, person (by name or address), term or meeting. With no query it lists the most-mentioned entries.
- `get_profile(entity_id | name)` returns everything about one entry: facts in both directions with their support, related entries, activity and the latest emails (with `view_url`).

**Admin console.** The **Knowledge** page (under Insights) has search and a kind filter. Each entry shows its facts, support badges, related entries, activity by month and the latest emails. Buttons rename, fix the kind, merge into another entry (found by name), or hide it, and hidden entries are listed with an Unhide button. Ticking several entries in the list (or the select-all box) and choosing **Merge Selected Into…** merges them all into one, all or nothing: if one can't be merged (a person into a non-person), nothing changes (Matthew, 2026-10-29: one property had many entries). The **Lab** shows what an email would add to the knowledge base before anything is stored.

**CLI.**

```bash
ci knowledge list "waller"
```

```bash
ci knowledge show 42
```

```bash
ci knowledge merge 57 58 61 --into 42
```

```bash
ci knowledge reclassify 57 organization
```

```bash
ci knowledge hide 57
```

```bash
ci knowledge rename 42 "Waller Station Retail"
```

```bash
ci knowledge backfill
```

`list` and `show` run as the signed-in person, like `ci search`. Merge, unmerge, reclassify, hide, unhide and rename are audited operator changes, and each can be undone.

**Wrong kind** (Matthew, 2026-10-29: people filed as projects). The AI sometimes files a name under the wrong kind, such as a person's name as a property. There are two fixes, on the console's **Fix Kind…** button or from the command line:

* **Merge it into the right entry.** Kinds may differ, and the merged entry shows as the target's kind. For a person, pick the person, who is known by email address. A person entry itself can only merge into another person, because people are keyed by address and participant exclusions are checked on the entry readers see.
* **Reclassify it.** This finds or creates an entry of the new kind with the same name (`ci knowledge reclassify ID KIND`, migration 0018 lets the operator role create it) and merges the wrong one into it. A person created this way has no email address.

Either way the wrong entry stays, merged, so later emails where the AI repeats the mistake still land under the right kind. Unmerge undoes it.

**Meetings** (Matthew, 2026-10-29, migration 0019). A fifth kind for recurring or named meetings and events, such as a weekly ops meeting, an OAC meeting or a closing. It has two facts: a meeting is **about** a property, and a person **attends** a meeting. Before this kind existed, the AI filed a meeting under a person. The same change tells the AI to name a person only when the email itself ties the name to the address (sender or recipient name, signature, "Name <address>"), never from a first name mentioned in passing. Both apply to mail processed from now on. Earlier mistakes are fixed with Fix Kind… (now offering Meeting), Merge or Hide.

**Mail processed before this existed.** `ci knowledge backfill` adds it from what its classification already holds, with no AI call. That is only the property and legal-entity names, because those messages were never asked for people, facts or terms. For the full knowledge, reprocess them, at full classification cost:

```bash
ci process --remask --limit 0
```

## Deploying

Apply migration `0016_knowledge.sql` **before** the new code deploys, as with the earlier migrations. The deploy workflow doesn't run migrations, and processing writes the new `classification_runs.knowledge` column, so every processing run fails until the migration exists. From the repository on the Mac (it uses the signed-in Azure CLI user):

```bash
make azure-env && make azure-migrate AZ_PSQL=psql
```

`make azure-env` writes `local/azure-dev.env` (the database address and your sign-in) when it is missing.

Then backfill what earlier mail already holds:

```bash
ci knowledge backfill
```

## Cost

The knowledge block adds output tokens to every classification call, roughly 100 to 400 depending on the email. The learned names add up to about 1,500 input tokens per call once the base is full. This has not been measured on real mail yet. The Cost page meters actual tokens per run, so compare a day's classification cost before and after deploying.

## Not done yet

- **No measured quality.** Precision of extracted facts and of the model's same-name links is unknown until a sample of real mail is checked by hand. Add a sample check to the §11 measurements.
- **Search filters still match per-email names as written.** A `property=` filter does not yet expand through learned spellings. `get_profile` lists an entry's emails through all its spellings.
- **The person check doesn't use it yet.** The Chuck Sr./Jr. routing check doesn't use people knowledge.
- **No purge.** Entries whose only messages were deleted stay as rows, invisible to everyone. Purge follows D-R1 (retention).
