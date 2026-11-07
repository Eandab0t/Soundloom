"""Update security primitives: hashes, signatures, safe extraction.

Everything here is fail-closed: an unconfigured key, a bad signature, a
mismatched hash or a path-traversal entry raises or returns False — never
a silent pass.
"""
from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class SecurityError(Exception):
    """Raised when an update package or signature fails verification."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_payload_bytes(payload: dict) -> bytes:
    """Deterministic byte form of a JSON dict, used for signing.

    sort_keys + tight separators mean the same dict always produces the
    same bytes on any machine, which is what makes signatures stable.
    """
    import json
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_signature(payload: bytes, signature_b64: str, public_key_hex: str) -> bool:
    """Verify an ed25519 signature (base64) over payload with a hex pubkey.

    Returns False — never raises — for malformed keys, signatures or a
    missing key configuration, so callers can treat every failure the same.
    """
    if not public_key_hex or not signature_b64:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_manifest_signature(manifest: dict, public_key_hex: str) -> bool:
    """Verify the 'signature' field of an update manifest over its body."""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    signature = manifest.get("signature", "")
    return verify_signature(canonical_payload_bytes(body), signature, public_key_hex)


def _safe_target(dest_dir: Path, archive_name: str) -> Path:
    """Resolve an archive entry inside dest_dir, refusing escapes."""
    target = (dest_dir / archive_name).resolve()
    if not target.is_relative_to(dest_dir.resolve()):
        raise SecurityError(f"archive entry escapes destination: {archive_name!r}")
    return target


def safe_extract(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract a zip into dest_dir, refusing path traversal and overruns.

    Every entry is resolved and checked to stay inside dest_dir before
    anything is written. Absolute paths, '..' segments and drive-absolute
    names are all rejected by the containment check.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = _safe_target(dest_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            written.append(target)
    return written
