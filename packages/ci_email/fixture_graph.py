"""Fixture Graph backend (GRAPH_BACKEND=fixture): synthetic mailboxes on disk.

``FixtureMailStore`` is a small JSON "Exchange": mailboxes, folders and
messages, with a per-folder change log that drives delta queries. The testkit
uses it to deliver, move and delete test messages and to simulate failures
(429 throttling, 410 expired delta links). ``FixtureGraphClient`` reads it
through the same ``GraphClient`` interface as the real client.

Synthetic data only: fixtures live in tests/fixtures/email and use invented
people, companies and ``.example`` domains.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ci_email.graph import (
    DeltaChange,
    DeltaPage,
    FolderInfo,
    GraphDeltaExpired,
    GraphForbidden,
    GraphNotFound,
    GraphThrottled,
    MailboxInfo,
)

DELTA_PAGE_SIZE = 4  # small, so paging is exercised
THROTTLE_RETRY_AFTER = 2  # seconds
DEFAULT_FOLDERS = (
    ("inbox", "Inbox"),
    ("sentitems", "Sent Items"),
    ("drafts", "Drafts"),
    ("deleteditems", "Deleted Items"),
    ("junkemail", "Junk Email"),
    (None, "Projects"),
)

State = dict[str, Any]


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _short(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class FixtureMailStore:
    """File-backed synthetic Exchange. Every mutation is locked and atomic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- persistence -------------------------------------------------------

    @contextmanager
    def _locked(self, write: bool) -> Iterator[State]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            state: State = json.loads(self.path.read_text()) if self.path.exists() else _empty()
            yield state
            if write:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
                tmp.replace(self.path)

    def read(self) -> State:
        with self._locked(write=False) as state:
            return copy.deepcopy(state)

    def exists(self) -> bool:
        return self.path.exists()

    # -- setup -------------------------------------------------------------

    def reset_from_fixtures(self, fixture_dir: str | Path) -> int:
        """Replace the store with the synthetic fixture set. Returns messages delivered."""
        fixture_dir = Path(fixture_dir)
        mailboxes = json.loads((fixture_dir / "mailboxes.json").read_text())
        with self._locked(write=True) as state:
            state.clear()
            state.update(_empty())
            for mbx in mailboxes:
                folders = {}
                for wkn, name in DEFAULT_FOLDERS:
                    fid = f"fld-{_short(mbx['id'] + name)}"
                    folders[fid] = {"id": fid, "displayName": name, "wellKnownName": wkn}
                state["mailboxes"][mbx["id"]] = {**mbx, "folders": folders}
        count = 0
        for path in sorted((fixture_dir / "messages").glob("*.json")):
            scenario = json.loads(path.read_text())
            for delivery in scenario["deliveries"]:
                self.deliver(
                    delivery["mailbox"],
                    delivery.get("folder", "inbox"),
                    scenario["message"],
                    scenario_key=path.stem,
                )
                count += 1
        return count

    # -- mutations used by the testkit -------------------------------------

    def deliver(
        self,
        mailbox_address: str,
        folder: str,
        message: dict[str, Any],
        scenario_key: str = "composed",
    ) -> str:
        """Put a copy of ``message`` into a mailbox folder (by well-known or display name)."""
        with self._locked(write=True) as state:
            mbx = _mailbox_by_address(state, mailbox_address)
            fid = _folder_id(mbx, folder)
            state["counter"] += 1
            msg_id = f"AAMkAG-synthetic-{_short(mbx['id'])}-{state['counter']:05d}"
            body = copy.deepcopy(message)
            body.update(
                {
                    "id": msg_id,
                    "parentFolderId": fid,
                    "changeKey": f"CQAAAB-{state['counter']:05d}-1",
                    "webLink": f"https://outlook.office365.com/owa/?ItemID={msg_id}&exvsurl=1",
                }
            )
            body.setdefault("isDraft", folder == "drafts")
            body.setdefault("hasAttachments", bool(body.get("attachments")))
            body.setdefault("uniqueBody", body.get("body"))
            state["messages"][msg_id] = {
                "mailbox_id": mbx["id"],
                "deleted": False,
                "scenario": scenario_key,
                "message": body,
            }
            _log(state, mbx["id"], fid, msg_id, removed=False)
            return msg_id

    def move(self, message_id: str, folder: str) -> None:
        with self._locked(write=True) as state:
            entry = _entry(state, message_id)
            mbx = state["mailboxes"][entry["mailbox_id"]]
            new_fid = _folder_id(mbx, folder)
            old_fid = entry["message"]["parentFolderId"]
            if new_fid == old_fid:
                return
            entry["message"]["parentFolderId"] = new_fid
            _bump_change_key(entry)
            _log(state, mbx["id"], old_fid, message_id, removed=True)
            _log(state, mbx["id"], new_fid, message_id, removed=False)

    def hard_delete(self, message_id: str) -> None:
        with self._locked(write=True) as state:
            entry = _entry(state, message_id)
            entry["deleted"] = True
            _log(
                state,
                entry["mailbox_id"],
                entry["message"]["parentFolderId"],
                message_id,
                removed=True,
            )

    def simulate(
        self, *, throttle_calls: int | None = None, expire_delta_for: str | None = None
    ) -> None:
        """429 for the next N Graph calls; 410 on the next delta call for a mailbox."""
        with self._locked(write=True) as state:
            if throttle_calls is not None:
                state["simulate"]["throttle_calls"] = max(0, throttle_calls)
            if expire_delta_for is not None:
                mbx = _mailbox_by_address(state, expire_delta_for)
                expired = set(state["simulate"]["expire_delta"])
                expired.add(mbx["id"])
                state["simulate"]["expire_delta"] = sorted(expired)

    # -- reads used by the testkit -----------------------------------------

    def folder_name(self, state: State, mailbox_id: str, folder_id: str) -> str:
        folder = state["mailboxes"][mailbox_id]["folders"].get(folder_id)
        return "?" if folder is None else str(folder["displayName"])


