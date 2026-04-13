"""
Smart Billing & Invoice Management API
=======================================
API #1 (mine)   : Invoice CRUD          — /api/invoices/
API #2 (friend) : PDF generation proxy  — /api/generate-pdf/
API #3 (public) : Frankfurter currency  — consumed directly by the frontend JS
API #4 (mine)   : Tax calculator        — /api/tax-calculator/, /api/invoice-summary/, /api/analytics/
"""

import os
import uuid
import json
import httpx
from datetime import datetime
from typing import List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Smart Billing & Invoice Management API",
    description=(
        "Integrates 4 web services:\n\n"
        "- **API #1 (mine)**: Invoice CRUD — `/api/invoices/`\n"
        "- **API #2 (friend)**: PDF generation proxy — `/api/generate-pdf/`\n"
        "- **API #3 (public)**: Frankfurter currency exchange (consumed by frontend JS)\n"
        "- **API #4 (mine)**: Tax calculator & analytics — `/api/tax-calculator/`, "
        "`/api/invoice-summary/`, `/api/analytics/`"
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory datastore (swap for DynamoDB/RDS for production) ──────────────
invoices_db: dict = {}


# ─── Pydantic Models ──────────────────────────────────────────────────────────
class LineItem(BaseModel):
    description: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)


class InvoiceCreate(BaseModel):
    customer_name: str = Field(..., min_length=1)
    customer_email: str
    items: List[LineItem] = Field(..., min_length=1)
    currency: str = "USD"
    notes: Optional[str] = None


class TaxRequest(BaseModel):
    items: List[LineItem] = Field(..., min_length=1)
    tax_rate: float = Field(23.0, ge=0, le=100)  # percentage, 23 = 23% (Irish VAT)


class InvoiceSummaryRequest(BaseModel):
    invoice_id: str
    discount_percentage: float = Field(0.0, ge=0, le=100)


# ─── AWS SQS — scalability layer ─────────────────────────────────────────────
def _send_to_sqs(message: dict) -> None:
    """Fire-and-forget: enqueue invoice event to SQS for async processing."""
    queue_url = os.getenv("SQS_QUEUE_URL", "")
    if not queue_url:
        return  # SQS not configured (local dev / tests) — skip silently
    try:
        sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "eu-north-1"))
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
    except (BotoCoreError, ClientError) as exc:
        print(f"[SQS] Warning: could not enqueue — {exc}")


def _process_invoice(invoice_id: str) -> None:
    """Background task: compute subtotal/tax/total and mark invoice processed."""
    inv = invoices_db.get(invoice_id)
    if not inv:
        return
    subtotal = sum(i["quantity"] * i["unit_price"] for i in inv["items"])
    tax = round(subtotal * 0.23, 2)
    total = round(subtotal + tax, 2)
    inv.update(subtotal=round(subtotal, 2), tax=tax, total=total, status="processed")


