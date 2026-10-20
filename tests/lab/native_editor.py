"""Interact with HA's rendered subentry and embedded native condition editors."""

from __future__ import annotations

from playwright.async_api import expect

from .runner import ARTIFACTS


async def open_subentry_editor(lab, title):
    await lab.page.goto(lab.ha.base + "/config/integrations/integration/ha_operator")
    row = lab.page.locator("ha-config-sub-entry-row").filter(has_text=title)
    await row.get_by_role("button", name="Configure", exact=True).click()
    await lab.page.get_by_role("button", name="Submit", exact=True).wait_for()


async def save_subentry_editor(lab):
    submit = lab.page.get_by_role("button", name="Submit", exact=True)
    await submit.click()
    await submit.wait_for(state="hidden")
    await lab.ha.reload_entry(lab.entry)


async def choose_entity(lab, selector, entity_id):
    # This scalar native picker replaces its value; exact readback rejects an append.
    field = selector.locator("ha-picker-field").first
    clear = field.locator("ha-icon-button.clear")
    if await clear.count():
        await clear.click()
    await field.click()
    search = selector.locator("ha-picker-combo-box").get_by_role("textbox")
    await search.fill(entity_id)
    name = (await lab.ha.state(entity_id))["attributes"]["friendly_name"]
    await selector.locator("ha-picker-combo-box").get_by_text(name, exact=True).click()
    await expect(selector).to_have_js_property("value", entity_id)


async def expand_native_row(selector):
    panel = selector.locator("ha-expansion-panel")
    summary = panel.locator("#summary")
    if await summary.get_attribute("aria-expanded") != "true":
        await panel.locator("ha-svg-icon.summary-icon").click()
    await expect(summary).to_have_attribute("aria-expanded", "true")


async def exercise_policy_conditions(lab, title, policy_id):
    """Add, save, edit, and remove a native state condition on the same policy."""
    original = {
        row["unique_id"]: row["id"]
        for row in await lab.ha.ws("config/entity_registry/list")
        if row.get("config_subentry_id") == policy_id
    }
    await open_subentry_editor(lab, title)
    selector = lab.page.locator("ha-selector-condition")
    await selector.get_by_role("button", name="Add condition", exact=True).click()
    picker = lab.page.locator("add-automation-element-dialog")
    await picker.get_by_text("By type", exact=True).click()
    await picker.get_by_role("button", name="Entity", exact=True).click()
    await picker.get_by_text("State", exact=True).click()
    condition = selector.locator("ha-automation-condition-state")
    await condition.locator("ha-selector-entity").wait_for(state="visible")
    await choose_entity(
        lab, condition.locator("ha-selector-entity"), "binary_sensor.sim_cellar_policy"
    )
    state = condition.locator("ha-selector-state")
    await state.locator("ha-picker-field").last.click()
    search = state.locator("ha-picker-combo-box").get_by_role("textbox")
    await search.fill("on")
    await state.locator("ha-picker-combo-box").get_by_text("On", exact=True).click()
    await expect(state).to_have_js_property("value", ["on"])
    await lab.page.screenshot(
        path=str(ARTIFACTS / "native-policy-condition-added.png"), full_page=True
    )
    await save_subentry_editor(lab)

    await open_subentry_editor(lab, title)
    selector = lab.page.locator("ha-selector-condition")
    await expect(selector).to_have_js_property(
        "value",
        [
            {
                "condition": "state",
                "entity_id": "binary_sensor.sim_cellar_policy",
                "state": ["on"],
            }
        ],
    )
    await expand_native_row(selector)
    await choose_entity(
        lab, selector.locator("ha-selector-entity"), "binary_sensor.sim_cellar_demand"
    )
    await lab.page.screenshot(
        path=str(ARTIFACTS / "native-policy-condition-reference-edited.png"), full_page=True
    )
    await save_subentry_editor(lab)

    await open_subentry_editor(lab, title)
    await expect(lab.page.locator("ha-selector-condition")).to_have_js_property(
        "value",
        [
            {
                "condition": "state",
                "entity_id": "binary_sensor.sim_cellar_demand",
                "state": ["on"],
            }
        ],
    )
    row = lab.page.locator("ha-selector-condition ha-automation-condition-row")
    await row.get_by_role("button", name="Menu", exact=True).click()
    await row.locator('ha-dropdown-item[value="delete"]').click()
    await lab.page.screenshot(
        path=str(ARTIFACTS / "native-policy-condition-removed.png"), full_page=True
    )
    await save_subentry_editor(lab)
    await open_subentry_editor(lab, title)
    await expect(lab.page.locator("ha-selector-condition")).to_have_js_property("value", [])
    await save_subentry_editor(lab)
    current = {
        row["unique_id"]: row["id"]
        for row in await lab.ha.ws("config/entity_registry/list")
        if row.get("config_subentry_id") == policy_id
    }
    assert current == original
    subentries = await lab.ha.ws("config_entries/subentries/list", entry_id=lab.entry)
    assert any(row["subentry_id"] == policy_id and row["title"] == title for row in subentries)


