# Local development

Run the email pipeline on a laptop with **no Azure and no real mail**. Everything here uses the synthetic fixture tenant in `tests/fixtures/email/`.

Status (PR 4): the database, fixture Graph client, fake screener/classifier/embedder and the `ci` operator commands work. Capture (`ci reconcile`, the worker and the Function Apps) arrives in PR 5, search in PR 7.

## One-time setup (Mac)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
2. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
3. In the repo folder: `uv sync`

## Start

```bash
make dev
```

This starts PostgreSQL 18 + pgvector 0.8.2 and Azurite (both on 127.0.0.1 only), applies the migrations, loads the fixture tenant and prints the mode and mailbox list. Running it again is safe.

## Everyday commands

| Command | What it does |
|---|---|
| `make status` | Show the mode and every mailbox's state |
| `make test` | Run all tests, including the database tests, against the local stack |
| `make down` | Stop the containers (data is kept) |
| `make reset` | Delete the local database and storage, then `make dev` to start fresh |

Operator commands (each writes an `audit_events` row):

```bash
uv run --env-file local/local.env ci scope list
uv run --env-file local/local.env ci scope include morgan.lee@finishessolutions.example
uv run --env-file local/local.env ci scope exclude pat.example@finishessolutions.example --reason personal_mailbox
uv run --env-file local/local.env ci scope exclude pat.example@finishessolutions.example --revoke
uv run --env-file local/local.env ci scope exclude --participant @brooks-hale-law.example --reason legal_privilege
uv run --env-file local/local.env ci mode set paused
uv run --env-file local/local.env ci mode show
```

- Excluding a **mailbox** keeps that whole mailbox out. It does not hide that person's mail in other people's mailboxes. To do that, use `--participant` with an address or `@domain`.
- Exclusions always win over approvals.
- An empty scope makes capture refuse to run. It never means "everything".
- Production changes also need `--confirm-production`.

## What the fixture tenant contains

Six mailboxes: three approved (`pat.example`, `morgan.lee` and the shared `ap` mailbox), one excluded (`casey.private`), one outside the Exchange scope (Graph answers 403) and one user with no mailbox (404). There are 24 synthetic messages: invoice, change order, lease renewal, legal notice, lender draw, vendor complaint, newsletter, spam, a phishing look-alike, a fake SSN (expect restricted), a fake API key (expect restricted), a long thread, an inline signature image, one message in three mailboxes (one received by BCC), a message moved later, a message deleted later, a prompt-injection attempt, a draft, a sent item, a junk-folder item, a message in the excluded mailbox and one older than the backfill boundary. Each file's `fixture.expect` block states the outcome the pipeline should reach.

All names, companies, addresses, amounts and identifiers are invented, and every domain uses the reserved `.example` TLD. Never add real email to fixtures.

## Connecting without make

`local/local.env` holds the connection settings (`PGHOST=127.0.0.1`, `PGPORT=5433`, database `ci_local`). The password there opens only the throwaway local container.
