"""
Results API routes.
Handles querying, updating, editing, and exporting discovered/analyzed profiles.
Exports match the old Social Media Tool's exact column format with embedded screenshots.
"""

import io
import os
import base64
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core.config import settings
from backend.core.db import (
    get_results,
    get_result_full,
    update_status,
    update_fields,
    get_keywords_for_client,
    get_known_urls_for_client,
    SUPPORTED_PLATFORMS,
)
from backend.core.logger import get_logger

router = APIRouter(tags=["results"])
logger = get_logger("api.results")


# ─── Request Models ──────────────────────────────────────────────────────────


class UpdateStatusRequest(BaseModel):
    status: str
    platform: str


class UpdateFieldsRequest(BaseModel):
    platform: str
    fields: dict


# ─── Risk Score Calculation (matches old tool's exact 3-9 logic) ─────────────


def calculate_risk_score(doc: dict) -> tuple[int, str]:
    """
    Calculate risk score (3-9) and priority from OSINT fields.
    Exact replica of the old tool's scoring logic.
    """
    has_name = bool(doc.get("has_name_match", False))
    has_logo = bool(doc.get("has_logo", False))
    is_active = bool(doc.get("is_active", False))
    location = str(doc.get("location") or "").strip()
    has_location = bool(location) and location.lower() not in ("nan", "none", "")

    followers = 0
    try:
        followers = int(float(str(doc.get("followers") or 0).replace(",", "")))
    except Exception:
        pass

    priority = "High" if has_logo else "Low"

    # Account age check
    created_months = 999
    created_str = doc.get("created_at") or ""
    if created_str:
        try:
            from dateutil.parser import parse as date_parse

            created_dt = date_parse(str(created_str))
            now = (
                datetime.now(created_dt.tzinfo) if created_dt.tzinfo else datetime.now()
            )
            created_months = (now.year - created_dt.year) * 12 + (
                now.month - created_dt.month
            )
        except Exception:
            pass

    is_new = created_months <= 6
    is_very_new = created_months <= 1

    # Rule 1 (9): Name + Logo + New + Active + Location + Followers > 100
    if (
        has_name
        and has_logo
        and is_new
        and is_active
        and has_location
        and followers > 100
    ):
        return 9, priority

    # Rule 2 (8): Name + Logo + Active + Location + Very New (<1 month)
    if has_name and has_logo and is_active and has_location and is_very_new:
        return 8, priority

    # Rule 3 (7): Name + Logo + Active + Location
    if has_name and has_logo and is_active and has_location:
        return 7, priority

    # Rule 3.5 (7): Name + Logo + (Active or New)
    if has_name and has_logo and (is_active or is_new):
        return 7, priority

    # Rule 4 (6): Name + Logo (inactive/old)
    if has_name and has_logo:
        return 6, priority

    # Rule 5 (4): Name + New (no logo)
    if has_name and is_new:
        return 4, priority

    # Rule 6 (3): Name only
    if has_name:
        return 3, priority

    return 0, priority


# ─── Routes ──────────────────────────────────────────────────────────────────


@router.get("/results/{client}/keywords")
async def list_keywords(
    client: str,
    platform: Optional[str] = Query(None),
):
    """Return unique keywords associated with a client's results."""
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    keywords = await get_keywords_for_client(client, platform)
    return {"client": client, "keywords": keywords}


@router.get("/results/{client}/known-urls")
async def list_known_urls(
    client: str,
    platform: Optional[str] = Query(None),
):
    """Return all known URLs for a client (any status) for frontend dedup."""
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    urls = await get_known_urls_for_client(client, platform)
    return {"client": client, "urls": urls}


