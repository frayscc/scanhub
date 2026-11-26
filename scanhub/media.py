"""Synchronous media workers, invoked through the bounded thread worker."""
import io
import zipfile

from PIL import Image, ImageOps

from .storage import clean_name


def preview_bytes(root, row):
    with Image.open(root / row["path"]) as image:
        image = ImageOps.exif_transpose(image).convert("RGB").rotate(-row["rotation"], expand=True)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)
        return out.getvalue()


def thumbnail_bytes(store, row):
    path = store.thumbnail(row)
    with Image.open(path) as image:
        image = image.rotate(-row["rotation"], expand=True)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=82)
        return out.getvalue()


def rotated_png(root, row, target):
    with Image.open(root / row["path"]) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode == "CMYK":
            image = image.convert("RGB")
        image.rotate(-row["rotation"], expand=True).save(target, "PNG")


def write_zip(root, rows, target):
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
        used = set()
        for row in rows:
            name = clean_name(row["name"])
            if name.casefold() in used:
                name = f"{row['id'][:8]}-{name}"
            while name.casefold() in used:
                name = "copy-" + name
            used.add(name.casefold())
            zipped.write(root / row["path"], arcname=name)
