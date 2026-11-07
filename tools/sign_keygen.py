"""Generate an ed25519 keypair for update-manifest signing.

The PRIVATE key signs update manifests. It must never live in the
repository, in the release machine's ordinary app folders, or anywhere the
packaged application can read. Default output: ./__secret/ (gitignored).

    python tools/sign_keygen.py                 # writes __secret/signing_key + signing_key.pub
    python tools/sign_keygen.py --show-public   # prints the public key hex for embedding

The public key hex gets embedded in backend/updater/security.py
(UPDATE_SIGNING_PUBKEY) so every installed app can verify manifests.
"""
import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SECRET_DIR = Path(__file__).resolve().parent.parent / "__secret"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-public", action="store_true",
        help="print the public key hex of an existing keypair and exit",
    )
    args = parser.parse_args()

    private_path = SECRET_DIR / "signing_key"
    if args.show_public:
        if not private_path.exists():
            print("no keypair found; run without --show-public first", file=sys.stderr)
            return 1
        private_bytes = private_path.read_bytes()
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        print(private_key.public_key().public_bytes_raw().hex())
        return 0

    if private_path.exists():
        print(f"keypair already exists at {private_path}; refusing to overwrite", file=sys.stderr)
        return 1

    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_path.write_bytes(private_key.private_bytes_raw())
    public_path = SECRET_DIR / "signing_key.pub"
    public_path.write_text(
        private_key.public_key().public_bytes_raw().hex() + "\n", encoding="ascii"
    )
    print(f"private key: {private_path}  (keep outside the repository)")
    print(f"public key:  {public_path}")
    print("public key hex (embed in backend/updater/security.py):")
    print(private_key.public_key().public_bytes_raw().hex())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
