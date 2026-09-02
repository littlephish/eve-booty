"""Pull character and corporation data from ESI into the local database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from .. import db
from ..config import ASSET_SAFETY_LOCATION_ID, Settings
from .client import ESIClient, ESIError

Progress = Callable[[str, int], None]

CHAR = "character"
CORP = "corporation"

# The wallet transactions route returns at most this many rows per call and has
# no page parameter; you rewind through history with from_id instead.
TX_PAGE_SIZE = 2500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has(scopes: list[str], scope: str) -> bool:
    return scope in scopes


class SyncError(RuntimeError):
    pass


class Syncer:
    def __init__(self, conn: sqlite3.Connection, client: ESIClient, settings: Settings):
        self.conn = conn
        self.client = client
        self.settings = settings

    # ------------------------------------------------------------ characters
    def register_character(self, character_id: int, name: str, scopes: list[str]) -> None:
        pub = self.client.get(f"/characters/{character_id}", allow_404=True) or {}
        corp_id = pub.get("corporation_id")
        corp_name = None
        if corp_id:
            corp = self.client.get(f"/corporations/{corp_id}", allow_404=True) or {}
            corp_name = corp.get("name")
            self.conn.execute(
                "INSERT INTO corporations(corporation_id, name, ticker) VALUES(?,?,?) "
                "ON CONFLICT(corporation_id) DO UPDATE SET name=excluded.name, "
                "ticker=excluded.ticker",
                (corp_id, corp_name, corp.get("ticker")),
            )
        self.conn.execute(
            """INSERT INTO characters(character_id, name, corporation_id, corporation_name,
                                      alliance_id, scopes, added_at, enabled)
               VALUES(?,?,?,?,?,?,?,1)
               ON CONFLICT(character_id) DO UPDATE SET
                 name=excluded.name, corporation_id=excluded.corporation_id,
                 corporation_name=excluded.corporation_name,
                 alliance_id=excluded.alliance_id, scopes=excluded.scopes""",
            (
                character_id, name or pub.get("name", ""), corp_id, corp_name,
                pub.get("alliance_id"), " ".join(scopes), _now(),
            ),
        )

    def enabled_characters(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM characters WHERE enabled=1 ORDER BY name"))

    # ------------------------------------------------------------------ main
    def sync_character(
        self,
        row: sqlite3.Row,
        progress: Progress | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[str]:
        """Pull everything we have scopes for. Returns a list of soft warnings.

        should_stop is polled between steps: a character with a lot of
        contracts can take minutes, and a cancel that only lands between
        characters is not much of a cancel."""
        cid = row["character_id"]
        scopes = (row["scopes"] or "").split()
        warnings: list[str] = []
        steps = [
            ("assets", "esi-assets.read_assets.v1", self._char_assets),
            ("wallet", "esi-wallet.read_character_wallet.v1", self._char_wallet),
            ("wallet journal", "esi-wallet.read_character_wallet.v1", self._char_journal),
            ("transactions", "esi-wallet.read_character_wallet.v1", self._char_transactions),
            ("market orders", "esi-markets.read_character_orders.v1", self._char_orders),
            ("contracts", "esi-contracts.read_character_contracts.v1", self._char_contracts),
            ("industry jobs", "esi-industry.read_character_jobs.v1", self._char_jobs),
            ("blueprints", "esi-characters.read_blueprints.v1", self._char_blueprints),
        ]
        for i, (label, scope, fn) in enumerate(steps):
            if should_stop is not None and should_stop():
                return warnings
            if progress:
                progress(f"{row['name']}: {label}", int(i * 100 / len(steps)))
            if not _has(scopes, scope):
                warnings.append(f"{row['name']}: skipped {label} (scope not granted)")
                continue
            try:
                fn(cid)
            except ESIError as exc:
                warnings.append(f"{row['name']}: {label} failed -- {exc}")

        if row["include_corp"] and row["corporation_id"]:
            warnings += self._sync_corp(row, scopes, progress)

        self.resolve_locations(CHAR, cid, cid)
        try:
            self.resolve_wallet_names(CHAR, cid)
        except ESIError as exc:
            warnings.append(f"{row['name']}: name lookup failed -- {exc}")
        self.conn.execute(
            "UPDATE characters SET last_sync_at=?, last_error=? WHERE character_id=?",
            (_now(), "\n".join(warnings) or None, cid),
        )
        if progress:
            progress(f"{row['name']}: done", 100)
        return warnings

    def _sync_corp(self, row, scopes, progress) -> list[str]:
        corp_id = row["corporation_id"]
        cid = row["character_id"]
        warnings: list[str] = []
        steps = [
            ("corp divisions", "esi-corporations.read_divisions.v1", self._corp_divisions),
            ("corp assets", "esi-assets.read_corporation_assets.v1", self._corp_assets),
            ("corp wallets", "esi-wallet.read_corporation_wallets.v1", self._corp_wallets),
            ("corp journal", "esi-wallet.read_corporation_wallets.v1", self._corp_journal),
            ("corp transactions", "esi-wallet.read_corporation_wallets.v1", self._corp_transactions),
            ("corp orders", "esi-markets.read_corporation_orders.v1", self._corp_orders),
            ("corp contracts", "esi-contracts.read_corporation_contracts.v1", self._corp_contracts),
            ("corp jobs", "esi-industry.read_corporation_jobs.v1", self._corp_jobs),
            ("corp blueprints", "esi-corporations.read_blueprints.v1", self._corp_blueprints),
            ("corp structures", "esi-corporations.read_structures.v1", self._corp_structures),
            # Needs esi-industry.read_corporation_mining.v1, which is not in
            # SCOPES yet -- see the note there about corp scopes and SSO. Until
            # it is added this skips with "scope not granted", which is the
            # same thing that happens for a character without the role.
            ("corp moon extractions", "esi-industry.read_corporation_mining.v1", self._corp_extractions),
        ]
        for label, scope, fn in steps:
            if progress:
                progress(f"{row['corporation_name'] or corp_id}: {label}", 50)
            if not _has(scopes, scope):
                warnings.append(f"corp: skipped {label} (scope not granted)")
                continue
            try:
                fn(corp_id, cid)
            except ESIError as exc:
                # 403 here almost always means the character lacks the in-game role
                warnings.append(f"corp {label} failed -- {exc}")
        self.conn.execute(
            "INSERT INTO corporations(corporation_id, via_character_id, last_sync_at) VALUES(?,?,?) "
            "ON CONFLICT(corporation_id) DO UPDATE SET via_character_id=excluded.via_character_id, "
            "last_sync_at=excluded.last_sync_at",
            (corp_id, cid, _now()),
        )
        self.resolve_locations(CORP, corp_id, cid)
        try:
            self.resolve_wallet_names(CORP, corp_id)
        except ESIError as exc:
            warnings.append(f"corp name lookup failed -- {exc}")
        return warnings

    # ----------------------------------------------------------------- assets
    ASSET_COLS = [
        "owner_type", "owner_id", "item_id", "type_id", "quantity", "location_id",
        "location_flag", "location_type", "is_singleton", "is_blueprint_copy", "custom_name",
    ]

    def _store_assets(self, owner_type: str, owner_id: int, items: list[dict], names: dict) -> None:
        rows = [
            (
                owner_type, owner_id, it["item_id"], it["type_id"], it.get("quantity", 1),
                it["location_id"], it.get("location_flag"), it.get("location_type"),
                int(bool(it.get("is_singleton"))), int(bool(it.get("is_blueprint_copy"))),
                names.get(it["item_id"]),
            )
            for it in items
        ]
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM assets WHERE owner_type=? AND owner_id=?", (owner_type, owner_id)
            )
            db.upsert_many(self.conn, "assets", self.ASSET_COLS, rows)

    def _fetch_names(self, path: str, ids: list[int], character_id: int) -> dict:
        if not ids:
            return {}
        try:
            got = self.client.post_chunked(path, ids, character_id=character_id, allow_403=True)
        except ESIError:
            return {}
        return {
            r["item_id"]: r.get("name")
            for r in (got or [])
            if r.get("name") and r["name"] != "None"
        }

    def _char_assets(self, cid: int) -> None:
        items = self.client.all_pages(f"/characters/{cid}/assets", character_id=cid)
        # Only assembled/singleton items can carry a custom name.
        named = [it["item_id"] for it in items if it.get("is_singleton")]
        names = self._fetch_names(f"/characters/{cid}/assets/names", named, cid)
        self._store_assets(CHAR, cid, items, names)

    def _corp_assets(self, corp_id: int, via: int) -> None:
        items = self.client.all_pages(f"/corporations/{corp_id}/assets", character_id=via)
        named = [it["item_id"] for it in items if it.get("is_singleton")]
        names = self._fetch_names(f"/corporations/{corp_id}/assets/names", named, via)
        self._store_assets(CORP, corp_id, items, names)

    # ---------------------------------------------------------------- wallets
    def _char_wallet(self, cid: int) -> None:
        balance = self.client.get(f"/characters/{cid}/wallet", character_id=cid)
        self.conn.execute(
            "INSERT OR REPLACE INTO wallets(owner_type, owner_id, division, balance) "
            "VALUES(?,?,1,?)",
            (CHAR, cid, float(balance or 0)),
        )

    def _corp_wallets(self, corp_id: int, via: int) -> None:
        got = self.client.get(f"/corporations/{corp_id}/wallets", character_id=via) or []
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM wallets WHERE owner_type=? AND owner_id=?", (CORP, corp_id)
            )
            db.upsert_many(
                self.conn, "wallets", ["owner_type", "owner_id", "division", "balance"],
                [(CORP, corp_id, w["division"], float(w.get("balance") or 0)) for w in got],
            )

    def _corp_divisions(self, corp_id: int, via: int) -> None:
        got = self.client.get(f"/corporations/{corp_id}/divisions", character_id=via) or {}
        rows = []
        for kind in ("hangar", "wallet"):
            for d in got.get(kind, []):
                rows.append((corp_id, kind, d["division"], d.get("name")))
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM corp_divisions WHERE corporation_id=?", (corp_id,))
            db.upsert_many(
                self.conn, "corp_divisions",
                ["corporation_id", "kind", "division", "name"], rows,
            )

    # --------------------------------------------------------- wallet history
    # Journal and transactions are the one place we accumulate rather than
    # replace. ESI serves roughly the last 30 days and at most 2500 rows, so
    # the local table outgrows what ESI can still tell you. INSERT OR IGNORE on
    # the natural key means re-syncing is free and never loses old rows.
    JOURNAL_COLS = [
        "owner_type", "owner_id", "division", "entry_id", "date", "ref_type", "amount",
        "balance", "description", "reason", "first_party_id", "second_party_id",
        "context_id", "context_id_type", "tax", "tax_receiver_id",
    ]
    TX_COLS = [
        "owner_type", "owner_id", "division", "transaction_id", "date", "type_id",
        "quantity", "unit_price", "is_buy", "is_personal", "client_id", "location_id",
        "journal_ref_id",
    ]

    def _append(self, table: str, columns: list[str], rows: list[tuple]) -> int:
        if not rows:
            return 0
        placeholders = ",".join("?" * len(columns))
        sql = (
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
        )
        with db.transaction(self.conn):
            before = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.conn.executemany(sql, rows)
            after = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return after - before

    def _store_journal(self, owner_type, owner_id, division, entries) -> int:
        rows = [
            (
                owner_type, owner_id, division, e["id"], e["date"], e.get("ref_type"),
                e.get("amount"), e.get("balance"), e.get("description"), e.get("reason"),
                e.get("first_party_id"), e.get("second_party_id"), e.get("context_id"),
                e.get("context_id_type"), e.get("tax"), e.get("tax_receiver_id"),
            )
            for e in entries
        ]
        return self._append("wallet_journal", self.JOURNAL_COLS, rows)

    def _store_transactions(self, owner_type, owner_id, division, entries) -> int:
        rows = [
            (
                owner_type, owner_id, division, t["transaction_id"], t["date"],
                t["type_id"], t.get("quantity", 0), t.get("unit_price", 0.0),
                int(bool(t.get("is_buy"))),
                None if t.get("is_personal") is None else int(bool(t["is_personal"])),
                t.get("client_id"), t.get("location_id"), t.get("journal_ref_id"),
            )
            for t in entries
        ]
        return self._append("wallet_transactions", self.TX_COLS, rows)

    def _walk_transactions(self, path: str, via: int, known: set[int], max_pages: int = 20) -> list:
        """Transactions are not paged; you rewind with from_id.

        Each call returns up to TX_PAGE_SIZE rows newest first. Pass from_id
        set to the oldest id you have seen to get the batch before it. Stop
        once a batch is entirely rows we already stored -- on a routine sync
        that is the first batch, so this costs one call.
        """
        collected: list[dict] = []
        from_id: int | None = None
        for _ in range(max_pages):
            params = {"from_id": from_id} if from_id is not None else None
            batch = self.client.get(path, character_id=via, params=params) or []
            if not batch:
                break
            fresh = [t for t in batch if t["transaction_id"] not in known]
            collected.extend(fresh)
            if not fresh:
                break
            from_id = min(t["transaction_id"] for t in batch)
            if len(batch) < TX_PAGE_SIZE:
                break
        return collected

    def _known_transaction_ids(self, owner_type, owner_id, division) -> set[int]:
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT transaction_id FROM wallet_transactions "
                "WHERE owner_type=? AND owner_id=? AND division=?",
                (owner_type, owner_id, division),
            )
        }

    def _char_journal(self, cid: int) -> None:
        entries = self.client.all_pages(f"/characters/{cid}/wallet/journal", character_id=cid)
        self._store_journal(CHAR, cid, 1, entries)

    def _char_transactions(self, cid: int) -> None:
        known = self._known_transaction_ids(CHAR, cid, 1)
        entries = self._walk_transactions(
            f"/characters/{cid}/wallet/transactions", cid, known
        )
        self._store_transactions(CHAR, cid, 1, entries)

    def _corp_divisions_list(self, corp_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT division FROM corp_divisions WHERE corporation_id=? AND kind='wallet' "
            "ORDER BY division",
            (corp_id,),
        ).fetchall()
        return [r[0] for r in rows] or [1]

    def _corp_journal(self, corp_id: int, via: int) -> None:
        for division in self._corp_divisions_list(corp_id):
            entries = self.client.all_pages(
                f"/corporations/{corp_id}/wallets/{division}/journal", character_id=via
            )
            self._store_journal(CORP, corp_id, division, entries)

    def _corp_transactions(self, corp_id: int, via: int) -> None:
        for division in self._corp_divisions_list(corp_id):
            known = self._known_transaction_ids(CORP, corp_id, division)
            entries = self._walk_transactions(
                f"/corporations/{corp_id}/wallets/{division}/transactions", via, known
            )
            self._store_transactions(CORP, corp_id, division, entries)

    # ------------------------------------------------------------------ names
    def resolve_names(self, ids: set[int]) -> None:
        """Turn journal counterparty ids into names via /universe/names.

        Public endpoint, 1000 ids per call. It rejects the whole batch if any
        id is unresolvable, so a failed chunk is retried in halves rather than
        losing the good ids alongside the bad one.
        """
        ids = {i for i in ids if i and i > 0}
        if not ids:
            return
        have = {
            r[0] for r in self.conn.execute("SELECT id FROM names")
        }
        todo = sorted(ids - have)
        resolved: list[tuple] = []

        def fetch(chunk: list[int]) -> None:
            if not chunk:
                return
            try:
                got = self.client.post("/universe/names", chunk, allow_404=True)
            except ESIError:
                got = None
            if got is None:
                if len(chunk) == 1:
                    return  # a single id nobody can resolve; skip it
                mid = len(chunk) // 2
                fetch(chunk[:mid])
                fetch(chunk[mid:])
                return
            for r in got:
                resolved.append((r["id"], r["name"], r.get("category"), _now()))

        for i in range(0, len(todo), 1000):
            fetch(todo[i : i + 1000])

        if resolved:
            with db.transaction(self.conn):
                db.upsert_many(
                    self.conn, "names", ["id", "name", "category", "updated_at"], resolved
                )

    def resolve_wallet_names(self, owner_type: str, owner_id: int) -> None:
        rows = self.conn.execute(
            """SELECT first_party_id AS a, second_party_id AS b FROM wallet_journal
               WHERE owner_type=? AND owner_id=?""",
            (owner_type, owner_id),
        ).fetchall()
        wanted = {r["a"] for r in rows} | {r["b"] for r in rows}
        wanted |= {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT client_id FROM wallet_transactions "
                "WHERE owner_type=? AND owner_id=?",
                (owner_type, owner_id),
            )
        }
        self.resolve_names(wanted)

    # ----------------------------------------------------------------- orders
    ORDER_COLS = [
        "owner_type", "owner_id", "order_id", "type_id", "location_id", "region_id",
        "is_buy_order", "price", "volume_remain", "volume_total", "escrow", "issued",
    ]

    def _store_orders(self, owner_type: str, owner_id: int, orders: list[dict]) -> None:
        rows = [
            (
                owner_type, owner_id, o["order_id"], o["type_id"], o.get("location_id"),
                o.get("region_id"), int(bool(o.get("is_buy_order"))), o.get("price"),
                o.get("volume_remain"), o.get("volume_total"), o.get("escrow") or 0.0,
                o.get("issued"),
            )
            for o in orders
        ]
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM market_orders WHERE owner_type=? AND owner_id=?",
                (owner_type, owner_id),
            )
            db.upsert_many(self.conn, "market_orders", self.ORDER_COLS, rows)

    def _char_orders(self, cid: int) -> None:
        self._store_orders(CHAR, cid, self.client.get(f"/characters/{cid}/orders", character_id=cid) or [])

    def _corp_orders(self, corp_id: int, via: int) -> None:
        self._store_orders(
            CORP, corp_id, self.client.all_pages(f"/corporations/{corp_id}/orders", character_id=via)
        )

    # -------------------------------------------------------------- contracts
    CONTRACT_COLS = [
        "owner_type", "owner_id", "contract_id", "type", "status", "issuer_id",
        "issuer_corporation_id", "assignee_id", "for_corporation", "availability", "price",
        "reward", "collateral", "volume", "start_location_id", "end_location_id",
        "date_issued", "date_expired", "title",
    ]

    def _store_contracts(self, owner_type, owner_id, contracts, path_prefix, via) -> None:
        rows = [
            (
                owner_type, owner_id, c["contract_id"], c.get("type"), c.get("status"),
                c.get("issuer_id"), c.get("issuer_corporation_id"), c.get("assignee_id"),
                int(bool(c.get("for_corporation"))), c.get("availability"), c.get("price"),
                c.get("reward"), c.get("collateral"), c.get("volume"),
                c.get("start_location_id"), c.get("end_location_id"),
                c.get("date_issued"), c.get("date_expired"), c.get("title"),
            )
            for c in contracts
        ]
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM contracts WHERE owner_type=? AND owner_id=?", (owner_type, owner_id)
            )
            db.upsert_many(self.conn, "contracts", self.CONTRACT_COLS, rows)

        # Items are only worth pulling for contracts that still hold our stuff.
        outstanding = [
            c for c in contracts
            if c.get("status") in ("outstanding", "in_progress")
            and c.get("type") in ("item_exchange", "auction", "courier")
        ]
        item_rows = []
        for c in outstanding:
            try:
                items = self.client.get(
                    f"{path_prefix}/contracts/{c['contract_id']}/items",
                    character_id=via, allow_404=True, allow_403=True,
                ) or []
            except ESIError:
                continue
            for it in items:
                item_rows.append((
                    c["contract_id"], it.get("record_id"), it["type_id"],
                    it.get("quantity", 1), int(bool(it.get("is_included", True))),
                    int(bool(it.get("is_singleton"))),
                ))
        if item_rows:
            db.upsert_many(
                self.conn, "contract_items",
                ["contract_id", "record_id", "type_id", "quantity", "is_included", "is_singleton"],
                item_rows,
            )

    def _char_contracts(self, cid: int) -> None:
        got = self.client.all_pages(f"/characters/{cid}/contracts", character_id=cid)
        self._store_contracts(CHAR, cid, got, f"/characters/{cid}", cid)

    def _corp_contracts(self, corp_id: int, via: int) -> None:
        got = self.client.all_pages(f"/corporations/{corp_id}/contracts", character_id=via)
        self._store_contracts(CORP, corp_id, got, f"/corporations/{corp_id}", via)

    # ---------------------------------------------------------- industry jobs
    JOB_COLS = [
        "owner_type", "owner_id", "job_id", "installer_id", "activity_id",
        "blueprint_type_id", "blueprint_location_id", "output_location_id", "facility_id",
        "product_type_id", "runs", "licensed_runs", "cost", "status", "start_date", "end_date",
    ]

    def _store_jobs(self, owner_type, owner_id, jobs) -> None:
        rows = [
            (
                owner_type, owner_id, j["job_id"], j.get("installer_id"), j.get("activity_id"),
                j.get("blueprint_type_id"), j.get("blueprint_location_id"),
                j.get("output_location_id"), j.get("facility_id"), j.get("product_type_id"),
                j.get("runs"), j.get("licensed_runs"), j.get("cost"), j.get("status"),
                j.get("start_date"), j.get("end_date"),
            )
            for j in jobs
        ]
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM industry_jobs WHERE owner_type=? AND owner_id=?",
                (owner_type, owner_id),
            )
            db.upsert_many(self.conn, "industry_jobs", self.JOB_COLS, rows)

    def _char_jobs(self, cid: int) -> None:
        got = self.client.get(
            f"/characters/{cid}/industry/jobs", character_id=cid,
            params={"include_completed": "true"},
        ) or []
        self._store_jobs(CHAR, cid, got)

    def _corp_jobs(self, corp_id: int, via: int) -> None:
        got = self.client.all_pages(
            f"/corporations/{corp_id}/industry/jobs", character_id=via,
            params={"include_completed": "true"},
        )
        self._store_jobs(CORP, corp_id, got)

    # ------------------------------------------------------------- blueprints
    BP_COLS = [
        "owner_type", "owner_id", "item_id", "type_id", "location_id", "location_flag",
        "quantity", "material_efficiency", "time_efficiency", "runs",
    ]

    def _store_blueprints(self, owner_type, owner_id, bps) -> None:
        rows = [
            (
                owner_type, owner_id, b["item_id"], b["type_id"], b.get("location_id"),
                b.get("location_flag"), b.get("quantity"), b.get("material_efficiency"),
                b.get("time_efficiency"), b.get("runs"),
            )
            for b in bps
        ]
        with db.transaction(self.conn):
            self.conn.execute(
                "DELETE FROM blueprints WHERE owner_type=? AND owner_id=?", (owner_type, owner_id)
            )
            db.upsert_many(self.conn, "blueprints", self.BP_COLS, rows)

    def _char_blueprints(self, cid: int) -> None:
        self._store_blueprints(CHAR, cid, self.client.all_pages(f"/characters/{cid}/blueprints", character_id=cid))

    def _corp_blueprints(self, corp_id: int, via: int) -> None:
        self._store_blueprints(
            CORP, corp_id,
            self.client.all_pages(f"/corporations/{corp_id}/blueprints", character_id=via),
        )

    # ------------------------------------------------------------- structures
    STRUCTURE_COLS = [
        "structure_id", "name", "system_id", "region_id", "type_id", "owner_id",
        "resolved_at", "accessible", "owned", "state", "state_timer_start",
        "state_timer_end", "fuel_expires", "reinforce_hour", "next_reinforce_hour",
        "next_reinforce_apply", "unanchors_at", "services", "updated_at",
    ]

    def _corp_structures(self, corp_id: int, via: int) -> None:
        """Everything ESI reports about our own structures.

        This used to keep the name and the location and drop the rest on the
        floor, because all anything wanted was somewhere to look up "what is
        structure 1035466617946". The fuel clock, the reinforcement state and
        its timer were all in the same response the whole time.

        Timestamps go in exactly as sent -- ISO 8601, UTC, unparsed. EVE quotes
        every timer in UTC, so this is the format people actually read.
        """
        got = self.client.all_pages(f"/corporations/{corp_id}/structures", character_id=via)
        now = _now()
        rows = []
        for s in got:
            sys_id = s.get("system_id")
            region = self.conn.execute(
                "SELECT region_id FROM sde_systems WHERE system_id=?", (sys_id,)
            ).fetchone()
            rows.append((
                s["structure_id"], s.get("name"), sys_id,
                region["region_id"] if region else None, s.get("type_id"), corp_id,
                now, 1, 1,
                s.get("state"), s.get("state_timer_start"), s.get("state_timer_end"),
                s.get("fuel_expires"), s.get("reinforce_hour"), s.get("next_reinforce_hour"),
                s.get("next_reinforce_apply"), s.get("unanchors_at"),
                json.dumps(s.get("services") or []), now,
            ))
        db.upsert_many(self.conn, "structures", self.STRUCTURE_COLS, rows)
        self._mark_unanchored(corp_id, [s["structure_id"] for s in got])

    def _mark_unanchored(self, corp_id: int, seen: list[int]) -> None:
        """Flag structures ESI no longer reports for this corp.

        An unanchored structure simply stops appearing in the response. Left
        alone the row stays owned=1 forever, still claiming whatever state and
        fuel_expires it had on the day it went away -- and a fuel clock frozen
        in the past reads as "out of fuel" rather than "gone", which is the
        same mistake _corp_extractions already refuses to make with stale
        chunk timers.

        Marked, not deleted: this table is also how an asset sitting in that
        structure gets a location name (ASSET_ROWS joins it), so deleting the
        row would rename somebody's hangar to "Unknown location 1048...".

        Nothing is marked when the response is empty. "This corp owns no
        structures" and "ESI handed back an empty page this once" are
        indistinguishable from here, and wrongly flagging an entire corp's
        structures is far more expensive than leaving a genuinely emptied one
        listed until someone notices.
        """
        if not seen:
            return
        placeholders = ",".join("?" * len(seen))
        self.conn.execute(
            f"""UPDATE structures
                   SET gone_at = ?
                 WHERE owned = 1
                   AND owner_id = ?
                   AND gone_at IS NULL
                   AND structure_id NOT IN ({placeholders})""",
            (_now(), corp_id, *seen),
        )
        # Anything still reported had gone_at cleared for free: upsert_many is
        # INSERT OR REPLACE, which rewrites the whole row, and gone_at is not
        # in STRUCTURE_COLS -- so a structure that comes back (an unanchor
        # cancelled, a bad sync) returns to NULL without a second statement.

    def _corp_extractions(self, corp_id: int, via: int) -> None:
        """Moon drill cycles. ESI reports only the current cycle per drill, so
        rows for drills that have since stopped are cleared rather than left
        to go stale -- a chunk_arrival_time from three cycles ago reads as an
        imminent timer, which is exactly the mistake worth not making."""
        got = self.client.all_pages(
            f"/corporation/{corp_id}/mining/extractions", character_id=via
        )
        now = _now()
        rows = [
            (
                e["structure_id"], e.get("moon_id"), corp_id,
                e.get("extraction_start_time"), e.get("chunk_arrival_time"),
                e.get("natural_decay_time"), now,
            )
            for e in got
        ]
        self.conn.execute("DELETE FROM moon_extractions WHERE owner_id=?", (corp_id,))
        db.upsert_many(
            self.conn, "moon_extractions",
            ["structure_id", "moon_id", "owner_id", "extraction_start_time",
             "chunk_arrival_time", "natural_decay_time", "updated_at"],
            rows,
        )

    def resolve_structure(self, structure_id: int, via: int) -> None:
        """One /universe/structures call. 403 means no docking access -- record
        that so we stop asking."""
        existing = self.conn.execute(
            "SELECT name, accessible FROM structures WHERE structure_id=?", (structure_id,)
        ).fetchone()
        if existing and (existing["name"] or not existing["accessible"]):
            return
        try:
            got = self.client.get(
                f"/universe/structures/{structure_id}", character_id=via,
                allow_403=True, allow_404=True,
            )
        except ESIError:
            got = None
        if got is None:
            self.conn.execute(
                "INSERT INTO structures(structure_id, accessible, resolved_at) VALUES(?,0,?) "
                "ON CONFLICT(structure_id) DO UPDATE SET accessible=0, resolved_at=excluded.resolved_at",
                (structure_id, _now()),
            )
            return
        sys_id = got.get("solar_system_id")
        region = self.conn.execute(
            "SELECT region_id FROM sde_systems WHERE system_id=?", (sys_id,)
        ).fetchone()
        self.conn.execute(
            """INSERT INTO structures(structure_id, name, system_id, region_id, type_id,
                                      owner_id, resolved_at, accessible)
               VALUES(?,?,?,?,?,?,?,1)
               ON CONFLICT(structure_id) DO UPDATE SET
                 name=excluded.name, system_id=excluded.system_id,
                 region_id=excluded.region_id, type_id=excluded.type_id,
                 owner_id=excluded.owner_id, resolved_at=excluded.resolved_at, accessible=1""",
            (
                structure_id, got.get("name"), sys_id,
                region["region_id"] if region else None, got.get("type_id"),
                got.get("owner_id"), _now(),
            ),
        )

    # -------------------------------------------------------- location resolve
    def resolve_locations(self, owner_type: str, owner_id: int, via: int) -> None:
        """Flatten the container tree.

        ESI gives every asset a location_id that may point at a station, a
        solar system, or another asset (a can, a ship's cargo, a ship inside a
        ship). Walk up until we hit something that is not one of our own
        items, then attach the station/structure/system it sits in.
        """
        rows = self.conn.execute(
            "SELECT item_id, location_id, location_type FROM assets "
            "WHERE owner_type=? AND owner_id=?",
            (owner_type, owner_id),
        ).fetchall()
        if not rows:
            return
        parent = {r["item_id"]: r["location_id"] for r in rows}

        roots: dict[int, int] = {}
        for item_id in parent:
            seen = set()
            cur = item_id
            while cur in parent and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
            roots[item_id] = cur

        stations = {
            r["station_id"]: (r["system_id"], r["region_id"])
            for r in self.conn.execute("SELECT station_id, system_id, region_id FROM sde_stations")
        }
        systems = {
            r["system_id"]: r["region_id"]
            for r in self.conn.execute("SELECT system_id, region_id FROM sde_systems")
        }

        # Anything left that is still one of our own item ids means the tree
        # had a cycle. Those are not structures -- do not go asking ESI about
        # them, just leave them unresolved. Asset Safety is excluded the same
        # way: 2004 is not a real structure id, it is ESI's fixed constant for
        # "sitting in Asset Safety" (see ASSET_SAFETY_LOCATION_ID), and asking
        # /universe/structures/2004 about it would just burn an ESI call for a
        # guaranteed 404, every sync, forever.
        unknown = {
            loc for loc in set(roots.values())
            if loc not in stations
            and loc not in systems
            and loc not in parent
            and loc != ASSET_SAFETY_LOCATION_ID
        }
        if self.client is not None:
            for loc in unknown:
                self.resolve_structure(loc, via)
        structures = {
            r["structure_id"]: (r["system_id"], r["region_id"])
            for r in self.conn.execute("SELECT structure_id, system_id, region_id FROM structures")
        }

        updates = []
        for item_id, root in roots.items():
            if root in stations:
                sys_id, region_id = stations[root]
            elif root in systems:
                sys_id, region_id = root, systems[root]
            elif root in structures:
                sys_id, region_id = structures[root]
            else:
                sys_id = region_id = None
            updates.append((root, sys_id, region_id, owner_type, owner_id, item_id))

        with db.transaction(self.conn):
            self.conn.executemany(
                "UPDATE assets SET root_location_id=?, system_id=?, region_id=? "
                "WHERE owner_type=? AND owner_id=? AND item_id=?",
                updates,
            )
