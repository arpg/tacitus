#!/usr/bin/env python3
"""
Compile a LaTeX letter on the institution's letterhead template.

Usage:
  python3 compile_letter.py --tex /path/to/letter.tex --output /path/to/letter.pdf

The template assets (a letterhead .sty, logos, the signature) live in
the directory the profile's institution block names
(`institution.letterhead.template_dir`, default ~/.ea/templates/letterhead)
and are symlinked into the build directory automatically. The block's
`required_assets` lists the files a letter cannot compile without; each
is taken from the template directory, else from the first of the block's
`asset_dirs` that holds it (an institution plugin's letterhead files).

Every build appends one line to ~/.ea/state/letter-builds.jsonl (the PDF,
its sha256 and the .tex), so doc_text.py can name a PDF's source even
after the PDF is copied or renamed into another folder.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import institution  # noqa: E402
import resident_io  # noqa: E402

_LETTERHEAD = institution.letterhead()
TEMPLATE_DIR = _LETTERHEAD["template_dir"]
ASSET_DIRS = _LETTERHEAD["asset_dirs"]
REQUIRED_ASSETS = _LETTERHEAD["required_assets"]
# The signature image is the user's own; every PDF whose name ends in
# "Signature.pdf" in the template directory is linked in beside the assets.
SIGNATURE_GLOB = "*Signature.pdf"
BUILDS_FILE = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "state" / "letter-builds.jsonl"


def record_build(tex_path: Path, pdf_path: Path, builds_file: Path | None = None) -> None:
    """Append the build to the builds file. A failure only warns: the PDF
    is already written."""
    builds_file = builds_file or BUILDS_FILE
    try:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "pdf": str(pdf_path), "tex": str(tex_path),
               "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest()}
    except OSError as e:
        print(f"Warning: build not recorded: {e}", file=sys.stderr)
        return
    if not resident_io.append_text(builds_file, json.dumps(row) + "\n"):
        print(f"Warning: build not recorded in {builds_file}", file=sys.stderr)


def find_asset(name: str, template_dir: Path = None, asset_dirs=None):
    """The path of a required asset: the template directory first, then
    each asset directory in order. None when no directory holds it."""
    template_dir = TEMPLATE_DIR if template_dir is None else Path(template_dir)
    asset_dirs = ASSET_DIRS if asset_dirs is None else asset_dirs
    for d in [template_dir, *asset_dirs]:
        src = Path(d) / name
        if src.exists():
            return src
    return None


def compile_letter(tex_path: str, output_path: str) -> bool:
    tex_path = Path(tex_path).resolve()
    output_path = Path(output_path).resolve()

    if not tex_path.exists():
        print(f"Error: {tex_path} not found", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory(prefix="letter-") as tmpdir:
        tmp = Path(tmpdir)

        shutil.copy2(tex_path, tmp / tex_path.name)

        for asset in REQUIRED_ASSETS:
            src = find_asset(asset)
            if src is not None:
                os.symlink(src, tmp / asset)
            else:
                where = ", ".join(str(d) for d in [TEMPLATE_DIR, *ASSET_DIRS])
                print(f"Warning: template asset {asset} not found in {where}", file=sys.stderr)
        signatures = sorted(TEMPLATE_DIR.glob(SIGNATURE_GLOB)) if TEMPLATE_DIR.exists() else []
        for src in signatures:
            os.symlink(src, tmp / src.name)
        if not signatures:
            print(f"Warning: no {SIGNATURE_GLOB} in {TEMPLATE_DIR}; the letter compiles without a signature image",
                  file=sys.stderr)

        for i in range(2):  # twice, so references resolve
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", tex_path.name],
                cwd=str(tmp),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 and i == 1:
                print("pdflatex failed:", file=sys.stderr)
                print(result.stdout[-1000:], file=sys.stderr)
                return False

        pdf_name = tex_path.stem + ".pdf"
        built_pdf = tmp / pdf_name
        if built_pdf.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built_pdf, output_path)
            record_build(tex_path, output_path)
            print(f"PDF written to: {output_path}")
            return True
        else:
            print("Error: PDF not generated", file=sys.stderr)
            return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile a letter on the institution letterhead", allow_abbrev=False)
    parser.add_argument("--tex", required=True, help="Path to .tex file")
    parser.add_argument("--output", required=True, help="Output PDF path")
    args = parser.parse_args()
    success = compile_letter(args.tex, args.output)
    sys.exit(0 if success else 1)