@router.get("/results/{client}")
async def query_results(
    client: str,
    platform: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    confidence: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Query results for a client with optional filters."""
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    if status and status not in ("pending", "approved", "rejected"):
        raise HTTPException(
            status_code=400, detail="Status must be pending, approved, or rejected"
        )

    if confidence and confidence not in ("HIGH", "MEDIUM", "LOW"):
        raise HTTPException(
            status_code=400, detail="Confidence must be HIGH, MEDIUM, or LOW"
        )

    results, total_count = await get_results(
        client=client,
        platform=platform,
        status=status,
        keyword=keyword,
        confidence=confidence,
        limit=limit,
        offset=offset,
    )

    return {"client": client, "results": results, "count": total_count}


@router.patch("/results/{doc_id}")
async def change_status(doc_id: str, req: UpdateStatusRequest):
    """Update the status (pending/approved/rejected) of a result."""
    if req.status not in ("pending", "approved", "rejected"):
        raise HTTPException(
            status_code=400, detail="Status must be pending, approved, or rejected"
        )

    if req.platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {req.platform}")

    updated = await update_status(doc_id, req.platform, req.status)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Result {doc_id} not found")

    return {"doc_id": doc_id, "status": req.status}


@router.patch("/results/{doc_id}/fields")
async def edit_fields(doc_id: str, req: UpdateFieldsRequest):
    """
    Update editable OSINT fields on a result and recalculate risk score.
    Used for inline editing of Logo/Active/Name toggles, comments, etc.
    """
    if req.platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {req.platform}")

    # First update the fields
    updated = await update_fields(doc_id, req.platform, req.fields)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Result {doc_id} not found")

    # Recalculate risk score if any scoring fields changed
    scoring_fields = {"has_logo", "is_active", "has_name_match"}
    if scoring_fields & set(req.fields.keys()):
        # Fetch the updated doc to recalculate
        full_doc = await get_result_full(doc_id, req.platform)
        if full_doc:
            risk_score, priority = calculate_risk_score(full_doc)
            await update_fields(
                doc_id,
                req.platform,
                {
                    "risk_score": risk_score,
                    "priority": priority,
                },
            )
            return {
                "doc_id": doc_id,
                "fields": req.fields,
                "risk_score": risk_score,
                "priority": priority,
            }

    return {"doc_id": doc_id, "fields": req.fields}


@router.get("/results/{client}/export")
async def export_results(
    client: str,
    platform: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    """
    Export results as an Excel file matching the old tool's exact format.
    Uses xlsxwriter for embedded screenshots.
    """
    try:
        import pandas as pd
    except ImportError:
        raise HTTPException(status_code=500, detail="pandas not installed")

    results, _ = await get_results(
        client=client,
        platform=platform,
        status=status,
        limit=10000,
        offset=0,
    )

    if not results:
        raise HTTPException(status_code=404, detail="No results found to export")

    # Build rows in old tool's exact column format
    rows = []
    screenshots = []  # keep screenshot data separate

    for r in results:
        row = {
            "Original Name": "",
            "Original feed": "",
            "IMPERSONATED": r.get("url", ""),
            "Profile name": r.get("display_name", r.get("username", "")),
            "Created Date": r.get("created_at", ""),
            "Logo (Yes / No)": "Yes" if r.get("has_logo") else "No",
            "Followers": r.get("followers", 0) or 0,
            "Active (Yes / No)": "Yes" if r.get("is_active") else "No",
            "Name (Yes / No)": "Yes" if r.get("has_name_match") else "No",
            "Location": r.get("location", ""),
            "Last Post (DD-MM-YYYY) (Optional)": r.get(
                "last_post_date", r.get("last_active", "")
            ),
            "Risk Score": r.get("risk_score", 0),
            "priority": r.get("priority", "Low"),
            "Date": datetime.now().strftime("%d-%m-%Y"),
            "Comments": r.get("comments", ""),
            "Platform": r.get("platform", ""),
            "Keyword": r.get("keyword", ""),
            "Username": r.get("username", ""),
            "Bio": r.get("bio", ""),
            "Verified": "Yes" if r.get("is_verified") else "No",
            "Status": r.get("status", "pending"),
        }

        # Handle keywords list
        kws = r.get("keywords", [])
        if isinstance(kws, list) and kws:
            row["Keyword"] = ", ".join(kws)

        rows.append(row)

        # Extract screenshot for embedding
        ss = r.get("screenshot_b64")
        screenshots.append(ss)

    df = pd.DataFrame(rows)

    # Write to Excel with xlsxwriter for screenshot embedding
    buffer = io.BytesIO()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"unified_tool_{client}_{platform or 'all'}_{timestamp}.xlsx"

    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            workbook = writer.book
            worksheet = writer.sheets["Results"]

            # Format headers
            header_format = workbook.add_format(
                {
                    "bold": True,
                    "bg_color": "#1a1a2e",
                    "font_color": "#ffffff",
                    "border": 1,
                    "text_wrap": True,
                    "valign": "vcenter",
                }
            )

            for col_idx, col_name in enumerate(df.columns):
                worksheet.write(0, col_idx, col_name, header_format)

            # Auto-fit column widths
            for col_idx, col_name in enumerate(df.columns):
                max_len = max(
                    len(str(col_name)),
                    df[col_name].astype(str).map(len).max() if len(df) > 0 else 0,
                )
                worksheet.set_column(col_idx, col_idx, min(max_len + 2, 40))

            # Add screenshot column
            ss_col = len(df.columns)
            worksheet.write(0, ss_col, "Screenshot", header_format)
            worksheet.set_column(ss_col, ss_col, 30)

            for idx, ss_b64 in enumerate(screenshots):
                if ss_b64:
                    try:
                        img_data = base64.b64decode(ss_b64)
                        worksheet.set_row(idx + 1, 120)
                        worksheet.insert_image(
                            idx + 1,
                            ss_col,
                            f"screenshot_{idx}.png",
                            {
                                "image_data": io.BytesIO(img_data),
                                "x_scale": 0.25,
                                "y_scale": 0.25,
                                "object_position": 1,
                            },
                        )
                    except Exception:
                        worksheet.write(idx + 1, ss_col, "Image Error")
                else:
                    worksheet.write(idx + 1, ss_col, "No Screenshot")

            # Risk score conditional formatting
            risk_col = (
                list(df.columns).index("Risk Score")
                if "Risk Score" in df.columns
                else None
            )
            if risk_col is not None and len(df) > 0:
                worksheet.conditional_format(
                    1,
                    risk_col,
                    len(df),
                    risk_col,
                    {
                        "type": "3_color_scale",
                        "min_color": "#22c55e",
                        "mid_color": "#f59e0b",
                        "max_color": "#ef4444",
                    },
                )

    except ImportError:
        # Fallback to openpyxl if xlsxwriter not installed
        logger.warning(
            "xlsxwriter not installed, falling back to openpyxl (no screenshots)"
        )
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Results")

    buffer.seek(0)

    logger.info(f"Exporting {len(rows)} results for client '{client}'")

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


class ExportMemoryRequest(BaseModel):
    platform: str
    results: list[dict]


@router.post("/results/{client}/export-memory")
async def export_memory_results(client: str, req: ExportMemoryRequest):
    """
    Export results as an Excel file matching the old tool's format directly from memory.
    This skips the database entirely.
    """
    try:
        import pandas as pd
    except ImportError:
        raise HTTPException(status_code=500, detail="pandas not installed")

    results = req.results
    platform = req.platform

    if not results:
        raise HTTPException(status_code=400, detail="No results provided to export")

    rows = []
    screenshots = []

    for r in results:
        row = {
            "Original Name": "",
            "Original feed": "",
            "IMPERSONATED": r.get("url", ""),
            "Profile name": r.get("display_name", r.get("username", "")),
            "Created Date": r.get("created_at", ""),
            "Logo (Yes / No)": "Yes" if r.get("has_logo") else "No",
            "Followers": r.get("followers", 0) or 0,
            "Active (Yes / No)": "Yes" if r.get("is_active") else "No",
            "Name (Yes / No)": "Yes" if r.get("has_name_match") else "No",
            "Location": r.get("location", ""),
            "Last Post (DD-MM-YYYY) (Optional)": r.get(
                "last_post_date", r.get("last_active", "")
            ),
            "Risk Score": r.get("risk_score", 0),
            "priority": r.get("priority", "Low"),
            "Date": datetime.now().strftime("%d-%m-%Y"),
            "Comments": r.get("comments", ""),
            "Platform": r.get("platform", ""),
            "Keyword": r.get("keyword", ""),
            "Username": r.get("username", ""),
            "Bio": r.get("bio", ""),
            "Verified": "Yes" if r.get("is_verified") else "No",
            "Status": r.get("status", "pending"),
        }

        kws = r.get("keywords", [])
        if isinstance(kws, list) and kws:
            row["Keyword"] = ", ".join(kws)

        rows.append(row)
        screenshots.append(r.get("screenshot_b64"))

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"unified_tool_{client}_{platform or 'all'}_memory_{timestamp}.xlsx"

    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            workbook = writer.book
            worksheet = writer.sheets["Results"]

            header_format = workbook.add_format(
                {
                    "bold": True,
                    "bg_color": "#1a1a2e",
                    "font_color": "#ffffff",
                    "border": 1,
                    "text_wrap": True,
                    "valign": "vcenter",
                }
            )

            for col_idx, col_name in enumerate(df.columns):
                worksheet.write(0, col_idx, col_name, header_format)
                max_len = max(
                    len(str(col_name)),
                    df[col_name].astype(str).map(len).max() if len(df) > 0 else 0,
                )
                worksheet.set_column(col_idx, col_idx, min(max_len + 2, 40))

            ss_col = len(df.columns)
            worksheet.write(0, ss_col, "Screenshot", header_format)
            worksheet.set_column(ss_col, ss_col, 30)

            for idx, ss_b64 in enumerate(screenshots):
                if ss_b64:
                    try:
                        img_data = base64.b64decode(ss_b64)
                        worksheet.set_row(idx + 1, 120)
                        worksheet.insert_image(
                            idx + 1,
                            ss_col,
                            f"screenshot_{idx}.png",
                            {
                                "image_data": io.BytesIO(img_data),
                                "x_scale": 0.25,
                                "y_scale": 0.25,
                                "object_position": 1,
                            },
                        )
                    except Exception:
                        worksheet.write(idx + 1, ss_col, "Image Error")
                else:
                    worksheet.write(idx + 1, ss_col, "No Screenshot")

            risk_col = (
                list(df.columns).index("Risk Score")
                if "Risk Score" in df.columns
                else None
            )
            if risk_col is not None and len(df) > 0:
                worksheet.conditional_format(
                    1,
                    risk_col,
                    len(df),
                    risk_col,
                    {
                        "type": "3_color_scale",
                        "min_color": "#22c55e",
                        "mid_color": "#f59e0b",
                        "max_color": "#ef4444",
                    },
                )
    except ImportError:
        logger.warning(
            "xlsxwriter not installed, falling back to openpyxl (no screenshots)"
        )
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Results")

    buffer.seek(0)
    
    logger.info(f"Exporting {len(rows)} memory results for client '{client}'")

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
