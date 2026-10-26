# OmniTicket SaaS — High-Throughput Distributed Ticketing & Reservation Engine

A production-grade, multi-tenant ticketing platform engineered to handle extreme write contention, prevent overselling disasters during flash sales, and enforce seat reservation leases across concurrent client sessions.

Built with **FastAPI**, **PostgreSQL 16**, **Redis 7**, **Docker Compose**, and **Tailwind CSS**, verified against high-concurrency traffic using **k6**.

---

## 1. System Architecture Blueprint

<pre><code>
                                🌐 CLIENTS / BROWSERS
                    (Normal Browser Session & Incognito Session)
                                       │
                                       ▼ HTTP (Port 80)
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          FASTAPI APPLICATION LAYER                              │
│                                                                                 │
│   ┌───────────────────────────────┐     ┌───────────────────────────────────┐   │
│   │   Multi-Tenant Admin Portal   │     │   Public Interactive Seat Map     │   │
│   │   (Stadium / Airline Config)  │     │   (Color-coded state transitions) │   │
│   └───────────────┬───────────────┘     └─────────────────┬─────────────────┘   │
└───────────────────┼───────────────────────────────────────┼─────────────────────┘
                    │                                       │
        ┌───────────┴──────────┐                 ┌──────────┴──────────┐
        │                      │                 │                     │
        ▼                      ▼                 ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  Multi-Tenant    │  │  ACID Ledger &   │  │  In-Memory Lock  │  │  Auto-Expiring   │
│  Schema Queries  │  │  Ticket Creation │  │  Engine (SET NX) │  │  Lease (TTL EX)  │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                     │                     │                     │
         ▼                     ▼                     ▼                     ▼
┌────────────────────────────────────────┐  ┌────────────────────────────────────────┐
│         POSTGRESQL 16 (DISK)           │  │             REDIS 7 (RAM)              │
│  - tenants (Organizers)                │  │  - lock:seat:{id} -> {user_token}      │
│  - events (Matches, Flights, Shows)    │  │  - TTL: 120-second sliding countdown   │
│  - seats (Unique Coordinate System)    │  │  - Sub-millisecond mutual exclusion    │
│  - tickets (Permanent Audit Ledger)    │  │  - Zero DB load during cart holds      │
│  - Invariants: UNIQUE(seat_id)         │  │                                        │
└────────────────────────────────────────┘  └────────────────────────────────────────┘
</code></pre>

---

## 2. The Core Problem: Concurrency & The Flash-Sale Disaster

When multiple users attempt to purchase the same inventory unit simultaneously, standard application code suffers from the **Read-Modify-Write race condition**:

1. **READ:** Server reads inventory (`available_tickets = 100`).
2. **MODIFY:** Server calculates new balance (`100 - 1 = 99`).
3. **WRITE:** Server commits updated balance (`available_tickets = 99`).

If 50 requests arrive within the same 5-millisecond database commit window, all 50 threads read `100`, all 50 calculate `99`, and all 50 write `99`. 

### Empirical Failure Proof (k6 Load Test)
During load testing with **50 concurrent virtual users** executing **500 total buy attempts** against an unprotected endpoint with 100 available tickets:

| Metric | Unprotected (Naive) | Protected (Optimistic / Pessimistic / Redis) |
| :--- | :--- | :--- |
| **Initial Inventory** | 100 seats | 100 seats |
| **Simulated Concurrent Users** | 50 Virtual Users | 50 Virtual Users |
| **Total Purchase Attempts** | 500 requests | 500 requests |
| **Tickets Sold in DB Ledger** | **259 tickets** | **Exactly 100 tickets** |
| **Oversell Disaster** | **YES (+159 non-existent seats sold)** | **NO (0 oversold)** |

---

## 3. Four Concurrency Patterns Analyzed & Tested

### Pattern 1: Unprotected Naive Updates (The Anti-Pattern)
- **Mechanism:** Disconnected `SELECT` followed by `UPDATE`.
- **Result:** Severe data corruption and overselling under load.
- **Verdict:** Unacceptable in any production system.

