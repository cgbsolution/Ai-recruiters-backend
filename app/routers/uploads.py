import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth import get_current_user
from app.config import settings

router = APIRouter(dependencies=[Depends(get_current_user)])

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}


@router.post("/jd")
async def upload_jd(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, and DOCX files are accepted")

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {settings.MAX_UPLOAD_SIZE_MB}MB limit",
        )

    filename = f"{uuid.uuid4()}{ext}"
    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    upload_dir = "/tmp/uploads/jd" if _is_serverless else settings.UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, filename)

    with open(path, "wb") as f:
        f.write(content)

    return {"filename": filename, "url": f"/uploads/jd/{filename}"}
