"""Real, known-IP HAP client for the isolated lab (aiohomekit 4.0.1 only).

No discovery browser is started. The caller supplies the lab IPv4 address and a
PIN obtained from HA. Pairing secrets belong in the private pairing volume, never
the evidence directory. Journal ``event`` records come from decrypted EVENT
messages; aiohomekit's optimistic write callbacks are deliberately excluded.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller import Controller
from aiohomekit.controller.abstract import TransportType
from aiohomekit.controller.ip.controller import IpController
from aiohomekit.controller.ip.discovery import IpDiscovery
from aiohomekit.controller.ip.pairing import IpPairing
from aiohomekit.model.categories import Categories
from aiohomekit.model.characteristics import CharacteristicsTypes
from aiohomekit.model.feature_flags import FeatureFlags
from aiohomekit.model.services import ServicesTypes
from aiohomekit.model.status_flags import StatusFlags
from aiohomekit.uuid import normalize_uuid
from aiohomekit.zeroconf import HAP_TYPE_TCP, HomeKitService


class HAPError(RuntimeError):
    """A HAP operation failed or its required evidence was missing."""


@dataclass(frozen=True)
class CharacteristicRef:
    """An address discovered from the actual accessory database."""

    aid: int
    iid: int
    name: str
    type: str
    service_type: str
    permissions: tuple[str, ...]

    @property
    def key(self) -> tuple[int, int]:
        return self.aid, self.iid


def _new_controller() -> tuple[Controller, IpController]:
    # The pinned IP backend needs no discovery state to load a known endpoint.
    # Do not call async_start: it registers multicast discovery (and other
    # transports). Explicit shutdown below owns every pairing we instantiate.
    cache = CharacteristicCacheMemory()
    controller = Controller(char_cache=cache)
    backend = IpController(char_cache=cache, zeroconf_instance=None)
    controller.transports[TransportType.IP] = backend
    return controller, backend


async def _close_controller(controller: Controller) -> None:
    pairings = set(controller.pairings.values())
    for backend in controller.transports.values():
        pairings.update(backend.pairings.values())
    try:
        for pairing in pairings:
            await pairing.shutdown()
    finally:
        await controller.async_stop()


def _save_pairing(path: Path, alias: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            # mkstemp uses 0600; no keys or PIN are included in the journal.
            json.dump({alias: data}, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_saved_endpoint(path: Path, alias: str, host: str, port: int) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {alias}:
        raise HAPError("Pairing file must contain exactly the requested lab alias")
    pairing = data[alias]
    if not isinstance(pairing, dict) or (
        pairing.get("Connection") != "IP"
        or pairing.get("AccessoryIP") != host
        or pairing.get("AccessoryIPs") != [host]
        or pairing.get("AccessoryPort") != port
    ):
        raise HAPError("Saved pairing endpoint differs from the requested lab endpoint")


def _pairing_exists(path: Path, journal: Path | None) -> bool:
    if journal is not None and journal.resolve() == path.resolve():
        raise ValueError("Pairing secrets and evidence must use separate files")
    return path.exists()


def _type_uuid(value: str, *, service: bool = False) -> str:
    constants = ServicesTypes if service else CharacteristicsTypes
    symbol = value.upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "CURRENT_POSITION": "POSITION_CURRENT",
        "TARGET_POSITION": "POSITION_TARGET",
        "HOLD_POSITION": "POSITION_HOLD",
    }
    return normalize_uuid(getattr(constants, aliases.get(symbol, symbol), value))


class HAPClient:
    """Pair once, then prove a fresh client can use the persisted credentials."""

    def __init__(
        self,
        controller: Controller,
        pairing: IpPairing,
        *,
        journal_path: Path | None,
        timeout: float,
    ) -> None:
        self._controller = controller
        self._pairing = pairing
        self._journal_path = journal_path
        self._timeout = timeout
        self._records: list[dict[str, Any]] = []
        self._session = uuid4().hex
        self._accessories: list[dict[str, Any]] = []
        self._changed = asyncio.Event()
        self._closed = False
        self._journal_error: OSError | None = None
        self._original_event_received = pairing.event_received
        pairing.event_received = self._wire_event

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        pin: str | None,
        pairing_path: str | Path,
        *,
        alias: str = "ha-operator-lab",
        journal_path: str | Path | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 - enforced by asyncio.timeout below
    ) -> HAPClient:
        """Pair or reload, and perform an encrypted characteristic read.

        ``host`` must be a literal private IPv4 address; network isolation is the
        host lab controller's responsibility. Existing credentials are never
        silently discarded or re-paired after an authentication failure.
        """
        if version("aiohomekit") != "4.0.1":
            raise HAPError("This helper requires the locked aiohomekit==4.0.1 API")
        address = ipaddress.IPv4Address(host)
        if not address.is_private or (
            address.is_loopback or address.is_link_local or address.is_unspecified
        ):
            raise ValueError("HAP requires the isolated lab's private IPv4 address")
        if not 0 < port < 65536 or timeout <= 0 or not alias:
            raise ValueError("A valid port, positive timeout, and alias are required")
        path = Path(pairing_path)
        journal = Path(journal_path) if journal_path is not None else None
        async with asyncio.timeout(timeout):
            if not await asyncio.to_thread(_pairing_exists, path, journal):
                if pin is None:
                    raise HAPError("A PIN is required for the first pairing")
                await cls._pair(host, port, pin, path, alias)
            _validate_saved_endpoint(path, alias, host, port)
            controller, _ = _new_controller()
            client = None
            try:
                # This is intentionally a new controller after initial shutdown.
                # IpDiscovery populates backend.pairings, not Controller.aliases;
                # serializing its returned pairing avoids 4.0.1's save_data trap.
                controller.load_data(str(path))
                pairing = controller.aliases.get(alias)
                if not isinstance(pairing, IpPairing):
                    raise HAPError("Persisted IP pairing was not loaded")
                client = cls(controller, pairing, journal_path=journal, timeout=timeout)
                accessories = await client.list_accessories()
                readable = next(
                    (
                        (item["aid"], char["iid"])
                        for item in accessories
                        for service in item["services"]
                        for char in service["characteristics"]
                        if "pr" in char.get("perms", [])
                    ),
                    None,
                )
                if readable is None:
                    raise HAPError("No readable characteristic in the accessory database")
                await client.read(readable)
                client._record("pairing_reloaded", secure=True)
                client._check()
                return client
            except BaseException:
                if client is not None:
                    await client.close()
                else:
                    await _close_controller(controller)
                raise

    @staticmethod
    async def _pair(host: str, port: int, pin: str, path: Path, alias: str) -> None:
        controller, backend = _new_controller()
        discovery = IpDiscovery(
            backend,
            HomeKitService(
                name="HA Operator isolated lab",
                # Endpoint metadata only. SRP returns the real accessory identity.
                id="00:00:00:00:00:00",
                model="Home Assistant Bridge",
                feature_flags=FeatureFlags(0),
                status_flags=StatusFlags.UNPAIRED,
                config_num=1,
                state_num=1,
                category=Categories.BRIDGE,
                protocol_version="1.1",
                type=HAP_TYPE_TCP,
                address=host,
                addresses=[host],
                port=port,
            ),
        )
        try:
            finish_pairing = await discovery.async_start_pairing(alias)
            pairing = await finish_pairing(pin)
            _save_pairing(path, alias, pairing.pairing_data)
        finally:
            try:
                await discovery.close()
            finally:
                await _close_controller(controller)

    @property
    def sequence(self) -> int:
        return len(self._records)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Copies of genuine wire event records, excluding local write echoes."""
        return tuple(copy.deepcopy(row) for row in self._records if row["kind"] == "event")

    @property
    def journal(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._records))

    def _record(self, kind: str, **fields: Any) -> None:
        row = {
            "session": self._session,
            "sequence": self.sequence + 1,
            "monotonic": time.monotonic(),
            "kind": kind,
            **copy.deepcopy(fields),
        }
        self._records.append(row)
        if self._journal_path is not None and self._journal_error is None:
            try:
                self._journal_path.parent.mkdir(parents=True, exist_ok=True)
                with self._journal_path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(row, sort_keys=True) + "\n")
            except OSError as error:
                self._journal_error = error
        self._changed.set()

    def _wire_event(self, payload: dict[str, Any]) -> None:
        # Record before the library mutates characteristic rows during formatting.
        self._record(
            "event",
            secure=bool(self._pairing.connection.is_secure),
            characteristics=payload.get("characteristics", []),
        )
        self._original_event_received(payload)

    def _check(self) -> None:
        if self._journal_error is not None:
            raise HAPError("HAP evidence journal could not be written") from self._journal_error
        if self._closed:
            raise HAPError("HAP client is closed")

    def _assert_secure(self) -> None:
        self._check()
        if not self._pairing.is_connected or not self._pairing.connection.is_secure:
            raise HAPError("HAP operation did not use a verified encrypted connection")

    @staticmethod
    def _key(ref: CharacteristicRef | tuple[int, int]) -> tuple[int, int]:
        return ref.key if isinstance(ref, CharacteristicRef) else ref

    @staticmethod
    def _assert_success(result: dict[tuple[int, int], dict[str, Any]]) -> None:
        failed = {key: row.get("status") for key, row in result.items() if row.get("status", 0)}
        if failed:
            raise HAPError(f"HAP characteristic operation failed: {failed}")

    async def list_accessories(self) -> list[dict[str, Any]]:
        self._check()
        async with asyncio.timeout(self._timeout):
            self._accessories = await self._pairing.list_accessories_and_characteristics()
        self._assert_secure()
        self._record("accessories", accessories=self._accessories, secure=True)
        self._check()
        return copy.deepcopy(self._accessories)

    def characteristic(
        self, name: str, type: str, *, service_type: str | None = None
    ) -> CharacteristicRef:
        """Resolve an exact accessory/service name and UUID or constant name.

        Examples: ``characteristic("Window", "TARGET_POSITION")`` or
        ``characteristic("Extractor", "25")``. Ambiguity fails instead of
        guessing a characteristic IID.
        """
        expected = _type_uuid(type)
        expected_service = _type_uuid(service_type, service=True) if service_type else None
        matches = []
        name_types = {CharacteristicsTypes.NAME, CharacteristicsTypes.CONFIGURED_NAME}
        for accessory in self._accessories:
            accessory_names = {
                char.get("value")
                for service in accessory["services"]
                if normalize_uuid(service["type"]) == ServicesTypes.ACCESSORY_INFORMATION
                for char in service["characteristics"]
                if normalize_uuid(char["type"]) in name_types
            }
            for service in accessory["services"]:
                actual_service = normalize_uuid(service["type"])
                if expected_service is not None and actual_service != expected_service:
                    continue
                names = accessory_names | {
                    char.get("value")
                    for char in service["characteristics"]
                    if normalize_uuid(char["type"]) in name_types
                }
                if name not in names:
                    continue
                for char in service["characteristics"]:
                    if normalize_uuid(char["type"]) == expected:
                        matches.append(
                            CharacteristicRef(
                                accessory["aid"],
                                char["iid"],
                                name,
                                expected,
                                actual_service,
                                tuple(char.get("perms", [])),
                            )
                        )
        if len(matches) != 1:
            raise HAPError(f"Expected one {type} characteristic for {name!r}; found {len(matches)}")
        return matches[0]

    async def read(self, ref: CharacteristicRef | tuple[int, int]) -> Any:
        self._check()
        key = self._key(ref)
        async with asyncio.timeout(self._timeout):
            result = await self._pairing.get_characteristics([key])
        self._assert_secure()
        self._assert_success(result)
        if key not in result or "value" not in result[key]:
            raise HAPError(f"HAP read returned no value for {key}")
        value = result[key]["value"]
        self._record("read", aid=key[0], iid=key[1], value=value, secure=True)
        self._check()
        return value

    async def write(self, ref: CharacteristicRef | tuple[int, int], value: Any) -> None:
        self._check()
        key = self._key(ref)
        async with asyncio.timeout(self._timeout):
            result = await self._pairing.put_characteristics([(*key, value)])
        self._assert_secure()
        self._assert_success(result)
        self._record("write", aid=key[0], iid=key[1], value=value, secure=True)
        self._check()

    async def write_many(self, values: Iterable[tuple[CharacteristicRef, Any]]) -> None:
        """Send multiple characteristics in one encrypted HAP request."""
        self._check()
        changes = [(*self._key(ref), value) for ref, value in values]
        async with asyncio.timeout(self._timeout):
            result = await self._pairing.put_characteristics(changes)
        self._assert_secure()
        self._assert_success(result)
        self._record("write_many", characteristics=changes, secure=True)
        self._check()

    async def subscribe(self, refs: Iterable[CharacteristicRef | tuple[int, int]]) -> None:
        self._check()
        keys = sorted({self._key(ref) for ref in refs})
        if not keys:
            raise ValueError("At least one characteristic is required")
        async with asyncio.timeout(self._timeout):
            result = await self._pairing.subscribe(keys)
        self._assert_secure()
        if not self._pairing.supports_subscribe or result is None:
            raise HAPError("HAP subscription was not supported")
        self._assert_success(result)
        self._record("subscribe", characteristics=[list(key) for key in keys], secure=True)
        self._check()

    async def wait_for_event(
        self,
        ref: CharacteristicRef | tuple[int, int],
        predicate: Callable[[Any], bool],
        *,
        after: int = 0,
        timeout: float = 10.0,  # noqa: ASYNC109 - enforced by asyncio.timeout below
    ) -> dict[str, Any]:
        """Wait for a received HAP event after the supplied journal sequence."""
        key = self._key(ref)
        async with asyncio.timeout(timeout):
            while True:
                self._check()
                self._changed.clear()
                for row in self._records:
                    if row["sequence"] <= after or row["kind"] != "event" or not row["secure"]:
                        continue
                    for char in row["characteristics"]:
                        if (
                            isinstance(char, dict)
                            and (char.get("aid"), char.get("iid")) == key
                            and not char.get("status", 0)
                            and "value" in char
                            and predicate(char["value"])
                        ):
                            return copy.deepcopy(row)
                await self._changed.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pairing.event_received = self._original_event_received
        try:
            await _close_controller(self._controller)
        finally:
            self._record("closed")
        if self._journal_error is not None:
            raise HAPError("HAP evidence journal could not be written") from self._journal_error

    async def __aenter__(self) -> HAPClient:
        self._check()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