def _empty() -> State:
    return {
        "mailboxes": {},
        "messages": {},
        "log": {},
        "seq": 0,
        "counter": 0,
        "simulate": {"throttle_calls": 0, "expire_delta": []},
    }


def _mailbox_by_address(state: State, address: str) -> dict[str, Any]:
    for mbx in state["mailboxes"].values():
        if mbx["address"] == address.lower():
            return dict(mbx)
    raise KeyError(f"no fixture mailbox {address}")


def _folder_id(mbx: dict[str, Any], folder: str) -> str:
    for fid, f in mbx["folders"].items():
        if folder in (f["wellKnownName"], f["displayName"], fid):
            return str(fid)
    raise KeyError(f"no folder {folder}")


def _entry(state: State, message_id: str) -> dict[str, Any]:
    """The live entry (not a copy): callers mutate it inside a write lock."""
    entry: dict[str, Any] | None = state["messages"].get(message_id)
    if entry is None or entry["deleted"]:
        raise KeyError("no such message")
    return entry


def _bump_change_key(entry: dict[str, Any]) -> None:
    prefix, _, n = entry["message"]["changeKey"].rpartition("-")
    entry["message"]["changeKey"] = f"{prefix}-{int(n) + 1}"


def _log(state: State, mailbox_id: str, folder_id: str, message_id: str, *, removed: bool) -> None:
    state["seq"] += 1
    state["log"].setdefault(f"{mailbox_id}/{folder_id}", []).append(
        {"seq": state["seq"], "id": message_id, "removed": removed}
    )


