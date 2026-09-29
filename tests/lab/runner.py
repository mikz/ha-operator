"""Real-HA acceptance runner. This process has no Docker socket or host API."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import traceback
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from .readiness import wait_native_tokens

ARTIFACTS = Path("/artifacts")
CONTROL = Path("/control")
STATE = Path("/state")


async def eventually(
    function,
    predicate=lambda value: bool(value),
    *,
    timeout=30,  # noqa: ASYNC109 -- diagnostic polling owns its deadline
    interval=0.2,
):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await function()
            if predicate(last):
                return last
        except aiohttp.ClientError, ConnectionError, TimeoutError:
            pass
        await asyncio.sleep(interval)
    raise AssertionError(f"Condition not reached within {timeout}s; last={last!r}")


class HA:
    def __init__(self, session, base):
        self.session = session
        self.base = base
        self.token = None
        self.ws_id = 0

    async def request(self, method, path, data=None, *, allow_error=False):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with self.session.request(
            method, self.base + path, json=data, headers=headers
        ) as response:
            body = await response.text()
            if response.status >= 400:
                if allow_error:
                    return {"status": response.status, "body": body}
                raise AssertionError(f"HA {method} {path} -> {response.status}: {body[:1000]}")
            return json.loads(body) if body else None

    async def ws(self, kind, **fields):
        async with self.session.ws_connect(self.base + "/api/websocket") as socket:
            assert (await socket.receive_json())["type"] == "auth_required"
            await socket.send_json({"type": "auth", "access_token": self.token})
            assert (await socket.receive_json())["type"] == "auth_ok"
            await socket.send_json({"id": 1, "type": kind, **fields})
            while True:
                result = await socket.receive_json()
                if result.get("id") == 1:
                    assert result.get("success"), result
                    return result.get("result")

    async def state(self, entity):
        return await self.request("GET", f"/api/states/{entity}")

    async def service(self, domain, service, data, *, response=False):
        suffix = "?return_response" if response else ""
        return await self.request("POST", f"/api/services/{domain}/{service}{suffix}", data)

    async def add_resource(self, entry, data):
        return await self.add_subentry(entry, "resource", data)

    async def wait_entry_loaded(self, entry):
        async def state():
            entries = await self.ws("config_entries/get")
            return next((item for item in entries if item["entry_id"] == entry), None)

        return await eventually(state, lambda item: item is not None and item["state"] == "loaded")

    async def reload_entry(self, entry):
        # Native flow completion schedules the listener; this explicit setup
        # barrier serializes behind it through HA's entry.setup_lock.
        await self.request("POST", f"/api/config/config_entries/entry/{entry}/reload", {})
        await self.wait_entry_loaded(entry)

    async def options(self, entry, data):
        flow = await self.request(
            "POST", "/api/config/config_entries/options/flow", {"handler": entry}
        )
        result = await self.request(
            "POST", "/api/config/config_entries/options/flow/" + flow["flow_id"], data
        )
        assert result["type"] == "create_entry", result
        await self.reload_entry(entry)
        return result

    async def add_subentry(self, entry, kind, data):
        flow = await self.request(
            "POST",
            "/api/config/config_entries/subentries/flow",
            {
                "handler": [entry, kind],
                "show_advanced_options": True,
            },
        )
        flow = await self.request(
            "POST", "/api/config/config_entries/subentries/flow/" + flow["flow_id"], data
        )
        assert flow["type"] == "create_entry", flow
        await self.reload_entry(entry)
        subentries = await self.ws("config_entries/subentries/list", entry_id=entry)
        matches = [item for item in subentries if item.get("title") == data["name"]]
        if matches:
            return matches[0]["subentry_id"]
        # The native flow response also returns the new subentry on some frontend revisions.
        result = flow.get("result", {})
        if isinstance(result, dict) and "subentry_id" in result:
            return result["subentry_id"]
        registry = await self.ws("config/entity_registry/list")
        found = [
            item
            for item in registry
            if item.get("config_entry_id") == entry
            and item.get("config_subentry_id")
            and item.get("original_name") == data["name"]
        ]
        assert found, {"flow": flow, "subentries": subentries}
        return found[0]["config_subentry_id"]

    async def managed(self, subentry, domain):
        async def registered():
            registry = await self.ws("config/entity_registry/list")
            matches = [
                item
                for item in registry
                if item.get("platform") == "ha_operator"
                and item.get("config_subentry_id") == subentry
                and item["entity_id"].startswith(domain + ".")
            ]
            assert len(matches) <= 1, matches
            if not matches:
                return None
            entity_id = matches[0]["entity_id"]
            states = await self.request("GET", "/api/states")
            if any(
                row["entity_id"] == entity_id and row["state"] not in {"unknown", "unavailable"}
                for row in states
            ):
                return entity_id
            return None

        return await eventually(registered)


class Lab:
    def __init__(self, session, page):
        self.session = session
        self.page = page
        self.ha = HA(session, os.environ["LAB_HA_URL"])
        self.sim_url = os.environ["LAB_SIM_URL"]
        self.results = []
        self.secret_values = []
        self.hap = None

    def record_secret(self, *values):
        self.secret_values.extend(value for value in values if value)
        temporary = CONTROL / ".secrets.tmp"
        temporary.write_text(json.dumps(self.secret_values))
        # The bind parent is host-owned 0700; 0644 permits the non-root Linux
        # host to read this root-container file without exposing the private path.
        temporary.chmod(0o644)
        temporary.replace(CONTROL / "secrets.json")

    async def sim(self, method="GET", path="/admin/state", data=None):
        async with self.session.request(method, self.sim_url + path, json=data) as response:
            text = await response.text()
            assert response.status < 400, text
            return json.loads(text)

    async def journal(self):
        return (await self.sim(path="/admin/journal"))["events"]

    async def physical(self, device="skylight"):
        state = await self.sim()
        return next(item for item in state["devices"] if item["id"] == device)["physical"]

    async def wait_position(
        self,
        position,
        *,
        device="skylight",
        timeout=15,  # noqa: ASYNC109
    ):
        return await eventually(
            lambda: self.physical(device),
            lambda s: abs(s["position"] - position) <= 2,
            timeout=timeout,
        )

    @asynccontextmanager
    async def scenario(self, name):
        item = {"id": name, "started_at": time.time(), "status": "running"}
        self.results.append(item)
        print(f"Starting {name}", flush=True)
        try:
            yield
        except Exception as error:
            item.update(status="failed", error=f"{type(error).__name__}: {error}")
            await self.page.screenshot(path=str(ARTIFACTS / f"failure-{name}.png"))
            if self.ha.token:
                try:
                    async with asyncio.timeout(10):
                        diagnostics = {"states": await self.ha.request("GET", "/api/states")}
                        if hasattr(self, "entry"):
                            diagnostics["explain"] = await self.ha.service(
                                "ha_operator", "explain", {}, response=True
                            )
                        (ARTIFACTS / f"failure-{name}.json").write_text(
                            json.dumps(diagnostics, indent=2)
                        )
                except Exception as diagnostic_error:
                    item["diagnostic_error"] = type(diagnostic_error).__name__
            raise
        else:
            item["status"] = "passed"
        finally:
            item["completed_at"] = time.time()
            print(f"{name}: {item['status']}", flush=True)
            (ARTIFACTS / "scenarios.json").write_text(json.dumps(self.results, indent=2))

    async def crash(self, action):
        nonce = secrets.token_hex(6)
        request = {
            "run_id": os.environ["LAB_RUN_ID"],
            "action": action,
            "requested_at": time.time(),
            "request_id": nonce,
        }
        pending = CONTROL / f".request-{nonce}.tmp"
        pending.write_text(json.dumps(request))
        pending.rename(CONTROL / f"request-{nonce}.json")

        async def acknowledgement():
            path = CONTROL / f"ack-{nonce}.json"
            return json.loads(path.read_text()) if path.exists() else None

        return await eventually(acknowledgement, timeout=100)

    async def ready(self):
        return await eventually(
            lambda: self.ha.request("GET", "/api/config"),
            lambda data: data.get("state") == "RUNNING",
            timeout=120,
        )

    async def bootstrap(self):
        async with self.scenario("LAB-ONBOARDING"):

            async def is_ready():
                async with self.session.get(self.ha.base + "/api/onboarding") as response:
                    return response.status == 200

            await eventually(is_ready, timeout=150)
            await self.page.goto(self.ha.base + "/", wait_until="domcontentloaded")
            await self.page.get_by_role("button", name="Create my smart home").click()
            password = secrets.token_urlsafe(24)
            self.record_secret(password)
            for name, value in (
                ("name", "Lab Operator"),
                ("username", "lab_operator"),
                ("password", password),
                ("password_confirm", password),
            ):
                await self.page.locator(f'onboarding-create-user input[name="{name}"]').fill(value)
            await self.page.get_by_role("button", name="Create account", exact=True).click()
            # All following controls are HA's rendered onboarding steps. Location detection
            # and map searches are deliberately never invoked in this offline fixture.
            await (
                self.page.locator("onboarding-location")
                .get_by_role("button", name="Next", exact=True)
                .click()
            )
            await self.page.locator("onboarding-location").wait_for(state="hidden")
            await (
                self.page.locator("onboarding-core-config")
                .get_by_role("button", name="Next", exact=True)
                .click()
            )
            await (
                self.page.locator("onboarding-analytics")
                .get_by_role("button", name="Next", exact=True)
                .click()
            )
            await self.page.get_by_role("button", name="Finish", exact=True).click()
            await self.page.wait_for_url(re.compile(r"^(?!.*onboarding).*$"))
            tokens = await wait_native_tokens(self.page)
            self.ha.token = tokens["access_token"]
            self.record_secret(tokens["access_token"], tokens.get("refresh_token", ""))
            config = await self.ready()
            assert config["version"] == os.environ["LAB_HA_VERSION"], config["version"]
            await self.page.screenshot(path=str(ARTIFACTS / "onboarding-complete.png"))
        async with self.scenario("LAB-NATIVE-CONFIG-FLOW"):
            await self.page.goto(self.ha.base + "/config/integrations/dashboard")
            await self.page.get_by_role("button", name="Add integration", exact=True).click()
            await self.page.get_by_placeholder("Search for a brand name", exact=True).fill(
                "HA Operator"
            )
            await self.page.get_by_text(re.compile(r"HA Operator\s+\(Helper\)"), exact=True).click()
            await self.page.get_by_role("button", name="OK", exact=True).click()
            await self.page.get_by_role("button", name="Submit", exact=True).click()
            await self.page.get_by_role("button", name="Finish", exact=True).click()
            entries = await eventually(
                lambda: self.ha.ws("config_entries/get"),
                lambda rows: any(r["domain"] == "ha_operator" for r in rows),
            )
            self.entry = next(
                item["entry_id"] for item in entries if item["domain"] == "ha_operator"
            )
            await self.ha.wait_entry_loaded(self.entry)
            await self.page.screenshot(path=str(ARTIFACTS / "integration-configured.png"))
        async with self.scenario("LAB-RESOURCE-CONFIGURATION"):
            await eventually(lambda: self.sim(path="/health"), timeout=30)
            await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": True, "speed": 100})
            self.resource = await self.ha.add_resource(
                self.entry,
                {
                    "name": "Lab Skylight",
                    "kind": "cover",
                    "entity_id": "cover.sim_skylight",
                    "retry_interval": 1,
                    "command_interval": 0.2,
                    "movement_timeout": 3,
                    "tolerance": 2,
                    "manual_duration": 120,
                },
            )
            self.cover = await self.ha.managed(self.resource, "cover")
            self.mode = await self.ha.managed(self.resource, "select")

    async def request(self, position, *, duration=30, request_id=None):
        return await self.ha.service(
            "ha_operator",
            "request",
            {
                "resource_id": self.resource,
                "target": {"position": position},
                "duration": duration,
                "request_id": request_id or secrets.token_hex(8),
            },
        )

    async def cover_scenarios(self):
        async with self.scenario("OBSERVE-ZERO"):
            before = len([e for e in await self.journal() if e["kind"] == "command"])
            rejected = await self.ha.request(
                "POST",
                "/api/services/ha_operator/request",
                {"resource_id": self.resource, "target": {"position": 70}, "duration": 30},
                allow_error=True,
            )
            assert rejected["status"] >= 400, rejected
            explained = await self.ha.service(
                "ha_operator", "explain", {"resource_id": self.resource}, response=True
            )
            assert explained["service_response"]["resources"][self.resource]["manual"] is None
            await asyncio.sleep(2)
            after = len([e for e in await self.journal() if e["kind"] == "command"])
            assert before == after, "Observe mode sent physical commands"
            assert (await self.ha.state(self.cover))["attributes"]["current_position"] == 0
        await self.ha.service("select", "select_option", {"entity_id": self.mode, "option": "live"})
        async with self.scenario("COVER-RAIN-RETRY"):
            await asyncio.sleep(1.2)
            assert len([e for e in await self.journal() if e["kind"] == "command"]) == after
            await self.request(70)
            await eventually(
                self.journal, lambda events: any(e["kind"] == "refusal" for e in events)
            )
            assert (await self.physical())["position"] == 0
            await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
            await self.wait_position(70)
            await eventually(
                lambda: self.ha.state(self.cover),
                lambda s: abs(s["attributes"].get("current_position", -100) - 70) <= 2,
            )
        async with self.scenario("COVER-SUPERSESSION"):
            await self.sim("POST", "/admin/devices/skylight", {"speed": 10})
            await self.request(10)
            await asyncio.sleep(0.4)
            await self.request(90)
            await self.sim("POST", "/admin/devices/skylight", {"speed": 100})
            await self.wait_position(90)
            await asyncio.sleep(1.3)
            assert abs((await self.physical())["position"] - 90) <= 2
        async with self.scenario("COVER-STOP"):
            await self.sim("POST", "/admin/devices/skylight", {"speed": 10})
            await self.request(10)
            await eventually(lambda: self.physical(), lambda s: s.get("moving", False))
            await self.ha.service("cover", "stop_cover", {"entity_id": self.cover})
            stopped = (await self.physical())["position"]
            await asyncio.sleep(1.5)
            assert abs((await self.physical())["position"] - stopped) < 1
            await self.sim("POST", "/admin/devices/skylight", {"speed": 100})
        async with self.scenario("COVER-EXPIRY"):
            await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": True})
            await self.request(100, duration=1)
            await asyncio.sleep(1.5)
            before = (await self.physical())["position"]
            await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
            await asyncio.sleep(2)
            assert abs((await self.physical())["position"] - before) < 1, "Expired request resumed"
        for action, name in (("restart", "COVER-RESTART"), ("kill", "COVER-KILL")):
            async with self.scenario(name):
                await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": True})
                health = await self.sim(path="/health")
                await self.request(100, duration=120)
                await eventually(
                    self.journal,
                    lambda events, health=health: any(
                        e["kind"] == "refusal" and e["seq"] > health["journal_seq"] for e in events
                    ),
                )
                await self.crash(action)
                if action == "kill":
                    await asyncio.sleep(0.5)
                    await self.crash("start")
                await self.ready()
                now = await self.sim(path="/health")
                assert now["instance_id"] == health["instance_id"], "Simulator restarted with HA"
                await self.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
                await self.wait_position(100, timeout=30)
                # Establish a new start position before the next blocked opening.
                await self.request(20)
                await self.wait_position(20)

    async def homekit(self):
        from .hap import HAPClient

        async with self.scenario("LAB-HAP-PAIR"):
            # Reload the native bridge after managed entities first appear.
            entries = await self.ha.ws("config_entries/get")
            homekit_entry = next(
                item["entry_id"] for item in entries if item["domain"] == "homekit"
            )
            await self.ha.request(
                "POST", f"/api/config/config_entries/entry/{homekit_entry}/reload", {}
            )
            notifications = await eventually(
                lambda: self.ha.ws("persistent_notification/get"),
                lambda items: any(item.get("title") == "HomeKit Pairing" for item in items),
            )
            notice = next(item for item in notifications if item.get("title") == "HomeKit Pairing")
            pin = re.search(r"\b\d{3}-\d{2}-\d{3}\b", notice["message"]).group()
            self.record_secret(pin)
            self.hap = await HAPClient.connect(
                os.environ["LAB_HA_ADDRESS"],
                21063,
                pin,
                STATE / "homekit-pairing.json",
                journal_path=ARTIFACTS / "hap-transcript.jsonl",
            )
            accessories = await self.hap.list_accessories()
            (ARTIFACTS / "hap-accessories.json").write_text(json.dumps(accessories, indent=2))
            names = [
                char["value"]
                for a in accessories
                for service in a["services"]
                for char in service["characteristics"]
                if char["type"].upper().startswith("00000023-")
            ]
            assert not any(str(name).startswith("Sim ") for name in names), names
            current = self.hap.characteristic(
                "Lab Skylight", "0000006D-0000-1000-8000-0026BB765291"
            )
            target = self.hap.characteristic("Lab Skylight", "0000007C-0000-1000-8000-0026BB765291")
            expected_characteristic = current.key
            await self.hap.subscribe([current])
            sequence = self.hap.sequence
            await self.hap.write(target, 40)
            await self.wait_position(40)
            await self.hap.wait_for_event(
                current, lambda value: abs(value - 40) <= 2, after=sequence, timeout=15
            )
            assert abs(await self.hap.read(current) - 40) <= 2
            explained = await self.ha.service(
                "ha_operator", "explain", {"resource_id": self.resource}, response=True
            )
            manual = explained["service_response"]["resources"][self.resource]["manual"]
            assert manual["target"]["position"] == 40 and manual["request_id"], manual
            (ARTIFACTS / "hap-durable-receipt.json").write_text(json.dumps(manual, indent=2))
        async with self.scenario("LAB-HAP-RESTART-DURABLE"):
            await self.hap.close()
            self.hap = None
            simulator_instance = (await self.sim(path="/health"))["instance_id"]
            await self.crash("restart")
            await self.ready()
            assert (await self.sim(path="/health"))["instance_id"] == simulator_instance
            explained = await self.ha.service(
                "ha_operator", "explain", {"resource_id": self.resource}, response=True
            )
            recovered = explained["service_response"]["resources"][self.resource]["manual"]
            assert recovered == manual, {"before": manual, "after": recovered}
            await self.ha.managed(self.resource, "cover")

            self.hap = await HAPClient.connect(
                os.environ["LAB_HA_ADDRESS"],
                21063,
                None,
                STATE / "homekit-pairing.json",
                journal_path=ARTIFACTS / "hap-transcript.jsonl",
            )

            async def hap_ready():
                accessories = await self.hap.list_accessories()
                names = [
                    char.get("value")
                    for accessory in accessories
                    for service in accessory["services"]
                    for char in service["characteristics"]
                    if char["type"].upper().startswith("00000023-")
                ]
                if "Lab Skylight" not in names:
                    return None
                return self.hap.characteristic(
                    "Lab Skylight", "0000006D-0000-1000-8000-0026BB765291"
                )

            current = await eventually(hap_ready, timeout=90, interval=0.5)
            assert current.key == expected_characteristic, "HAP accessory identity changed"
            assert abs(await self.hap.read(current) - 40) <= 2

    async def native_control(self):
        async with self.scenario("LAB-NATIVE-COVER"):
            await self.page.goto(self.ha.base + "/config/entities")
            await self.page.get_by_placeholder(re.compile(r"Search \d+ entities")).fill(self.cover)
            await self.page.get_by_role("rowheader", name="Lab Skylight", exact=True).click()
            await (
                self.page.locator("more-info-cover")
                .get_by_role("button", name="Set position to 0%", exact=True)
                .click()
            )
            await self.wait_position(0)
            await self.page.screenshot(path=str(ARTIFACTS / "native-cover-closed.png"))

    async def diagnostics(self):
        async with self.scenario("LAB-DIAGNOSTICS"):
            downloaded = await self.ha.request("GET", f"/api/diagnostics/config_entry/{self.entry}")
            data = downloaded["data"]
            assert set(data) == {
                "version",
                "faulted",
                "resources",
                "policies",
                "requirements",
                "history",
                "shadow_locked",
                "trace",
            }, data.keys()
            assert data["version"] == 1 and isinstance(data["faulted"], bool)
            assert isinstance(data["shadow_locked"], bool)
            assert set(data["trace"]) == {
                "enabled",
                "healthy",
                "complete",
                "last_sequence",
                "durable_sequence",
                "queued_records",
                "dropped_records",
                "write_errors",
                "rotations",
                "last_heartbeat",
                "last_write_at",
                "session",
            }
            assert len(data["history"]) <= 100
            for collection in ("resources", "policies", "requirements"):
                assert all(re.fullmatch(r"[0-9a-f]{12}", key) for key in data[collection])
            resource_fields = {
                "kind",
                "mode",
                "status",
                "source",
                "desired",
                "observed",
                "available",
                "moving",
                "restricted",
                "reported_at",
                "manual",
                "last_command",
                "attempts",
                "next_attempt",
            }
            assert all(set(row) == resource_fields for row in data["resources"].values())
            assert all(set(row) == {"enabled"} for row in data["policies"].values())
            assert all(
                set(row) == {"status", "selected_provider", "acquiring_provider"}
                for row in data["requirements"].values()
            )
            assert all(
                set(row) == {"resource", "status", "source", "target", "at"}
                for row in data["history"]
            )
            encoded = json.dumps(data)
            for private_value in (
                self.resource,
                self.entry,
                self.cover,
                "cover.sim_skylight",
                "Lab Skylight",
            ):
                assert private_value not in encoded, "Diagnostics exposed identifying configuration"
            (ARTIFACTS / "downloaded-diagnostics.json").write_text(json.dumps(downloaded, indent=2))

    async def soak(self):
        """Two full five-minute retry periods, using production timing defaults."""
        async with self.scenario("LAB-REALISTIC-SOAK"):
            resource = await self.ha.add_resource(
                self.entry,
                {
                    "name": "Lab Soak Inlet",
                    "kind": "cover",
                    "entity_id": "cover.sim_inlet",
                    "retry_interval": 300,
                    "command_interval": 30,
                    "movement_timeout": 120,
                    "tolerance": 2,
                    "manual_duration": 1800,
                },
            )
            mode = await self.ha.managed(resource, "select")
            await self.sim("POST", "/admin/devices/inlet", {"hidden_rain": True, "speed": 2.5})
            await self.ha.service("select", "select_option", {"entity_id": mode, "option": "live"})
            before = (await self.sim(path="/health"))["journal_seq"]
            await self.ha.service(
                "ha_operator",
                "request",
                {
                    "resource_id": resource,
                    "target": {"position": 70},
                    "duration": 1200,
                },
            )

            async def refusals():
                return [
                    item
                    for item in await self.journal()
                    if item["kind"] == "refusal"
                    and item["device_id"] == "inlet"
                    and item["seq"] > before
                ]

            initial = await eventually(refusals)
            first = initial[0]["monotonic"]
            retried = await eventually(
                refusals, lambda rows: len(rows) >= 2, timeout=330, interval=1
            )
            assert retried[1]["monotonic"] - first >= 299, "Retry storm at default timing"
            await self.sim("POST", "/admin/devices/inlet", {"hidden_rain": False})
            await self.wait_position(70, device="inlet", timeout=360)
            await eventually(
                lambda: self.physical("inlet"),
                lambda state: not state["moving"] and abs(state["position"] - 70) <= 2,
                timeout=30,
            )
            commands = [
                item
                for item in await self.journal()
                if item["kind"] == "command"
                and item["device_id"] == "inlet"
                and item["seq"] > before
            ]
            assert len(commands) == 3, commands
            assert commands[-1]["monotonic"] - first >= 598
            assert (await self.sim(path="/health"))["instance_id"] == commands[0]["instance_id"]

    def sanitize(self):
        values = [value for value in self.secret_values if value]
        for path in ARTIFACTS.glob("*.json*"):
            data = path.read_text()
            for secret in values:
                data = data.replace(secret, "<redacted-test-secret>")
            path.write_text(data)
        trace = ARTIFACTS / "trace.zip"
        if trace.exists():
            replacement = trace.with_suffix(".redacted.zip")
            with zipfile.ZipFile(trace) as source, zipfile.ZipFile(replacement, "w") as target:
                for item in source.infolist():
                    content = source.read(item)
                    if not item.filename.endswith((".png", ".jpeg", ".jpg")):
                        for secret in values:
                            content = content.replace(secret.encode(), b"<redacted-test-secret>")
                    target.writestr(item, content)
            replacement.replace(trace)


async def main():
    from playwright.async_api import async_playwright

    await asyncio.to_thread(ARTIFACTS.mkdir, exist_ok=True)
    await asyncio.to_thread(STATE.mkdir, exist_ok=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}
            )
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = await context.new_page()
            page.set_default_timeout(30_000)
            lab = Lab(session, page)
            try:
                await lab.bootstrap()
                if os.environ["LAB_SCENARIO"] == "windows":
                    from .scenarios_windows import run_windows

                    await run_windows(lab)
                    await lab.diagnostics()
                    return
                if os.environ["LAB_SCENARIO"] == "replay":
                    from .scenarios_shadow import run_external_replay

                    await run_external_replay(lab)
                    return
                if os.environ["LAB_SCENARIO"] == "shadow":
                    from .scenarios_shadow import run_shadow_scenarios

                    await run_shadow_scenarios(lab)
                    await lab.diagnostics()
                    return
                if os.environ["LAB_SCENARIO"] == "cellar":
                    from .scenarios_cellar import run_cellar_debug

                    await run_cellar_debug(lab)
                    await lab.diagnostics()
                    return
                if os.environ["LAB_SCENARIO"] == "soak":
                    await lab.soak()
                    return
                if os.environ["LAB_SCENARIO"] != "smoke":
                    await lab.cover_scenarios()
                    if os.environ["LAB_SCENARIO"] == "all":
                        from .scenarios_schedule import schedule_scenarios

                        await schedule_scenarios(lab)
                else:
                    await lab.ha.service(
                        "select", "select_option", {"entity_id": lab.mode, "option": "live"}
                    )
                    await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
                await lab.homekit()
                await lab.native_control()
                if os.environ["LAB_SCENARIO"] == "all":
                    from .scenarios_airflow import run_airflow_scenarios

                    await run_airflow_scenarios(lab)
                    from .scenarios_cellar import run_cellar_scenarios
                    from .scenarios_shadow import observe, run_shadow_scenarios

                    await observe(lab, locked=False)
                    await run_cellar_scenarios(lab)
                    await run_shadow_scenarios(lab)
                await lab.diagnostics()
            finally:
                if lab.hap:
                    await lab.hap.close()
                try:
                    (ARTIFACTS / "simulator-journal.json").write_text(
                        json.dumps(await lab.journal(), indent=2)
                    )
                finally:
                    await context.tracing.stop(path=str(ARTIFACTS / "trace.zip"))
                    await browser.close()
                    lab.sanitize()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1) from None
