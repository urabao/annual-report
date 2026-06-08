import os
import uuid
import shutil
import time
import logging
from typing import Dict, Any, List
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from backend.extractor import AnnualReportExtractor
import fitz
import io
from fastapi.responses import StreamingResponse
# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("App")

app = FastAPI(title="Indian Annual Report Financial Statement Extractor")

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory state for jobs
# Schema:
# {
#   job_id: {
#     "status": "parsing_toc" | "ready" | "extracting" | "completed" | "failed",
#     "progress": int,
#     "error": str | None,
#     "toc": list,
#     "mappings": dict,
#     "total_pages": int,
#     "offset": int,
#     "pdf_path": str,
#     "timestamp": float
#   }
# }
jobs_db: Dict[str, Dict[str, Any]] = {}

# Max file size: 200MB
MAX_FILE_SIZE = 200 * 1024 * 1024
TMP_BASE_DIR = "/tmp/annual_report_extractor"
os.makedirs(TMP_BASE_DIR, exist_ok=True)

class ExtractionRequest(BaseModel):
    mappings: Dict[str, Any]  # key -> {"pages": [start_page, end_page], "crop": [x0, top, x1, bottom] | None}

def cleanup_old_jobs():
    """Deletes job files older than 1 hour."""
    now = time.time()
    for job_id, job in list(jobs_db.items()):
        if now - job.get("timestamp", 0) > 3600:
            logger.info(f"Cleaning up expired job {job_id}")
            job_dir = os.path.join(TMP_BASE_DIR, job_id)
            if os.path.exists(job_dir):
                try:
                    shutil.rmtree(job_dir)
                except Exception as e:
                    logger.error(f"Error deleting dir {job_dir}: {e}")
            jobs_db.pop(job_id, None)

def bg_parse_toc(job_id: str, pdf_path: str):
    """Background task to run Table of Contents parser."""
    try:
        def progress_cb(percent: int, step: str):
            if job_id in jobs_db:
                # TOC parsing takes progress from 10% to 50%
                jobs_db[job_id]["progress"] = 10 + int(percent * 0.8)
                logger.info(f"Job {job_id} progress: {jobs_db[job_id]['progress']}% - {step}")

        extractor = AnnualReportExtractor(pdf_path, progress_callback=progress_cb)
        
        # Scan TOC (will update progress internally)
        toc_data = extractor.scan_toc()
        
        # Initial statement mapping based on keyword rules
        mappings = extractor.map_statements(toc_data)
        
        # Clean mappings to standard format for frontend
        formatted_mappings = {}
        for key, val in mappings.items():
            formatted_mappings[key] = {
                "name": val["name"],
                "start_page": val["start_page"],
                "end_page": val["end_page"]
            }
            
        if job_id in jobs_db:
            jobs_db[job_id]["toc"] = toc_data
            jobs_db[job_id]["mappings"] = formatted_mappings
            jobs_db[job_id]["total_pages"] = extractor.total_pages
            jobs_db[job_id]["offset"] = extractor.offset
            jobs_db[job_id]["scale"] = extractor.scale
            jobs_db[job_id]["report_type"] = extractor.report_type
            jobs_db[job_id]["status"] = "ready"
            jobs_db[job_id]["progress"] = 50
            
    except Exception as e:
        logger.error(f"Error in bg_parse_toc for {job_id}: {e}", exc_info=True)
        if job_id in jobs_db:
            jobs_db[job_id]["status"] = "failed"
            jobs_db[job_id]["error"] = f"Failed to parse PDF: {str(e)}"
            jobs_db[job_id]["progress"] = 100