async def clear_v3_migration_fields(lab, policy_title, resource_title):
    """Use the released 0.3 selectors before the incompatible upgrade."""
    await open_subentry_editor(lab, policy_title)
    (ARTIFACTS / (policy_title.replace(" ", "-") + "-v3-input-ui.txt")).write_text(
        await lab.page.locator("body").aria_snapshot()
    )
    await (
        lab.page.locator("ha-selector-select")
        .filter(has_text="Policy input")
        .locator("ha-picker-field")
        .click()
    )
    await lab.page.get_by_text("Existing state or submitted occurrence", exact=True).click()
    await lab.page.screenshot(
        path=str(ARTIFACTS / "migration-v3-legacy-input-selected.png"), full_page=True
    )
    await save_subentry_editor(lab)
    await open_subentry_editor(lab, resource_title)
    (ARTIFACTS / "migration-v3-resource-before-clear-ui.txt").write_text(
        await lab.page.locator("body").aria_snapshot()
    )
    check = lab.page.locator("ha-selector-object").filter(has_text="Synthetic telemetry")
    await check.get_by_role("button", name="Delete", exact=True).click()
    await lab.page.screenshot(
        path=str(ARTIFACTS / "migration-v3-health-check-removed.png"), full_page=True
    )
    await save_subentry_editor(lab)


async def edit_resource_actor(lab, title, entity_id):
    await open_subentry_editor(lab, title)
    await choose_entity(lab, lab.page.locator("ha-selector-entity").first, entity_id)
    await lab.page.screenshot(
        path=str(ARTIFACTS / ("native-resource-reference-" + entity_id.replace(".", "-") + ".png")),
        full_page=True,
    )
    await save_subentry_editor(lab)
    await open_subentry_editor(lab, title)
    await expect(lab.page.locator("ha-selector-entity").first).to_have_js_property(
        "value", entity_id
    )
    await save_subentry_editor(lab)


async def delete_subentry(lab, title):
    from .runner import eventually

    before = await lab.ha.ws("config_entries/subentries/list", entry_id=lab.entry)
    identifier = next(item["subentry_id"] for item in before if item["title"] == title)
    survivor = next(item["title"] for item in before if item["subentry_id"] != identifier)
    await lab.page.goto(lab.ha.base + "/config/integrations/integration/ha_operator")
    row = lab.page.locator("ha-config-sub-entry-row").filter(has_text=title)
    menu = row.locator("ha-list-item-base.sub-entry > ha-dropdown[slot='end']")
    await menu.get_by_role("button", name="Menu", exact=True).click()
    await menu.locator('ha-dropdown-item[value="delete"]').click()
    await lab.page.get_by_role("heading", name=f"Delete {title}?", exact=True).wait_for(
        state="visible"
    )
    await lab.page.get_by_role("button", name="Delete", exact=True).click()
    remaining = await eventually(
        lambda: lab.ha.ws("config_entries/subentries/list", entry_id=lab.entry),
        lambda rows: all(item["subentry_id"] != identifier for item in rows),
    )
    assert {item["subentry_id"] for item in remaining} == {
        item["subentry_id"] for item in before if item["subentry_id"] != identifier
    }
    await lab.ha.reload_entry(lab.entry)
    await lab.page.goto(lab.ha.base + "/config/integrations/integration/ha_operator")
    await (
        lab.page.locator("ha-config-sub-entry-row")
        .filter(has_text=survivor)
        .wait_for(state="visible")
    )
    await expect(row).to_have_count(0)


