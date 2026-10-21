"""Embeds a ZIP inside a PDF as a file attachment, so one PDF carries the code with it.

    python report/tools/embed_zip.py <report.pdf> <code.zip> <out.pdf>

Needs `pip install pikepdf`. The attachment shows up in Adobe Acrobat / Reader (paperclip
panel) and in most desktop PDF viewers; browser viewers often hide it. Run it on the FINAL
PDF exported from Word. The ZIP must not contain the PDF itself (build it with
`report/tools/package_code_zip.sh`).
"""
import sys
from pathlib import Path

import pikepdf


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    pdf_path, zip_path, out_path = (Path(a) for a in sys.argv[1:])
    with pikepdf.open(pdf_path) as pdf:
        # the MIME type (application/zip) is guessed from the file name
        spec = pikepdf.AttachedFileSpec.from_filepath(pdf, zip_path, description="HomeGuard source code")
        pdf.attachments[zip_path.name] = spec
        pdf.save(out_path)
    with pikepdf.open(out_path) as pdf:
        names = list(pdf.attachments.keys())
    print(f"{out_path}: {out_path.stat().st_size / 1_048_576:.1f} MiB, attachments: {names}")


if __name__ == "__main__":
    main()
