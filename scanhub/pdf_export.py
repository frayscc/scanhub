"""Image-to-PDF export without lossy image re-encoding."""
import tempfile
from pathlib import Path

import img2pdf
from pypdf import PdfReader, PdfWriter, Transformation


def build_pdf(root, rows, pages, output, a4=False):
    if sum(rows[page.id]["size"] for page in pages) > 64 * 1024 * 1024:
        raise ValueError("单次合并原件总大小不得超过 64 MiB，请分批导出")
    writer = PdfWriter()
    with tempfile.TemporaryDirectory(prefix="pdf-pages-", dir=root / "tmp") as directory:
        for index, spec in enumerate(pages):
            row = rows[spec.id]
            if not row["mime"].startswith("image/") or row["deleted_at"] is not None:
                raise ValueError("PDF 合并仅支持收件箱中的图片")
            source_pdf = Path(directory) / f"{index}.pdf"
            with source_pdf.open("wb") as out:
                try:
                    img2pdf.convert(str(root / row["path"]), outputstream=out, rotation=img2pdf.Rotation.ifvalid)
                except (img2pdf.ImageOpenError, img2pdf.UnsupportedColorspaceError,
                        img2pdf.JpegColorspaceError, img2pdf.AlphaChannelError,
                        img2pdf.NegativeDimensionError, img2pdf.PdfTooLargeError,
                        img2pdf.ExifOrientationError):
                    raise ValueError("图片无法转换为 PDF，请检查颜色空间或图片编码；原件未修改") from None
            reader = PdfReader(source_pdf)
            if len(writer.pages) + len(reader.pages) > 200:
                raise ValueError("单次合并最多 200 页（含多页 TIFF）")
            for page in reader.pages:
                angle = (row["rotation"] + spec.rotation) % 360
                if angle:
                    page.rotate(angle)
                if a4:
                    # pypdf 7 requires transformation targets to belong to a writer.
                    owner = PdfWriter()
                    page = owner.add_page(page)
                    page.transfer_rotation_to_content()
                    width, height = float(page.mediabox.width), float(page.mediabox.height)
                    # Fixed portrait A4; preserve aspect ratio with centered margins.
                    target = writer.add_blank_page(width=595.2756, height=841.8898)
                    scale = min(float(target.mediabox.width) / width, float(target.mediabox.height) / height)
                    transform = Transformation().scale(scale).translate(
                        (float(target.mediabox.width) - width * scale) / 2,
                        (float(target.mediabox.height) - height * scale) / 2)
                    target.merge_transformed_page(page, transform)
                else:
                    writer.add_page(page)
        with open(output, "wb") as out:
            writer.write(out)
