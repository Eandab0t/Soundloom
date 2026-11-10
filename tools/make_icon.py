"""Generate Soundloom's Windows icon set from the brand mark.

Sources frontend/img/logo.png (the 1080x1080 SoundLoom mark) and writes
every size the browser favicons and the Windows exe need:

  frontend/img/favicon-{16,32,48,128,180}.png, frontend/img/favicon.ico
  icon.ico (repo root, for the PyInstaller exe)

Run once after changing logo.png; the outputs are committed with the repo.
"""
from PIL import Image
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "frontend" / "img" / "logo.png"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    logo = Image.open(LOGO).convert("RGBA")
    img_dir = ROOT / "frontend" / "img"

    for size in (16, 32, 48, 128, 180):
        logo.resize((size, size), Image.LANCZOS).save(
            img_dir / f"favicon-{size}x{size}.png"
        )

    logo.resize((256, 256), Image.LANCZOS).save(
        img_dir / "favicon.ico",
        sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
    )
    logo.resize((256, 256), Image.LANCZOS).save(
        ROOT / "icon.ico",
        sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
    )
    print(f"icon set written from {LOGO.name}")


if __name__ == "__main__":
    main()
