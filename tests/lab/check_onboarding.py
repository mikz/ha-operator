"""Browser regression, runnable in the prepared runner with Docker network=none."""

from __future__ import annotations

import asyncio
import json
import re

from playwright.async_api import async_playwright

from .readiness import wait_native_tokens


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.route("http://fixture.invalid/**", lambda route: route.fulfill(body=""))
            await page.goto("http://fixture.invalid/")
            await page.wait_for_url(re.compile(r"^(?!.*onboarding).*$"))
            # The observed CI boundary: final URL is ready, auth storage is not.
            assert await page.evaluate("localStorage.getItem('hassTokens')") is None
            pending = asyncio.create_task(wait_native_tokens(page))
            await page.evaluate("localStorage.setItem('hassTokens', JSON.stringify({}))")
            await page.evaluate("new Promise(resolve => requestAnimationFrame(resolve))")
            assert not pending.done(), "Empty auth data incorrectly satisfied readiness"
            expected = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
            await page.evaluate(
                "tokens => localStorage.setItem('hassTokens', JSON.stringify(tokens))", expected
            )
            assert await pending == expected
            print(json.dumps({"status": "passed", "url_ready_before_auth": True}))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
