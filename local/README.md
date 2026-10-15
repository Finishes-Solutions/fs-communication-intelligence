# Local testkit

Run the whole email pipeline on your Mac: synthetic test mail goes in, and you watch it get captured, screened, classified, indexed (or deliberately held back) and searched. No Azure and no real mail.

## What is real and what is fake

| Real code (what will run in Azure) | Fake stand-ins (local only) |
|---|---|
| PostgreSQL 18 + pgvector schema, roles and views | Microsoft Graph: synthetic mailboxes from `tests/fixtures/email` |
| Mailbox scope, approvals, exclusions, mode switch | Screening: simple patterns instead of Azure AI Language / Prompt Shields |
| Per-folder delta capture, job queue, retries | Classifier: keyword rules with made-up probabilities |
| Normalizing, attachment policy (D-E1), duplicate linking | Embeddings: word hashing instead of Azure OpenAI |
| Outcome rules, chunking, atomic publication | Sign-in: you pick "executive" or "no role" instead of an Entra token |
| Authorized search (as the `ci_search` role) | Blob storage: a folder in `local/.data` |

The classifier's probabilities only exercise the rules. They tell you nothing about how accurate a real model will be.

## One-time setup (Mac)

```bash
brew install uv                         # Python tool; installs Python 3.14 itself
brew install --cask docker-desktop      # then open Docker Desktop once
brew install libpq && brew link --force libpq   # psql, used to apply migrations
```

## Every time

From the repository folder, with Docker Desktop running:

```bash
uv sync
uv run ci up
```

`ci up` starts the database container, creates and seeds it the first time, and opens the UI at http://127.0.0.1:8765. Press **Run everything** on the flow board. Stop it with Ctrl+C.

## What to try

1. **Flow board:** every synthetic message sits in the lane it ended up in (searchable, flagged for review, restricted, held, not indexed, excluded, not read). Click any card to see what each stage decided and why.
2. **Send a test email:** pick a scenario or write your own, deliver it, and follow it through the flow.
3. **Scope & rules:** approve the `leasing@` mailbox, exclude a mailbox or a sender domain, pause the system, switch the attachment policy (D-E1) and press **Reprocess everything**, simulate a screening outage, or arm Graph 429/410 failures.
4. **Search:** query as an executive and as an employee without a reader role (who must get nothing). Copies of the same message collapse into one result. Restricted items never appear.

## Other commands

```bash
uv run ci run       # run the flow once in the terminal
uv run ci status    # mode, mailboxes, queue
uv run ci reset     # wipe and reseed (database + fixture mailboxes)
uv run ci down      # stop the database (add --wipe to delete its data)
```

## Notes

- The database listens on 127.0.0.1:5433 (not 5432, so it won't clash with another PostgreSQL), and the UI listens on 127.0.0.1 only.
- Never paste real email into the compose form. Use invented names and `.example` addresses.
- `python3 db/run_checks.py` drops cluster-level roles. Point it at its own throwaway container (see the root README), not at this one.
- Azurite (Functions host storage) is in `docker-compose.yml` under the `functions` profile; it isn't needed for the testkit.
