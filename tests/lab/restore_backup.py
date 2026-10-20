"""Host-invoked native backup restore transport inside the isolated runner.

Only the host controller executes this module. The acceptance runner requests the
operation through the existing control loop and never invokes HA restart/restore.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp


async def main():
    backup_id, agent_id = sys.argv[1:]
    token = json.loads(await asyncio.to_thread(Path("/control/lifecycle-session.json").read_text))[
        "access_token"
    ]
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(os.environ["LAB_HA_URL"] + "/api/websocket") as socket:
            if (await socket.receive_json())["type"] != "auth_required":
                raise RuntimeError("Native restore transport handshake failed")
            await socket.send_json({"type": "auth", "access_token": token})
            if (await socket.receive_json())["type"] != "auth_ok":
                raise RuntimeError("Native restore transport authentication failed")
            await socket.send_json(
                {
                    "id": 1,
                    "type": "backup/restore",
                    "backup_id": backup_id,
                    "agent_id": agent_id,
                    "restore_homeassistant": True,
                    "restore_database": True,
                }
            )
            # Native restore can close the connection during its own restart.
            # Success still requires the host/runner's later restored-file and
            # config-entry/Store verification; a closed socket is not proof.
            try:
                message = await socket.receive_json(timeout=180)
                if message.get("type") == "result" and not message.get("success"):
                    raise RuntimeError("Native backup restore rejected")
            except TypeError, aiohttp.ClientConnectionError:
                pass
    print(json.dumps({"native_restore_requested": True}))


if __name__ == "__main__":
    asyncio.run(main())
