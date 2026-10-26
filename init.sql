-- 1. Create Tenants Table (Organizers / Companies)
CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    category VARCHAR(50) NOT NULL CHECK (category IN ('SPORTS', 'AVIATION', 'CONCERT')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Create Events Table (Matches, Flights, Shows)
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    venue_name VARCHAR(150) NOT NULL,
    event_date TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(20) DEFAULT 'PUBLISHED' CHECK (status IN ('DRAFT', 'PUBLISHED', 'COMPLETED')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Create Physical Seats Table
CREATE TABLE IF NOT EXISTS seats (
    id SERIAL PRIMARY KEY,
    event_id INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    section VARCHAR(50) NOT NULL,
    row_label VARCHAR(20) NOT NULL,
    seat_number VARCHAR(20) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Defensive constraint: No two seats can have identical coordinates in the same event
    CONSTRAINT unique_seat_coordinate UNIQUE(event_id, section, row_label, seat_number)
);

-- 4. Create Confirmed Tickets Ledger Table
CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    event_id INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    seat_id INT NOT NULL REFERENCES seats(id) ON DELETE CASCADE,
    customer_name VARCHAR(150) NOT NULL,
    customer_email VARCHAR(150) NOT NULL,
    ticket_token UUID DEFAULT gen_random_uuid() UNIQUE NOT NULL,
    purchased_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Absolute invariant: One seat can only ever have ONE confirmed ticket
    CONSTRAINT unique_seat_ticket UNIQUE(seat_id)
);

-- Indices for high-speed queries under heavy traffic
CREATE INDEX IF NOT EXISTS idx_events_tenant ON events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_seats_event ON seats(event_id);
CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id);

-- SEED DATA: 1 Football Stadium & 1 Airline Flight
INSERT INTO tenants (id, name, slug, category) VALUES
(1, 'Camp Nou Stadium - FC Barcelona', 'barca', 'SPORTS'),
(2, 'SkyHigh Airways', 'skyhigh', 'AVIATION')
ON CONFLICT (id) DO NOTHING;

INSERT INTO events (id, tenant_id, title, venue_name, event_date, status) VALUES
(1, 1, 'El Clasico: FC Barcelona vs Real Madrid', 'Camp Nou Stadium', CURRENT_TIMESTAMP + INTERVAL '7 days', 'PUBLISHED'),
(2, 2, 'Flight SK-702: London to New York', 'Boeing 787-9', CURRENT_TIMESTAMP + INTERVAL '3 days', 'PUBLISHED')
ON CONFLICT (id) DO NOTHING;