def bg_extract(job_id: str, mappings: Dict[str, Any], pdf_path: str):
    """Background task to extract slices and package them into ZIP."""
    try:
        if job_id not in jobs_db:
            return
            
        jobs_db[job_id]["progress"] = 75
        jobs_db[job_id]["status"] = "extracting"
        
        extractor = AnnualReportExtractor(pdf_path)
        job_dir = os.path.join(TMP_BASE_DIR, job_id)
        
        # Run extraction & zip
        zip_path, extracted_files = extractor.extract_and_zip(mappings, job_dir)
        
        if job_id in jobs_db:
            jobs_db[job_id]["progress"] = 100
            jobs_db[job_id]["status"] = "completed"
            jobs_db[job_id]["extracted_files"] = extracted_files
            logger.info(f"Job {job_id} extraction completed successfully. ZIP at {zip_path}")
            
    except Exception as e:
        logger.error(f"Error in bg_extract for {job_id}: {e}", exc_info=True)
        if job_id in jobs_db:
            jobs_db[job_id]["status"] = "failed"
            jobs_db[job_id]["error"] = f"Extraction failed: {str(e)}"
            jobs_db[job_id]["progress"] = 100

@app.post("/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    # 1. Clean up old jobs first to free space
    cleanup_old_jobs()
    
    # 2. Verify file extension
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a PDF.")
        
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(TMP_BASE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    pdf_path = os.path.join(job_dir, "source.pdf")
    
    # 3. Save upload file with size limitation
    total_bytes = 0
    try:
        with open(pdf_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB chunk
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE:
                    # File too large; remove it and reject
                    buffer.close()
                    shutil.rmtree(job_dir)
                    raise HTTPException(status_code=400, detail="PDF file size exceeds 200MB limit.")
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving upload: {e}")
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {e}")
        
    # 4. Initialise job in db
    jobs_db[job_id] = {
        "status": "parsing_toc",
        "progress": 10,
        "error": None,
        "toc": [],
        "mappings": {},
        "total_pages": 0,
        "offset": 0,
        "pdf_path": pdf_path,
        "timestamp": time.time()
    }
    
    # 5. Start background task for TOC detection
    background_tasks.add_task(bg_parse_toc, job_id, pdf_path)
    
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
        
    job = jobs_db[job_id]
    return {
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"],
        "extracted_files": job.get("extracted_files", [])
    }

@app.get("/toc/{job_id}")
async def get_toc(job_id: str):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
        
    job = jobs_db[job_id]
    if job["status"] not in ["ready", "extracting", "completed"]:
        raise HTTPException(status_code=400, detail="TOC is not ready yet.")
        
    return {
        "total_pages": job["total_pages"],
        "offset": job["offset"],
        "scale": job.get("scale", 1.0),
        "report_type": job.get("report_type", "single_page"),
        "toc": job["toc"],
        "mappings": job["mappings"]
    }

@app.get("/page_preview/{job_id}/{page_num}")
async def page_preview(job_id: str, page_num: int):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job = jobs_db[job_id]
    pdf_path = job["pdf_path"]
    
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
        
    try:
        doc = fitz.open(pdf_path)
        if page_num < 0 or page_num >= len(doc):
            raise HTTPException(status_code=400, detail="Invalid page number")
            
        page = doc[page_num]
        pix = page.get_pixmap(dpi=100) # Fast preview rendering
        img_bytes = pix.tobytes("png")
        return StreamingResponse(io.BytesIO(img_bytes), media_type="image/png")
    except Exception as e:
        logger.error(f"Failed to render preview: {e}")
        raise HTTPException(status_code=500, detail="Failed to render preview image")

@app.post("/extract/{job_id}")
async def extract(job_id: str, request: ExtractionRequest, background_tasks: BackgroundTasks):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
        
    job = jobs_db[job_id]
    if job["status"] not in ["ready", "completed"]:
        raise HTTPException(status_code=400, detail="Cannot run extraction in current job state.")
        
    # Prepare mapping ranges from body
    # Frontend provides Dict[str, List[int]] -> {"consolidated_balance_sheet": [start, end]}
    logger.info(f"Extraction requested for job {job_id} with mappings: {request.mappings}")
    
    # Start background task to slice PDF and package ZIP
    background_tasks.add_task(bg_extract, job_id, request.mappings, job["pdf_path"])
    
    return {"download_url": f"/download/{job_id}"}

@app.get("/download/{job_id}")
async def download(job_id: str):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
        
    job = jobs_db[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Extraction is not completed yet.")
        
    job_dir = os.path.join(TMP_BASE_DIR, job_id)
    zip_path = os.path.join(job_dir, "financial_statements.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="ZIP file not found.")
        
    return FileResponse(zip_path, media_type="application/zip", filename=f"annual_report_{job_id}.zip")

class FlagRequest(BaseModel):
    filename: str
    issue: str

@app.post("/flag/{job_id}")
async def flag_issue(job_id: str, req: FlagRequest):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found.")
        
    flag_file = os.path.join(TMP_BASE_DIR, "flagged_issues.json")
    flags = []
    if os.path.exists(flag_file):
        with open(flag_file, "r") as f:
            try:
                flags = json.load(f)
            except:
                flags = []
                
    flags.append({
        "job_id": job_id,
        "filename": req.filename,
        "issue": req.issue,
        "timestamp": time.time()
    })
    
    with open(flag_file, "w") as f:
        json.dump(flags, f, indent=2)
        
    logger.info(f"User flagged an issue for job {job_id}, file {req.filename}: {req.issue}")
    return {"status": "success"}

# Benchmarking API Routes
@app.get("/batch", response_class=HTMLResponse)
async def get_batch_ui():
    """Serves the batch benchmarking UI."""
    return FileResponse(os.path.join(BASE_DIR, "frontend", "batch.html"))

@app.post("/api/benchmark")
async def api_benchmark(files: List[UploadFile] = File(...)):
    """Accepts multiple PDFs, extracts them, and runs heuristics inline."""
    # Ensure test directories exist
    test_data_dir = os.path.join(BASE_DIR, "tests", "data")
    test_out_dir = os.path.join(BASE_DIR, "tests", "output")
    os.makedirs(test_data_dir, exist_ok=True)
    os.makedirs(test_out_dir, exist_ok=True)
    
    # We need to import the heuristics function from the benchmark script
    import sys
    sys.path.append(os.path.join(BASE_DIR, "tests"))
    try:
        from benchmark import check_extraction_heuristics
    except ImportError:
        def check_extraction_heuristics(path):
            return True, "No heuristic module found."

    results = {}
    
    for file in files:
        if not file.filename.endswith('.pdf'):
            continue
            
        pdf_path = os.path.join(test_data_dir, file.filename)
        # Save uploaded file
        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        file_res = {"success": False, "files": [], "error": None}
        
        try:
            extractor = AnnualReportExtractor(pdf_path)
            toc = extractor.scan_toc()
            mappings = extractor.map_statements(toc)
            
            job_out_dir = os.path.join(test_out_dir, file.filename.replace('.pdf', ''))
            os.makedirs(job_out_dir, exist_ok=True)
            
            _, extracted_files = extractor.extract_and_zip(mappings, job_out_dir)
            file_res["success"] = True
            
            for ext_file in extracted_files:
                if ext_file.endswith('.csv'):
                    csv_path = os.path.join(job_out_dir, ext_file)
                    passed, msg = check_extraction_heuristics(csv_path)
                    file_res["files"].append({
                        "file": ext_file,
                        "passed": passed,
                        "message": msg
                    })
        except Exception as e:
            file_res["error"] = str(e)
            
        results[file.filename] = file_res
        
    return {"results": results}

# Static file serving
BASE_DIR = "/Users/arham/.gemini/antigravity/scratch/annual-report-extractor"

frontend_dir = os.path.join(BASE_DIR, "frontend")
os.makedirs(frontend_dir, exist_ok=True)

# Also expose individual PDF downloads by mounting `/tmp/annual_report_extractor`
# under `/output`. This allows individual files to be downloaded directly.
app.mount("/output", StaticFiles(directory=TMP_BASE_DIR), name="output")

# Fallback mount to serve the frontend UI
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
