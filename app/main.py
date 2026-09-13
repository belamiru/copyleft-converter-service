import asyncio
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

app = FastAPI(title="Copyleft Converter", docs_url=None, redoc_url=None)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_CONVERTED_PDF_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_PAGE_COUNT = 10_000
MAX_OFFICE_ARCHIVE_FILES = 10_000
MAX_OFFICE_UNCOMPRESSED_SIZE = 350 * 1024 * 1024  # 350 MB
MAX_COMPRESSION_RATIO = 100

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}

OFFICE_REQUIRED_FILES = {
    ".docx": {"[content_types].xml", "word/document.xml"},
    ".xlsx": {"[content_types].xml", "xl/workbook.xml"},
    ".pptx": {"[content_types].xml", "ppt/presentation.xml"},
}

conversion_lock = asyncio.Lock()


def check_api_key(authorization: str | None) -> None:
    expected_key = os.environ.get("CONVERTER_API_KEY")

    if not expected_key:
        raise HTTPException(
            status_code=500,
            detail="Converter is not configured",
        )

    if authorization != f"Bearer {expected_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_pdf_page_count(pdf_path: Path) -> int:
    try:
        page_count = len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="The PDF could not be read",
        ) from exc

    if page_count < 1:
        raise HTTPException(
            status_code=422,
            detail="The PDF does not contain pages",
        )

    if page_count > MAX_PAGE_COUNT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The document contains more than "
                f"{MAX_PAGE_COUNT} pages"
            ),
        )

    return page_count


def ensure_pdf_file(input_path: Path) -> None:
    try:
        with input_path.open("rb") as source:
            signature = source.read(5)
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail="The uploaded PDF could not be read",
        ) from exc

    if signature != b"%PDF-":
        raise HTTPException(
            status_code=422,
            detail="The file content does not match the PDF format",
        )


def ensure_safe_office_package(input_path: Path, extension: str) -> None:
    required_files = OFFICE_REQUIRED_FILES[extension]

    try:
        with zipfile.ZipFile(input_path) as archive:
            entries = archive.infolist()

            if len(entries) > MAX_OFFICE_ARCHIVE_FILES:
                raise HTTPException(
                    status_code=422,
                    detail="The Office document contains too many internal files",
                )

            normalized_names = set()
            total_uncompressed_size = 0
            total_compressed_size = 0

            for entry in entries:
                name = entry.filename.replace("\\", "/").lstrip("/")

                if not name or name.endswith("/"):
                    continue

                path_parts = PurePosixPath(name).parts

                if ".." in path_parts:
                    raise HTTPException(
                        status_code=422,
                        detail="The Office document contains an unsafe internal path",
                    )

                normalized_name = name.lower()
                normalized_names.add(normalized_name)

                if entry.flag_bits & 0x1:
                    raise HTTPException(
                        status_code=422,
                        detail="Password-protected Office documents are not supported",
                    )

                if normalized_name.endswith("vbaproject.bin"):
                    raise HTTPException(
                        status_code=422,
                        detail="Office documents containing macros are not supported",
                    )

                total_uncompressed_size += entry.file_size
                total_compressed_size += entry.compress_size

                if total_uncompressed_size > MAX_OFFICE_UNCOMPRESSED_SIZE:
                    raise HTTPException(
                        status_code=422,
                        detail="The Office document is too large after unpacking",
                    )

            if not required_files.issubset(normalized_names):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"The file content does not match the "
                        f"{extension[1:].upper()} format"
                    ),
                )

            if total_uncompressed_size > 0:
                if total_compressed_size == 0:
                    raise HTTPException(
                        status_code=422,
                        detail="The Office document has an unsafe compression ratio",
                    )

                compression_ratio = (
                    total_uncompressed_size / total_compressed_size
                )

                if compression_ratio > MAX_COMPRESSION_RATIO:
                    raise HTTPException(
                        status_code=422,
                        detail="The Office document has an unsafe compression ratio",
                    )

    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The file content does not match the "
                f"{extension[1:].upper()} format"
            ),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail="The uploaded Office document could not be read",
        ) from exc


def validate_input_file(input_path: Path, extension: str) -> None:
    if extension == ".pdf":
        ensure_pdf_file(input_path)
        return

    ensure_safe_office_package(input_path, extension)


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

            file_size = 0

            with input_path.open("wb") as destination:
                while chunk := await file.read(1024 * 1024):
                    file_size += len(chunk)

                    if file_size > MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail="The file is larger than 50 MB",
                        )

                    destination.write(chunk)

            if file_size == 0:
                raise HTTPException(
                    status_code=422,
                    detail="The uploaded file is empty",
                )

            validate_input_file(input_path, extension)

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

            try:
                pdf_size = pdf_path.stat().st_size
            except OSError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="The converted PDF could not be read",
                ) from exc

            if pdf_size < 1:
                raise HTTPException(
                    status_code=422,
                    detail="The converted PDF is empty",
                )

            if pdf_size > MAX_CONVERTED_PDF_SIZE:
                raise HTTPException(
                    status_code=422,
                    detail="The converted PDF is larger than 100 MB",
                )

            try:
                pdf_bytes = pdf_path.read_bytes()
            except OSError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="The converted PDF could not be read",
                ) from exc

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": 'attachment; filename="converted.pdf"',
                    "X-Page-Count": str(pages),
                },
            )