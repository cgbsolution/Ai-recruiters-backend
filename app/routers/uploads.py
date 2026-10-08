import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth import get_current_user
from app.config import settings
from app.media import UPLOAD_ROOT, safe_upload_name
from app.services.file_compression import schedule_compression

router = APIRouter(dependencies=[Depends(get_current_user)])

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}


@router.post("/jd")
async def upload_jd(file: UploadFile = File(...)):
    # The client file name is never trusted: only its allowlisted, lower-cased
    # extension is kept, and the stored name is server-generated.
    safe = safe_upload_name(file.filename, ALLOWED_EXTENSIONS)
    if safe is None:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, and DOCX files are accepted")
    ext = safe[1]

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {settings.MAX_UPLOAD_SIZE_MB}MB limit",
        )

    filename = f"{uuid.uuid4()}{ext}"
    upload_dir = os.path.join(UPLOAD_ROOT, "jd")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, filename)

    with open(path, "wb") as f:
        f.write(content)
    schedule_compression(path)

    return {"filename": filename, "url": f"/uploads/jd/{filename}"}