# ═══════════════════════════════════════════════════════════════════════════════
# API #1 — Invoice CRUD  (my service)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/invoices/", tags=["API-1: Invoice Management"])
async def create_invoice(payload: InvoiceCreate, bg: BackgroundTasks):
    """Create a new invoice. Queued to SQS for async processing (scalability)."""
    invoice_id = str(uuid.uuid4())[:8].upper()
    record = {
        "invoice_id": invoice_id,
        "customer_name": payload.customer_name,
        "customer_email": payload.customer_email,
        "items": [i.model_dump() for i in payload.items],
        "currency": payload.currency,
        "notes": payload.notes,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    invoices_db[invoice_id] = record

    # Scalability: queue event + async processing
    bg.add_task(_send_to_sqs, {"event": "invoice.created", "data": record})
    bg.add_task(_process_invoice, invoice_id)

    return {"message": "Invoice created successfully", "invoice": record}


@app.get("/api/invoices/", tags=["API-1: Invoice Management"])
async def list_invoices():
    """Return all invoices."""
    return {"total": len(invoices_db), "invoices": list(invoices_db.values())}


@app.get("/api/invoices/{invoice_id}", tags=["API-1: Invoice Management"])
async def get_invoice(invoice_id: str):
    """Return a single invoice by ID."""
    inv = invoices_db.get(invoice_id.upper())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@app.delete("/api/invoices/{invoice_id}", tags=["API-1: Invoice Management"])
async def delete_invoice(invoice_id: str):
    """Delete an invoice by ID."""
    if invoice_id.upper() not in invoices_db:
        raise HTTPException(status_code=404, detail="Invoice not found")
    del invoices_db[invoice_id.upper()]
    return {"message": "Invoice deleted successfully"}


# ═══════════════════════════════════════════════════════════════════════════════
# API #2 — PDF Generation proxy  (friend's service)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/generate-pdf/", tags=["API-2: PDF Generation (Friend)"])
async def generate_pdf(invoice_id: str):
    """Proxy: forwards invoice data to the friend's PDF generation service."""
    inv = invoices_db.get(invoice_id.upper())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    subtotal = sum(i["quantity"] * i["unit_price"] for i in inv["items"])
    total = inv.get("total", subtotal)

    payload = {
        "title": f"Invoice #{inv['invoice_id']}",
        "customer": inv["customer_name"],
        "items": [
            f"{i['description']} x{i['quantity']} @ ${i['unit_price']:.2f}"
            for i in inv["items"]
        ],
        "total_amount": f"${total:.2f} {inv['currency']}",
        "status": inv["status"].capitalize(),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                "http://cat-env.eba-gjsvvvwm.us-east-1.elasticbeanstalk.com/generate_pdf_api/",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-CSRFTOKEN": "AN8JkCNhH9PnB3ZW96wdDU2ZAuV21GRD",
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"PDF service returned HTTP {exc.response.status_code}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"PDF service unreachable: {exc}",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# API #4 — Tax Calculator & Invoice Analytics  (my second service)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/tax-calculator/", tags=["API-4: Tax & Analytics"])
async def calculate_tax(req: TaxRequest):
    """Calculate subtotal, tax amount, and total for a list of line items."""
    subtotal = sum(i.quantity * i.unit_price for i in req.items)
    tax_amount = subtotal * (req.tax_rate / 100)
    return {
        "subtotal": round(subtotal, 2),
        "tax_rate_pct": req.tax_rate,
        "tax_amount": round(tax_amount, 2),
        "total": round(subtotal + tax_amount, 2),
    }


@app.post("/api/invoice-summary/", tags=["API-4: Tax & Analytics"])
async def invoice_summary(req: InvoiceSummaryRequest):
    """Return a detailed financial breakdown for an invoice with optional discount."""
    inv = invoices_db.get(req.invoice_id.upper())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    subtotal = sum(i["quantity"] * i["unit_price"] for i in inv["items"])
    discount = round(subtotal * (req.discount_percentage / 100), 2)
    after_discount = round(subtotal - discount, 2)
    tax = round(after_discount * 0.23, 2)
    total_due = round(after_discount + tax, 2)

    return {
        "invoice_id": inv["invoice_id"],
        "customer_name": inv["customer_name"],
        "subtotal": round(subtotal, 2),
        "discount_pct": req.discount_percentage,
        "discount_amount": discount,
        "taxable_amount": after_discount,
        "tax_23pct": tax,
        "total_due": total_due,
    }


@app.get("/api/analytics/", tags=["API-4: Tax & Analytics"])
async def analytics():
    """Return aggregate stats: invoice count, revenue, status breakdown."""
    all_inv = list(invoices_db.values())
    total_revenue = round(sum(i.get("total", 0) for i in all_inv), 2)
    return {
        "total_invoices": len(all_inv),
        "total_revenue": total_revenue,
        "pending": sum(1 for i in all_inv if i["status"] == "pending"),
        "processed": sum(1 for i in all_inv if i["status"] == "processed"),
    }


# ─── Serve Bootstrap 5 frontend (only when directory exists) ─────────────────
if os.path.isdir("frontend"):
    app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
