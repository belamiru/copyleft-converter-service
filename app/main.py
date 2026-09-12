import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

app = FastAPI(title="Copyleft Converter", docs_url=None, redoc_url=None)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}
conversion_lock = asyncio.Lock()


def check_api_key(authorization: str | None) -> None:
    expected_key = os.environ.get("CONVERTER_API_KEY")

    if not expected_key:
        raise HTTPException(status_code=500, detail="Converter is not configured")

    if authorization != f"Bearer {expected_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_pdf_page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="The resulting PDF could not be read",
        ) from exc


@app.get("/internal/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/convert")
async def convert_file(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    check_api_key(authorization)

    original_name = Path(file.filename or "document").name
    extension = Path(original_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Supported formats: PDF, DOCX, XLSX, PPTX",
        )

    async with conversion_lock:
        with tempfile.TemporaryDirectory(dir="/tmp/converter") as temp_dir:
            work_dir = Path(temp_dir)
            input_path = work_dir / f"source{extension}"
            output_dir = work_dir / "output"
            output_dir.mkdir()

            size = 0
            with input_path.open("wb") as destination:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail="The file is larger than 50 MB",
                        )
                    destination.write(chunk)

            if size == 0:
                raise HTTPException(status_code=422, detail="The uploaded file is empty")

            if extension == ".pdf":
                pdf_path = input_path
            else:
                try:
                    subprocess.run(
                        [
                            "libreoffice",
                            "--headless",
                            "--nologo",
                            "--nofirststartwizard",
                            "--nodefault",
                            "--nolockcheck",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            str(output_dir),
                            str(input_path),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=90,
                        env={
                            **os.environ,
                            "HOME": str(work_dir),
                            "TMPDIR": str(work_dir),
                        },
                    )
                except subprocess.TimeoutExpired as exc:
                    raise HTTPException(
                        status_code=422,
                        detail="Conversion took too long",
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail="The document could not be converted",
                    ) from exc

                pdf_candidates = list(output_dir.glob("*.pdf"))
                if len(pdf_candidates) != 1:
                    raise HTTPException(
                        status_code=422,
                        detail="The document could not be converted to PDF",
                    )

                pdf_path = pdf_candidates[0]

            pages = get_pdf_page_count(pdf_path)
            download_name = f"{Path(original_name).stem}.pdf"

            pdf_bytes = pdf_path.read_bytes()

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": 'attachment; filename="converted.pdf"',
                    "X-Page-Count": str(pages),
                },
            )
