"""Read-only inventory and visual contact sheets for a local OCR PDF corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    from pypdf import PdfReader

    args.output.mkdir(parents=True, exist_ok=False)
    inspection = []
    choices = {
        "01": [1, 2, 3, 4, 5, 10, 16, 21, 26, 31, 36, 41, 46, 51, 61, 71, 74],
        "02": list(range(1, 7)), "03": list(range(1, 7)),
        "04": [7, 8, 9, 10, 11, 12], "05": [2, 3, 4],
        "06": [1, 2, 3, 4], "07": [3, 5, 7, 11, 12, 13, 14],
        "08": list(range(1, 10)),
    }
    old_hashes = {}
    old_root = args.input.parents[1] / "ocr-layout-20261019" / "inputs" / "images"
    for path in old_root.glob("*.png"):
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            digest = hashlib.sha256(rgb.tobytes()).hexdigest()
            old_hashes[(rgb.size, digest)] = path.name
    for path in sorted(args.input.glob("*.pdf")):
        document = pdfium.PdfDocument(path)
        record = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "pages": [], "embedded_image_matches": []}
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            record["pages"].append({"page": index + 1, "size": page.get_size(),
                                    "chars": len("".join(text.split())),
                                    "replacement_chars": text.count("\ufffd"),
                                    "preview": text[:200]})
            textpage.close()
            page.close()
        # These PDFs wrap public scan images. Compare pixels, not the new PDF hash.
        if path.name[:2] in {"02", "03"}:
            reader = PdfReader(path)
            for index, page in enumerate(reader.pages):
                for embedded in page.images:
                    rgb = embedded.image.convert("RGB")
                    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
                    record["embedded_image_matches"].append({
                        "page": index + 1, "size": rgb.size, "pixel_sha256": digest,
                        "previous_sample": old_hashes.get((rgb.size, digest)),
                    })
        nums = choices.get(path.name[:2], [1])
        thumb_w, thumb_h, cols = 360, 520, 4
        sheet = Image.new("RGB", (cols * thumb_w, ((len(nums) + cols - 1) // cols) * thumb_h), "#d9d9d9")
        draw = ImageDraw.Draw(sheet)
        for offset, number in enumerate(nums):
            if number > len(document):
                continue
            page = document[number - 1]
            image = page.render(scale=1).to_pil().convert("RGB")
            image.thumbnail((thumb_w - 10, thumb_h - 30))
            x, y = (offset % cols) * thumb_w, (offset // cols) * thumb_h
            sheet.paste(image, (x + (thumb_w - image.width) // 2, y + 25))
            draw.text((x + 8, y + 5), f"{path.name[:2]} / physical page {number}", fill="black")
            page.close()
        sheet.save(args.output / f"contact-{path.name[:2]}.png")
        document.close()
        inspection.append(record)
        print(path.name, len(record["pages"]), "text pages", sum(p["chars"] > 0 for p in record["pages"]), flush=True)
    (args.output / "inventory-verified.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
