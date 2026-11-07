"""Tests for backend/updater/security: hashes, signatures, safe extraction."""
import base64
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.updater import security
from backend.updater.security import SecurityError


@pytest.fixture()
def keypair():
    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes_raw().hex()
    return private_key, public_hex


def _sign(private_key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(private_key.sign(data)).decode("ascii")


class TestSha256:
    def test_known_vector(self):
        assert security.sha256_bytes(b"hello") == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_file_hash_matches_bytes(self, tmp_path):
        target = tmp_path / "blob.bin"
        target.write_bytes(b"soundloom update package")
        assert security.sha256_file(target) == security.sha256_bytes(
            b"soundloom update package"
        )


class TestSignature:
    def test_valid_signature_verifies(self, keypair):
        private_key, public_hex = keypair
        payload = security.canonical_payload_bytes({"a": 1, "b": [2, 3]})
        assert security.verify_signature(payload, _sign(private_key, payload), public_hex)

    def test_tampered_payload_fails(self, keypair):
        private_key, public_hex = keypair
        payload = security.canonical_payload_bytes({"version": "1.0.0"})
        tampered = security.canonical_payload_bytes({"version": "9.9.9"})
        assert not security.verify_signature(tampered, _sign(private_key, payload), public_hex)

    def test_wrong_key_fails(self, keypair):
        private_key, _ = keypair
        _, other_public = keypair  # same key — build a truly different one below
        other_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        payload = b"payload"
        assert not security.verify_signature(payload, _sign(private_key, payload), other_public)

    def test_malformed_inputs_fail_closed(self, keypair):
        _, public_hex = keypair
        assert not security.verify_signature(b"x", "not-base64!!", public_hex)
        assert not security.verify_signature(b"x", base64.b64encode(b"short"), "zz")
        assert not security.verify_signature(b"x", "", "")
        assert not security.verify_signature(b"x", base64.b64encode(b"sig"), "")


class TestManifestSignature:
    def test_round_trip(self, keypair):
        private_key, public_hex = keypair
        manifest = {
            "manifest_version": 1,
            "app_version": "0.3.0",
            "components": [{"id": "youtube", "version": "0.2.3"}],
        }
        body = {k: v for k, v in manifest.items()}
        manifest["signature"] = _sign(
            private_key, security.canonical_payload_bytes(body)
        )
        assert security.verify_manifest_signature(manifest, public_hex)

    def test_extra_field_breaks_signature(self, keypair):
        private_key, public_hex = keypair
        manifest = {"app_version": "0.3.0"}
        manifest["signature"] = _sign(private_key, security.canonical_payload_bytes(manifest))
        manifest["injected"] = True
        assert not security.verify_manifest_signature(manifest, public_hex)


class TestSafeExtract:
    def _zip_with(self, tmp_path, entries):
        archive_path = tmp_path / "pkg.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return archive_path

    def test_extracts_normal_entries(self, tmp_path):
        archive = self._zip_with(tmp_path, {
            "pkg/manifest.json": "{}",
            "pkg/code.py": "print('hi')",
        })
        dest = tmp_path / "install"
        written = security.safe_extract(archive, dest)
        assert (dest / "pkg" / "code.py").read_text() == "print('hi')"
        assert len(written) == 2

    @pytest.mark.parametrize("evil", [
        "../escape.py",
        "..\\escape.py",
        "a/../../escape.py",
    ])
    def test_traversal_entries_rejected(self, tmp_path, evil):
        archive = self._zip_with(tmp_path, {evil: "malicious"})
        dest = tmp_path / "install"
        with pytest.raises(SecurityError):
            security.safe_extract(archive, dest)
        assert not (tmp_path / "escape.py").exists()

    def test_absolute_entry_rejected(self, tmp_path):
        archive = self._zip_with(tmp_path, {"C:\\Windows\\evil.py": "x"})
        with pytest.raises(SecurityError):
            security.safe_extract(archive, tmp_path / "install")

    def test_partial_extraction_stops_at_first_evil_entry(self, tmp_path):
        # evil entry comes first; nothing may be written.
        archive = self._zip_with(tmp_path, {"../evil.py": "x", "ok.txt": "fine"})
        dest = tmp_path / "install"
        with pytest.raises(SecurityError):
            security.safe_extract(archive, dest)
        assert not (dest / "ok.txt").exists()
