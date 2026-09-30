"""Only executed assertion failures can kill a safety mutant."""

from types import SimpleNamespace
from xml.etree.ElementTree import Element, ElementTree, SubElement

import pytest

from scripts.mutation_gate import run_tests


@pytest.mark.parametrize(
    ("messages", "errors", "skipped", "returncode", "expected"),
    [
        (["assert 7 == 100"], 0, 0, 1, "failed_assertions"),
        (["AssertionError: stale command"], 0, 0, 1, "failed_assertions"),
        (["Failed: DID NOT RAISE <class 'HomeAssistantError'>"], 0, 0, 1, "failed_assertions"),
        (["RuntimeError: setup failed"], 0, 0, 1, "error"),
        (["assert False", "RuntimeError: unrelated crash"], 0, 0, 1, "error"),
        (["assert False"], 1, 0, 1, "error"),
        (["assert False"], 0, 1, 1, "error"),
        ([], 0, 0, 0, "passed"),
        ([], 0, 0, 2, "error"),
    ],
)
def test_mutation_requires_only_assertion_failures(
    tmp_path, monkeypatch, messages, errors, skipped, returncode, expected
):
    def execute(command, **kwargs):
        report = next(
            item.removeprefix("--junitxml=") for item in command if item.startswith("--junitxml=")
        )
        suite = Element(
            "testsuite",
            tests="3",
            failures=str(len(messages)),
            errors=str(errors),
            skipped=str(skipped),
        )
        for message in messages:
            SubElement(SubElement(suite, "testcase"), "failure", message=message)
        ElementTree(suite).write(report)
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr("scripts.mutation_gate.subprocess.run", execute)
    result = run_tests(tmp_path, ("test_guard.py",), tmp_path, "guard", 10)
    assert result["status"] == expected
