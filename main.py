from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg2 import pool
from pydantic import BaseModel
import redis
import os

app = FastAPI(title="OmniTicket Multi-Tenant SaaS")
templates = Jinja2Templates(directory="templates")

# PostgreSQL Connection Pool
db_pool = pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=50,
    dbname=os.getenv("POSTGRES_DB", "omniticket_db"),
    user=os.getenv("POSTGRES_USER", "saas_admin"),
    password=os.getenv("POSTGRES_PASSWORD", "saas_super_secret_password"),
    host=os.getenv("POSTGRES_HOST", "postgres"),
    port=os.getenv("POSTGRES_PORT", "5432")
)

# Redis Connection Pool
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

class ReserveRequest(BaseModel):
    user_name: str
    ttl_seconds: int = 120

class ConfirmRequest(BaseModel):
    customer_name: str
    customer_email: str

# ----------------- WEB PAGES (HTML UI) -----------------

@app.get("/", response_class=HTMLResponse)
def admin_portal(request: Request):
    """The Organizer Dashboard in Chrome."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.title, e.venue_name, t.category,
                       (SELECT COUNT(*) FROM seats s WHERE s.event_id = e.id) as seat_count
                FROM events e
                JOIN tenants t ON e.tenant_id = t.id
                ORDER BY e.id;
            """)
            rows = cur.fetchall()
            events = [
                {"id": r[0], "title": r[1], "venue_name": r[2], "category": r[3], "seat_count": r[4]}
                for r in rows
            ]
            return templates.TemplateResponse(
                request=request, 
                name="admin.html", 
                context={"events": events}
            )
    finally:
        db_pool.putconn(conn)

@app.post("/admin/generate-seats/{event_id}")
def generate_seats_for_event(event_id: int):
    """Admin button to generate 50 physical seats (5 rows x 10 seats)."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seats WHERE event_id = %s;", (event_id,))
            
            rows = ['A', 'B', 'C', 'D', 'E']
            for row in rows:
                price = 150.00 if row in ['A', 'B'] else 80.00
                for num in range(1, 11):
                    cur.execute("""
                        INSERT INTO seats (event_id, section, row_label, seat_number, price)
                        VALUES (%s, 'Main Stand', %s, %s, %s);
                    """, (event_id, row, str(num), price))
            conn.commit()
            return RedirectResponse(url="/", status_code=303)
    finally:
        db_pool.putconn(conn)

@app.get("/event/{event_id}", response_class=HTMLResponse)
def view_event_seat_map(request: Request, event_id: int):
    """The Public Interactive Seat Map."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.title, e.venue_name, t.name 
                FROM events e JOIN tenants t ON e.tenant_id = t.id WHERE e.id = %s;
            """, (event_id,))
            event_row = cur.fetchone()
            if not event_row:
                raise HTTPException(status_code=404, detail="Event not found")
            
            event = {"id": event_id, "title": event_row[0], "venue_name": event_row[1], "tenant_name": event_row[2]}

            cur.execute("""
                SELECT s.id, s.row_label, s.seat_number, s.price, t.id as ticket_id
                FROM seats s
                LEFT JOIN tickets t ON s.id = t.seat_id
                WHERE s.event_id = %s
                ORDER BY s.row_label, CAST(s.seat_number AS INT);
            """, (event_id,))
            seat_rows = cur.fetchall()

            seats = []
            for r in seat_rows:
                seat_id = r[0]
                seat_code = f"{r[1]}{r[2]}"
                price = r[3]
                is_sold = r[4] is not None

                if is_sold:
                    status = "SOLD"
                elif redis_client.get(f"lock:seat:{seat_id}"):
                    status = "RESERVED"
                else:
                    status = "AVAILABLE"

                seats.append({"id": seat_id, "seat_code": seat_code, "price": price, "status": status})

            return templates.TemplateResponse(
                request=request, 
                name="event.html", 
                context={"event": event, "seats": seats}
            )
    finally:
        db_pool.putconn(conn)

@app.get("/ticket/{token}", response_class=HTMLResponse)
def view_digital_ticket(request: Request, token: str):
    """Confirmed Digital Pass."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.title, e.venue_name, t.customer_name, s.row_label || s.seat_number as seat_code, s.price, t.ticket_token
                FROM tickets t
                JOIN seats s ON t.seat_id = s.id
                JOIN events e ON t.event_id = e.id
                WHERE t.ticket_token = %s;
            """, (token,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Ticket token not found")

            ticket = {
                "event_title": row[0],
                "venue_name": row[1],
                "customer_name": row[2],
                "seat_code": row[3],
                "price": row[4],
                "ticket_token": row[5]
            }
            return templates.TemplateResponse(
                request=request, 
                name="ticket.html", 
                context={"ticket": ticket}
            )
    finally:
        db_pool.putconn(conn)

# ----------------- JSON APIS (Redis Locks & Purchasing) -----------------

@app.post("/api/reserve-seat/{seat_id}")
def api_reserve_seat(seat_id: int, req: ReserveRequest):
    """Atomic Distributed Lock in Redis."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tickets WHERE seat_id = %s;", (seat_id,))
            if cur.fetchone():
                return {"status": "failed", "message": "Seat already permanently sold!"}
    finally:
        db_pool.putconn(conn)

    lock_acquired = redis_client.set(
        f"lock:seat:{seat_id}",
        req.user_name,
        nx=True,
        ex=req.ttl_seconds
    )

    if not lock_acquired:
        return {"status": "failed", "message": "Someone else just clicked this seat! Please select another."}

    return {"status": "success", "message": "Seat reserved for 120 seconds", "seat_id": seat_id}

@app.post("/api/confirm-payment/{seat_id}")
def api_confirm_payment(seat_id: int, req: ConfirmRequest):
    """Converts Redis hold into permanent PostgreSQL ticket."""
    lock_key = f"lock:seat:{seat_id}"
    holder = redis_client.get(lock_key)

    if not holder:
        return {"status": "failed", "message": "Your reservation expired! Seat released."}

    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT event_id FROM seats WHERE id = %s;", (seat_id,))
            seat = cur.fetchone()
            if not seat:
                raise HTTPException(status_code=404, detail="Seat not found")
            
            event_id = seat[0]

            cur.execute("""
                INSERT INTO tickets (event_id, seat_id, customer_name, customer_email)
                VALUES (%s, %s, %s, %s)
                RETURNING ticket_token;
            """, (event_id, seat_id, req.customer_name, req.customer_email))
            
            token = cur.fetchone()[0]
            conn.commit()

            redis_client.delete(lock_key)

            return {"status": "success", "ticket_token": str(token)}
    except Exception as e:
        conn.rollback()
        return {"status": "failed", "message": str(e)}
    finally:
        db_pool.putconn(conn)
