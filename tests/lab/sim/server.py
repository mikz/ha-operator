"""HTTP process for independent lab devices; no Home Assistant dependencies."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiohttp import web

from .model import CommandError, Simulator

SIMULATOR = web.AppKey("simulator", Simulator)


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except KeyError as err:
        return web.json_response({"error": f"Missing device or field: {err}"}, status=404)
    except (ValueError, TypeError) as err:
        return web.json_response(
            {"error": str(err)}, status=409 if isinstance(err, CommandError) else 400
        )


async def health(request: web.Request) -> web.Response:
    sim = request.app[SIMULATOR]
    return web.json_response({"instance_id": sim.instance_id, "journal_seq": sim.sequence})


async def devices(request: web.Request) -> web.Response:
    sim = request.app[SIMULATOR]
    payload = sim.public()
    if "device_id" in request.match_info:
        return web.json_response(sim.devices[request.match_info["device_id"]].descriptor())
    return web.json_response(payload)


async def command(request: web.Request) -> web.Response:
    return web.json_response(
        request.app[SIMULATOR].command(
            request.match_info["device_id"],
            await request.json(),
        )
    )


async def reset(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    sim = request.app[SIMULATOR]
    sim.reset(body.get("devices"))
    return web.json_response(sim.public())


async def control(request: web.Request) -> web.Response:
    sim = request.app[SIMULATOR]
    sim.control(request.match_info["device_id"], await request.json())
    return web.json_response({"accepted": True, "journal_seq": sim.sequence})


async def state(request: web.Request) -> web.Response:
    return web.json_response(request.app[SIMULATOR].inspect())


async def journal(request: web.Request) -> web.Response:
    sim = request.app[SIMULATOR]
    after = int(request.query.get("after", "0"))
    return web.json_response(
        {
            "instance_id": sim.instance_id,
            "events": [event for event in sim.events if event["seq"] > after],
        }
    )


async def ticker(app: web.Application):
    async def tick_loop() -> None:
        while True:
            await asyncio.sleep(0.05)
            app[SIMULATOR].tick()

    task = asyncio.create_task(tick_loop(), name="physical-simulator-clock")
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def create_app(simulator: Simulator | None = None) -> web.Application:
    app = web.Application(middlewares=[errors])
    app[SIMULATOR] = simulator or Simulator()
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/devices", devices),
            web.get("/devices/{device_id}", devices),
            web.post("/devices/{device_id}/command", command),
            web.post("/admin/reset", reset),
            web.post("/admin/devices/{device_id}", control),
            web.patch("/admin/devices/{device_id}", control),
            web.get("/admin/state", state),
            web.get("/admin/journal", journal),
        ]
    )
    app.cleanup_ctx.append(ticker)
    return app


def main(argv: list[str] | None = None) -> Any:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)
    web.run_app(create_app(Simulator(journal_path=args.journal)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
