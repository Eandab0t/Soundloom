"""Build signed update packages and the remote update manifest.

Workflow (run on the release machine; the signing key lives in __secret/,
which is gitignored and must never leave that machine):

    python tools/make_release.py frontend --version 0.2.3 --out dist/updates
    python tools/make_release.py youtube --version 0.2.3 \
        --file backend/sources/ytdlp_source.py=backend.sources.ytdlp_source \
        --out dist/updates
    python tools/make_release.py manifest --app-version 0.3.0 --out dist/updates

The first two build <id>-<version>.zip packages; the last one scans the
output directory, signs the aggregate manifest, and writes updates.json
ready to upload to the update source.
"""
import argparse
import base64
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_DIR = REPO_ROOT / "__secret"


class ReleaseError(Exception):
    pass


def load_signing_key(secret_dir: Path = SECRET_DIR) -> Ed25519PrivateKey:
    key_path = secret_dir / "signing_key"
    if not key_path.exists():
        raise ReleaseError(
            f"No signing key at {key_path}. Generate one with tools/sign_keygen.py."
        )
    return Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def build_package(component_id: str, version: str, package_type: str,
                  files: dict[str, bytes], provides: dict[str, str] | None = None) -> bytes:
    package_json = {
        "id": component_id,
        "name": component_id.replace("-", " ").title(),
        "version": version,
        "type": package_type,
    }
    if provides:
        package_json["provides"] = provides
    return _zip_bytes({
        "package.json": json.dumps(package_json, indent=2).encode("utf-8"),
        **files,
    })


def build_frontend_package(frontend_dir: Path, version: str) -> bytes:
    files: dict[str, bytes] = {}
    for path in sorted(frontend_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(frontend_dir).as_posix()] = path.read_bytes()
    if "index.html" not in files:
        raise ReleaseError("frontend directory has no index.html")
    return build_package("frontend", version, "frontend", files)


def build_module_package(component_id: str, version: str,
                         provides: dict[str, str]) -> bytes:
    """provides maps repo file path -> dotted module name it replaces."""
    files: dict[str, bytes] = {}
    for repo_path in provides:
        source = REPO_ROOT / repo_path
        if not source.exists():
            raise ReleaseError(f"module source not found: {repo_path}")
        files[Path(repo_path).name] = source.read_bytes()
    return build_package(component_id, version, "integration", files, provides=provides)


def build_manifest(packages: list[tuple[str, bytes]], app_version: str,
                   core_compatibility: str, key: Ed25519PrivateKey,
                   release_notes: str = "",
                   restart_by_type: dict[str, bool] | None = None) -> dict:
    restart_by_type = restart_by_type or {
        "core": True, "frontend": False, "integration": True,
    }
    components = []
    for name, package_bytes in packages:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            package = json.loads(archive.read("package.json").decode("utf-8"))
        components.append({
            "id": package["id"],
            "version": package["version"],
            "core_compatibility": core_compatibility,
            "dependencies": package.get("dependencies", []),
            "restart_required": restart_by_type.get(package["type"], True),
            "release_notes": "",
            "package_url": f"packages/{name}",
            "sha256": hashlib.sha256(package_bytes).hexdigest(),
        })
    body = {
        "manifest_version": 1,
        "app_version": app_version,
        "released_at": "",
        "release_notes": release_notes,
        "components": components,
    }
    body["signature"] = base64.b64encode(
        key.sign(_canonical(body))
    ).decode("ascii")
    return body


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_frontend = sub.add_parser("frontend")
    p_frontend.add_argument("--version", required=True)
    p_frontend.add_argument("--out", required=True)
    p_frontend.add_argument("--frontend-dir", default=str(REPO_ROOT / "frontend"))

    p_module = sub.add_parser("module")
    p_module.add_argument("--id", required=True)
    p_module.add_argument("--version", required=True)
    p_module.add_argument("--out", required=True)
    p_module.add_argument(
        "--file", action="append", required=True,
        metavar="REPO_PATH=DOTTED.MODULE",
        help="source file and the module name it replaces (repeatable)",
    )

    p_manifest = sub.add_parser("manifest")
    p_manifest.add_argument("--app-version", required=True)
    p_manifest.add_argument("--out", required=True)
    p_manifest.add_argument("--core-compatibility", default=">=0.2.2")
    p_manifest.add_argument("--release-notes", default="")

    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "frontend":
        package = build_frontend_package(Path(args.frontend_dir), args.version)
        name = f"frontend-{args.version}.zip"
        (out_dir / name).write_bytes(package)
        print(f"wrote {out_dir / name}")
        return 0

    if args.command == "module":
        provides = dict(
            part.split("=", 1) for part in args.file
        )
        package = build_module_package(args.id, args.version, provides)
        name = f"{args.id}-{args.version}.zip"
        (out_dir / name).write_bytes(package)
        print(f"wrote {out_dir / name}")
        return 0

    key = load_signing_key()
    packages = [
        (path.name, path.read_bytes())
        for path in sorted(out_dir.glob("*.zip"))
    ]
    if not packages:
        print("no packages found in output dir; build some first", file=sys.stderr)
        return 1
    manifest = build_manifest(
        packages, args.app_version, args.core_compatibility, key,
        release_notes=args.release_notes,
    )
    manifest_path = out_dir / "updates.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {manifest_path} with {len(packages)} component(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