### Pattern 2: Pessimistic Locking (`SELECT ... FOR UPDATE`)
- **Mechanism:** Acquires an exclusive database row lock at query time. Forces all other competing transactions into a FIFO serialization queue.
- **Result:** Exactly 100 tickets sold. Zero oversells.
- **Trade-off:** High lock contention, database connection pool exhaustion under scale, and high risk of freezing the database if held across third-party operations (e.g., payment gateways).

### Pattern 3: Optimistic Concurrency Control (Version / CAS)
- **Mechanism:** No database row locks. Reads current state and a `version` number. Commits only if `version` has not changed:
<pre><code>UPDATE events 
SET available_tickets = available_tickets - 1, version = version + 1 
WHERE id = :id AND version = :current_version;</code></pre>
- **Result:** Exactly 100 tickets sold. Version counter climbed strictly from `0` to `100`. 
- **Trade-off:** Conflicting transactions must retry, consuming application CPU cycles during intense contention.

### Pattern 4: Distributed Locks with Auto-Expiring Leases (Redis TTL)
- **Mechanism:** The Two-Phase Reservation pattern.
  1. **Phase 1 (In-Memory Hold):** Fast atomic lock via Redis `SET lock:seat:{id} {user} NX EX 120`. Prevents competing buyers from selecting the seat for 2 minutes.
  2. **Phase 2 (Payment Confirmation):** Validates active Redis hold, writes permanent ticket to PostgreSQL, and deletes the Redis key.
  3. **Auto-Release:** If the buyer abandons the cart, Redis purges the key at `00:00`, instantly making the seat available to other fans without executing database cleanup cron jobs.

---
## 4. Multi-Tenant Relational Data Model

Designed with **Pooled Multi-Tenancy** (shared database instance with logical tenant isolation) and database-level defensive invariants:

<pre><code>
┌──────────────────┐        1:N        ┌──────────────────┐
│     tenants      ├──────────────────►│      events      │
├──────────────────┤                   ├──────────────────┤
│ id (PK)          │                   │ id (PK)          │
│ name             │                   │ tenant_id (FK)   │
│ slug (UNIQUE)    │                   │ title            │
│ category         │                   │ venue_name       │
│ created_at       │                   │ event_date       │
└──────────────────┘                   └────────┬─────────┘
                                                │
                                            1:N │
                                                ▼
