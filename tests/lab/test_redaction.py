"""Host-side redaction regressions, runnable without HA or Docker."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.lab.redaction import REDACTED, sanitize_artifacts


class ArtifactRedactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_redacts_late_docker_logs_exact_secret_and_pin(self):
        logs = self.root / "docker" / "ha.log"
        logs.parent.mkdir()
        logs.write_text("token=opaque-secret; HAP PIN: 123-45-678\n", encoding="utf-8")
        sanitize_artifacts(self.root, ["opaque-secret"])
        self.assertEqual(logs.read_text(), f"token={REDACTED}; HAP PIN: {REDACTED}\n")

    def test_generic_bearer_jwt_and_pairing_keys_preserve_json(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJoYSJ9.abcdefgHIJKLMNOP"
        path = self.root / "trace.json"
        path.write_text(
            json.dumps(
                {
                    "header": "Bearer unlisted-token_123",
                    "jwt": jwt,
                    "iOSDeviceLTSK": "private-material",
                    "AccessoryLTPK": "pairing-material",
                    "state": "on",
                }
            )
        )
        sanitize_artifacts(self.root, [])
        content = json.loads(path.read_text())
        self.assertEqual(
            content,
            {
                "header": f"Bearer {REDACTED}",
                "jwt": REDACTED,
                "iOSDeviceLTSK": REDACTED,
                "AccessoryLTPK": REDACTED,
                "state": "on",
            },
        )

    def test_redacts_zip_text_and_comments_preserves_image_member(self):
        path = self.root / "trace.zip"
        image = b"\x89PNG\r\n\x1a\n\x00secret-value123-45-678"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.comment = b"secret-value"
            info = zipfile.ZipInfo("trace.network")
            info.comment = b"secret-value"
            archive.writestr(info, '{"token": "secret-value", "pin": "123-45-678"}')
            archive.writestr("resources/screenshot.png", image)
        sanitize_artifacts(self.root, ["secret-value"])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.comment, REDACTED.encode())
            self.assertEqual(archive.getinfo("trace.network").comment, REDACTED.encode())
            self.assertEqual(
                json.loads(archive.read("trace.network")), {"token": REDACTED, "pin": REDACTED}
            )
            self.assertEqual(archive.read("resources/screenshot.png"), image)

    def test_preserves_images_and_non_utf_binary(self):
        images = {
            "image.png": b"secret-value123-45-678",
            "image-no-extension": b"GIF89asecret-value",
            "data.bin": b"\xff\x00secret-value",
        }
        for name, contents in images.items():
            (self.root / name).write_bytes(contents)
        sanitize_artifacts(self.root, ["secret-value"])
        for name, contents in images.items():
            self.assertEqual((self.root / name).read_bytes(), contents)

    def test_control_receipt_and_symlink_are_untouched(self):
        control = self.root / "control"
        control.mkdir()
        receipt = control / "secrets.json"
        receipt.write_text('["secret-value"]')
        link = self.root / "receipt-link.json"
        link.symlink_to(receipt)
        sanitize_artifacts(self.root, ["secret-value"])
        self.assertEqual(receipt.read_text(), '["secret-value"]')
        self.assertTrue(link.is_symlink())

    def test_json_escaped_secrets_and_unicode_text(self):
        secret = 'my"secret\\value'
        json_path = self.root / "escaped.json"
        json_path.write_text(json.dumps({"token": secret}))
        utf16_path = self.root / "utf16.log"
        utf16_path.write_bytes("HAP 123-45-678".encode("utf-16"))
        sanitize_artifacts(self.root, [secret])
        self.assertEqual(json.loads(json_path.read_text()), {"token": REDACTED})
        self.assertEqual(utf16_path.read_bytes().decode("utf-16"), f"HAP {REDACTED}")

    def test_nested_zip_and_idempotence(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("ha.log", "Bearer token-not-in-list")
        path = self.root / "trace.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("nested.zip", nested.getvalue())
        sanitize_artifacts(self.root, [])
        sanitized = path.read_bytes()
        sanitize_artifacts(self.root, [])
        self.assertEqual(path.read_bytes(), sanitized)
        with (
            zipfile.ZipFile(path) as outer,
            zipfile.ZipFile(io.BytesIO(outer.read("nested.zip"))) as inner,
        ):
            self.assertEqual(inner.read("ha.log"), f"Bearer {REDACTED}".encode())

    def test_malformed_zip_raises(self):
        (self.root / "trace.zip").write_bytes(b"not a zip")
        with self.assertRaises(zipfile.BadZipFile):
            sanitize_artifacts(self.root, [])


if __name__ == "__main__":
    unittest.main()