class FixtureGraphClient:
    """GraphClient over a FixtureMailStore. Honors simulated 429 and 410."""

    def __init__(self, store: FixtureMailStore) -> None:
        self.store = store

    def _maybe_throttle(self) -> None:
        # Raise only after the lock block has saved the decremented counter.
        with self.store._locked(write=True) as state:
            throttled = state["simulate"]["throttle_calls"] > 0
            if throttled:
                state["simulate"]["throttle_calls"] -= 1
        if throttled:
            raise GraphThrottled(THROTTLE_RETRY_AFTER)

    def list_mailboxes(self) -> list[MailboxInfo]:
        self._maybe_throttle()
        state = self.store.read()
        return [
            MailboxInfo(
                m["id"],
                m["address"],
                m["displayName"],
                m["recipientTypeDetails"],
                m["accountEnabled"],
            )
            for m in state["mailboxes"].values()
        ]

    def probe_mailbox(self, mailbox_id: str) -> None:
        self._maybe_throttle()
        mbx = self.store.read()["mailboxes"].get(mailbox_id)
        if mbx is None or not mbx.get("hasMailbox", True):
            raise GraphNotFound("no mailbox")
        if not mbx["exchangeAccess"]:
            raise GraphForbidden("outside the Exchange application RBAC scope")

    def list_folders(self, mailbox_id: str) -> list[FolderInfo]:
        self.probe_mailbox(mailbox_id)
        folders = self.store.read()["mailboxes"][mailbox_id]["folders"]
        return [FolderInfo(f["id"], f["displayName"], f["wellKnownName"]) for f in folders.values()]

    def message_delta(
        self, mailbox_id: str, folder_id: str, link: str | None, received_since: datetime
    ) -> DeltaPage:
        self.probe_mailbox(mailbox_id)
        with self.store._locked(write=True) as state:
            expired = link is not None and mailbox_id in state["simulate"]["expire_delta"]
            if expired:
                state["simulate"]["expire_delta"].remove(mailbox_id)
            current_seq = state["seq"]
        if expired:
            raise GraphDeltaExpired("delta token expired")
        state = self.store.read()
        # Links: "start:<snapshot seq>:<offset>" during the initial round,
        # "since:<seq>" afterwards (the delta link), "since:<seq>:<snap>:<offset>" mid-round.
        if link is None:
            changes = self._initial(state, mailbox_id, folder_id, received_since)
            mode, base, snap, offset = "start", 0, current_seq, 0
        else:
            parts = link.split(":")
            mode = parts[0]
            if mode == "start":
                snap, offset, base = int(parts[1]), int(parts[2]), 0
                changes = self._initial(state, mailbox_id, folder_id, received_since)
            elif mode == "since":
                base = int(parts[1])
                snap = int(parts[2]) if len(parts) > 2 else current_seq
                offset = int(parts[3]) if len(parts) > 3 else 0
                changes = self._since(state, mailbox_id, folder_id, base, snap, received_since)
            else:
                raise GraphDeltaExpired("unrecognized delta link")
        page = changes[offset : offset + DELTA_PAGE_SIZE]
        next_offset = offset + DELTA_PAGE_SIZE
        if next_offset < len(changes):
            nxt = (
                f"start:{snap}:{next_offset}"
                if mode == "start"
                else f"since:{base}:{snap}:{next_offset}"
            )
            return DeltaPage(tuple(page), next_link=nxt, delta_link=None)
        return DeltaPage(tuple(page), next_link=None, delta_link=f"since:{snap}")

    @staticmethod
    def _in_window(state: State, msg_id: str, received_since: datetime) -> bool:
        received = state["messages"][msg_id]["message"]["receivedDateTime"]
        return _parse_time(received) >= received_since

    def _initial(
        self, state: State, mailbox_id: str, folder_id: str, received_since: datetime
    ) -> list[DeltaChange]:
        return [
            DeltaChange(mid, removed=False)
            for mid, e in sorted(state["messages"].items())
            if e["mailbox_id"] == mailbox_id
            and not e["deleted"]
            and e["message"]["parentFolderId"] == folder_id
            and self._in_window(state, mid, received_since)
        ]

    def _since(
        self,
        state: State,
        mailbox_id: str,
        folder_id: str,
        base: int,
        snap: int,
        received_since: datetime,
    ) -> list[DeltaChange]:
        latest: dict[str, bool] = {}
        for item in state["log"].get(f"{mailbox_id}/{folder_id}", []):
            if base < item["seq"] <= snap:
                latest.pop(item["id"], None)
                latest[item["id"]] = item["removed"]
        return [
            DeltaChange(mid, removed)
            for mid, removed in latest.items()
            if removed or self._in_window(state, mid, received_since)
        ]

    def get_message(self, mailbox_id: str, message_id: str) -> dict[str, Any]:
        self.probe_mailbox(mailbox_id)
        entry = self.store.read()["messages"].get(message_id)
        if entry is None or entry["deleted"] or entry["mailbox_id"] != mailbox_id:
            raise GraphNotFound("message not found")
        return copy.deepcopy(dict(entry["message"]))