┌──────────────────┐        1:1        ┌──────────────────┐
│     tickets      │◄──────────────────┤      seats       │
├──────────────────┤                   ├──────────────────┤
│ id (PK)          │                   │ id (PK)          │
│ event_id (FK)    │                   │ event_id (FK)    │
│ seat_id (FK,UNQ) ├─┐                 │ section          │
│ customer_name    │ │                 │ row_label        │
│ customer_email   │ │                 │ seat_number      │
│ ticket_token     │ │                 │ price            │
│ purchased_at     │ │                 │ CONSTRAINT:      │
└──────────────────┘ │                 │ UNIQUE(event_id, │
                     │                 │  section, row, #)│
                     │                 └──────────────────┘
                     │                          ▲
                     └──────────────────────────┘
                      Hard Invariant: UNIQUE(seat_id)
</code></pre>

### Defensive Database Invariants
1. **Coordinate Duplicate Prevention:**
   `CONSTRAINT unique_seat_coordinate UNIQUE(event_id, section, row_label, seat_number)`
   Prevents an organizer from accidentally generating duplicate seat codes for the same venue.
2. **Double-Sale Hardware Guard:**
   `CONSTRAINT unique_seat_ticket UNIQUE(seat_id)`
   Guarantees at the storage engine level that two confirmed tickets can never reference the same physical seat.

---

## 5. Seat Lifecycle State Machine

<pre><code>
      [ AVAILABLE ]  (Green: Open to all buyers)
            │
            │ User clicks seat (API calls Redis SET NX EX 120)
            ▼
      [ RESERVED ]   (Yellow: 2-minute countdown timer active)
            │
            ├────────────────────────────────────────┐
            │ Buyer abandons cart / TTL reaches 0:00 │ Buyer completes checkout
            │ (Redis automatically purges key)       │ (PostgreSQL atomic INSERT)
            ▼                                        ▼
      [ AVAILABLE ]                            [ SOLD ] (Dark Red: Confirmed pass issued)
</code></pre>

---

## 6. Real-World Live Verification

The application was deployed to a remote Google Cloud Platform Linux Virtual Machine and verified across concurrent client sessions:

1. **Mutual Exclusion Proof:**
   - **Tab 1 (Standard Session):** User selects `Seat A1`. Redis grants lock; UI enters 120-second active hold countdown.
   - **Tab 2 (Incognito Session / Distinct Client):** Attempting to click `Seat A1` returns HTTP 200 with an explicit conflict message: *"Seat is currently held by someone else!"*. The button is disabled with amber styling.
2. **Permanent Ledger Handoff:**
   - User completes payment in Tab 1.
   - Ticket is issued with a cryptographic UUID token (e.g., `9e8b2ff9-33ff-4612-9efd-3a2e1b767359`).
   - Refreshing Tab 2 displays `Seat A1` permanently marked as **Confirmed Sold** (Dark Red).

---

## 7. Project Structure

<pre><code>
omniticket-saas/
├── docker-compose.yml       # Multi-container orchestration (Web, DB, Redis)
├── Dockerfile               # Production container recipe for FastAPI service
├── init.sql                 # Multi-tenant DDL migrations, indices, and seeds
├── main.py                  # FastAPI application (SSR web routes + JSON APIs)
├── test_naive.js            # k6 load script proving the race condition
├── test_pessimistic.js      # k6 load script verifying SELECT FOR UPDATE
├── test_optimistic.js       # k6 load script verifying Version CAS
└── templates/
    ├── admin.html           # Multi-tenant organizer console & seat generator
    ├── event.html           # Interactive SVG/CSS grid seat map with live timers
    └── ticket.html          # Digital confirmed pass with verification token
</code></pre>

---

## 8. Quickstart & Deployment

### Prerequisites
- Docker Engine 24+ & Docker Compose v2+
- Port 80 and 8000 accessible on host/firewall

### 1. Launch the Cluster
<pre><code>git clone https://github.com/YOUR_USERNAME/omniticket-saas.git
cd omniticket-saas
docker compose up -d --build
</code></pre>

### 2. Access the Application
Open your browser and navigate to:
- **Organizer Dashboard:** `http://localhost/` (or `http://YOUR_VM_PUBLIC_IP/`)
- **Public Event Seat Map:** `http://localhost/event/1`

### 3. Run High-Concurrency Load Tests
Execute simulated traffic spikes against the cluster using k6:
<pre><code># Test the naive endpoint to witness the oversell failure:
docker run --rm --network host -i grafana/k6 run - < test_naive.js

# Test pessimistic locking under identical load:
docker run --rm --network host -i grafana/k6 run - < test_pessimistic.js
</code></pre>

---

## 9. Senior Engineering Decision Matrix

| Business Scenario | Recommended Pattern | Architectural Justification |
| :--- | :--- | :--- |
| **Financial Balance Deductions** | **Pessimistic Locking** (`SELECT FOR UPDATE`) | Requires strict disk ACID durability. Lock held for only 2–5ms. |
| **Collaborative Document Editing** | **Optimistic Locking** (`version = version + 1`) | Prevents blocking readers while humans type. Conflicts resolved gracefully. |
| **Temporary Cart Reservations (5–10 min)** | **Distributed Locks with TTL** (Redis `SET NX EX`) | Keeps database connection pool unburdened. Auto-expires abandoned carts. |
| **Cluster Job Execution (Cron)** | **Distributed Lock** (Redis) | Guarantees that out of N microservice instances, only one executes the job. |

---

## License
MIT License. Open-source educational reference for distributed systems engineering.