async def exercise_occurrence_editor(lab, title, policy_id, event_prefix):
    """Edit real native trigger/duration selectors while the occurrence is disabled."""
    enabled = await lab.ha.managed(policy_id, "switch")
    assert (await lab.ha.state(enabled))["state"] == "off"
    await open_subentry_editor(lab, title)
    selectors = lab.page.locator("ha-selector-trigger")
    for index, event in enumerate((event_prefix + "_draft", event_prefix + "_cancel")):
        selector = selectors.nth(index)
        await selector.get_by_role("button", name="Add trigger", exact=True).click()
        picker = lab.page.locator("add-automation-element-dialog")
        await picker.get_by_text("By type", exact=True).click()
        await picker.get_by_role("button", name="Manual event received", exact=True).click()
        await picker.get_by_role(
            "button",
            name=(
                "Manual event received Triggers when an event is being received "
                "(events are a building block used across Home Assistant)."
            ),
            exact=True,
        ).click()
        body = selector
        await body.get_by_role("textbox", name="Event type", exact=True).wait_for(state="visible")
        field = body.get_by_role("textbox", name="Event type", exact=True)
        await field.fill(event)
        await field.press("Tab")
    duration = lab.page.get_by_role("group", name="Request duration", exact=True).get_by_role(
        "spinbutton", name="ss", exact=True
    )
    await duration.fill("3")
    await duration.press("Tab")
    await lab.page.screenshot(
        path=str(ARTIFACTS / "native-occurrence-triggers-added.png"), full_page=True
    )
    await save_subentry_editor(lab)

    await open_subentry_editor(lab, title)
    selector = lab.page.locator("ha-selector-trigger").first
    await expect(selector).to_have_js_property(
        "value", [{"trigger": "event", "event_type": event_prefix + "_draft"}]
    )
    await expect(lab.page.locator("ha-selector-trigger").nth(1)).to_have_js_property(
        "value", [{"trigger": "event", "event_type": event_prefix + "_cancel"}]
    )
    await expand_native_row(selector)
    field = selector.get_by_role("textbox", name="Event type", exact=True)
    assert await field.input_value() == event_prefix + "_draft"
    await field.fill(event_prefix + "_admit")
    await field.press("Tab")
    duration = lab.page.get_by_role("group", name="Request duration", exact=True).get_by_role(
        "spinbutton", name="ss", exact=True
    )
    await duration.fill("4")
    await duration.press("Tab")
    await lab.page.screenshot(
        path=str(ARTIFACTS / "native-occurrence-triggers-edited.png"), full_page=True
    )
    await save_subentry_editor(lab)

    await open_subentry_editor(lab, title)
    for index, event in enumerate((event_prefix + "_admit", event_prefix + "_cancel")):
        selector = lab.page.locator("ha-selector-trigger").nth(index)
        await expect(selector).to_have_js_property(
            "value", [{"trigger": "event", "event_type": event}]
        )
        await expand_native_row(selector)
        assert (
            await selector.get_by_role("textbox", name="Event type", exact=True).input_value()
            == event
        )
    assert (
        int(
            await lab.page.get_by_role("group", name="Request duration", exact=True)
            .get_by_role("spinbutton", name="ss", exact=True)
            .input_value()
        )
        == 4
    )
    await save_subentry_editor(lab)
    assert (await lab.ha.state(enabled))["state"] == "off"
    rows = await lab.ha.ws("config_entries/subentries/list", entry_id=lab.entry)
    assert any(row["subentry_id"] == policy_id and row["title"] == title for row in rows)


async def prove_native_occurrence(lab, resource, policy_id, event_prefix):
    """Prove native admission, absolute expiry, and cancellation without automation entities."""
    from .runner import eventually

    async def selection():
        result = await lab.ha.service(
            "ha_operator", "explain", {"resource_id": resource}, response=True
        )
        return result["service_response"]["resources"][resource]["selection"]

    async def automation_entities():
        return {
            row["entity_id"]
            for row in await lab.ha.request("GET", "/api/states")
            if row["entity_id"].startswith(("automation.", "script."))
        }

    before = await automation_entities()
    enabled = await lab.ha.managed(policy_id, "switch")
    await lab.ha.service("switch", "turn_on", {"entity_id": enabled})
    await lab.ha.reload_entry(lab.entry)
    await lab.ha.request("POST", "/api/events/" + event_prefix + "_admit", {})
    accepted = await eventually(
        selection, lambda row: row.get("source_id") == policy_id and row.get("occurrence_id")
    )
    await eventually(selection, lambda row: row.get("source_id") != policy_id, timeout=12)
    await lab.ha.request("POST", "/api/events/" + event_prefix + "_admit", {})
    second = await eventually(selection, lambda row: row.get("source_id") == policy_id)
    assert second["occurrence_id"] != accepted["occurrence_id"]
    await lab.ha.request("POST", "/api/events/" + event_prefix + "_cancel", {})
    await eventually(selection, lambda row: row.get("source_id") != policy_id)
    assert await automation_entities() == before
    await lab.ha.service("switch", "turn_off", {"entity_id": enabled})
    return {"first": accepted, "second": second, "automation_entities": sorted(before)}


async def clear_v3_timer_input(lab, title):
    await open_subentry_editor(lab, title)
    (ARTIFACTS / (title.replace(" ", "-") + "-v3-input-ui.txt")).write_text(
        await lab.page.locator("body").aria_snapshot()
    )
    await (
        lab.page.locator("ha-selector-select")
        .filter(has_text="Policy input")
        .locator("ha-picker-field")
        .click()
    )
    await lab.page.get_by_text("Existing state or submitted occurrence", exact=True).click()
    await lab.page.screenshot(
        path=str(ARTIFACTS / "migration-v3-timer-input-cleared.png"), full_page=True
    )
    await save_subentry_editor(lab)


async def clear_v3_occurrence_eligibility(lab, title):
    await open_subentry_editor(lab, title)
    (ARTIFACTS / "migration-v3-occurrence-before-clear-ui.txt").write_text(
        await lab.page.locator("body").aria_snapshot()
    )
    # Released form order: eligibility first, target source second. The pinned
    # native picker exposes its clear affordance as an icon without a label.
    picker = lab.page.locator("ha-selector-entity").first
    await expect(picker).to_have_js_property("value", "binary_sensor.sim_cellar_policy")
    await picker.locator("ha-picker-field ha-icon-button.clear").click()
    field = lab.page.get_by_role("textbox", name="Eligible state", exact=True)
    await field.fill("")
    await field.press("Tab")
    await lab.page.screenshot(
        path=str(ARTIFACTS / "migration-v3-occurrence-eligibility-cleared.png"), full_page=True
    )
    await save_subentry_editor(lab)
