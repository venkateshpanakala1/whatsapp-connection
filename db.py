import psycopg2
import psycopg2.pool
import os
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()

_database_url = os.getenv('DATABASE_URL', '')

if _database_url:
    _u = urlparse(_database_url)
    pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,
        host=_u.hostname,
        port=_u.port,
        dbname=_u.path.lstrip('/'),
        user=_u.username,
        password=_u.password,
        sslmode='require',
    )
else:
    pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', 5432),
        dbname=os.getenv('DB_NAME', 'whatsapp_saas'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', 'postgres'),
    )

def get_conn():
    return pool.getconn()

def put_conn(conn):
    pool.putconn(conn)

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS whatsapp_connections (
                id SERIAL PRIMARY KEY,
                phone_number_id VARCHAR(100) NOT NULL,
                waba_id VARCHAR(100) NOT NULL,
                access_token TEXT NOT NULL,
                token_type VARCHAR(20) DEFAULT 'user',
                status VARCHAR(20) DEFAULT 'active',
                verified_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS whatsapp_templates (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                category VARCHAR(50) DEFAULT 'MARKETING',
                language VARCHAR(10) DEFAULT 'en',
                header_type VARCHAR(20) DEFAULT 'NONE',
                body_text TEXT NOT NULL,
                footer_text VARCHAR(200),
                status VARCHAR(20) DEFAULT 'PENDING',
                meta_template_id VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200),
                phone VARCHAR(20) NOT NULL UNIQUE,
                source_file VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS send_logs (
                id SERIAL PRIMARY KEY,
                job_id VARCHAR(100),
                phone VARCHAR(20),
                name VARCHAR(200),
                template_name VARCHAR(100),
                status VARCHAR(20) DEFAULT 'sent',
                sent_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS replies (
                id SERIAL PRIMARY KEY,
                from_phone VARCHAR(20),
                message_body TEXT,
                message_type VARCHAR(20) DEFAULT 'text',
                template_name VARCHAR(100),
                contact_name VARCHAR(200),
                wamid VARCHAR(200),
                received_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS counter_replies (
                id SERIAL PRIMARY KEY,
                phone VARCHAR(20),
                contact_name VARCHAR(200),
                message_text TEXT,
                template_name VARCHAR(100),
                template_lang VARCHAR(10) DEFAULT 'en',
                status VARCHAR(50) DEFAULT 'creating',
                created_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS send_jobs (
                id VARCHAR(64) PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                source_file VARCHAR(255),
                template_name VARCHAR(100),
                status VARCHAR(20) DEFAULT 'pending',
                total INTEGER DEFAULT 0,
                current INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                delay INTEGER DEFAULT 1,
                errors JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS template_media (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                template_name VARCHAR(100) NOT NULL,
                token VARCHAR(40) NOT NULL UNIQUE,
                mime_type VARCHAR(100),
                filename VARCHAR(255),
                data BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, template_name)
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                endpoint TEXT NOT NULL,
                p256dh VARCHAR(255) NOT NULL,
                auth VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(endpoint)
            );

            CREATE TABLE IF NOT EXISTS reply_media (
                id SERIAL PRIMARY KEY,
                reply_id INTEGER REFERENCES replies(id) UNIQUE,
                mime_type VARCHAR(100),
                filename VARCHAR(255),
                data BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        # Defaults existing rows to read so old history doesn't suddenly
        # appear unread; new incoming rows explicitly set FALSE at insert time.
        cur.execute("ALTER TABLE replies ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT TRUE;")
        cur.execute("ALTER TABLE template_media ADD COLUMN IF NOT EXISTS filename VARCHAR(255);")
        # Migrations: add source_file + user_id to all tables
        cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS source_file VARCHAR(255);")
        cur.execute("ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        cur.execute("ALTER TABLE whatsapp_templates    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        cur.execute("ALTER TABLE contacts              ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        cur.execute("ALTER TABLE send_logs             ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        cur.execute("ALTER TABLE replies               ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        cur.execute("ALTER TABLE replies               ADD COLUMN IF NOT EXISTS direction VARCHAR(4) DEFAULT 'in';")
        # Delivery/read tracking for bulk-send analytics — populated from WhatsApp's
        # status callbacks (sent → delivered → read), matched back to a send_logs
        # row by the message id ('wamid') Meta returned when we sent it.
        cur.execute("ALTER TABLE send_logs              ADD COLUMN IF NOT EXISTS wamid VARCHAR(100);")
        cur.execute("ALTER TABLE send_logs              ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP;")
        cur.execute("ALTER TABLE send_logs              ADD COLUMN IF NOT EXISTS read_at TIMESTAMP;")
        # Tenant name, captured when an admin registers a new tenant account.
        cur.execute("ALTER TABLE users                  ADD COLUMN IF NOT EXISTS name VARCHAR(200);")
        cur.execute("ALTER TABLE counter_replies       ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
        # Change contacts UNIQUE(phone) → UNIQUE(user_id, phone) so two users can share same number.
        # (Superseded below by UNIQUE(user_id, phone, source_file) — this old
        # constraint is only ever dropped now, never recreated, since
        # recreating it on every boot would conflict with the newer, looser
        # one once any user legitimately has the same number in 2+ files.)
        cur.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contacts_phone_key') THEN
                    ALTER TABLE contacts DROP CONSTRAINT contacts_phone_key;
                END IF;
            END $$;
        """)
        # Change contacts UNIQUE(user_id, phone) → UNIQUE(user_id, phone, source_file)
        # so the same number can belong to multiple uploaded files independently —
        # previously importing a number into a 2nd file silently stole it away
        # from whichever file it was in before, since only one row per
        # (user_id, phone) could ever exist.
        cur.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contacts_user_phone_unique') THEN
                    ALTER TABLE contacts DROP CONSTRAINT contacts_user_phone_unique;
                END IF;
            END $$;
        """)
        cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contacts_user_phone_file_unique') THEN
                    ALTER TABLE contacts ADD CONSTRAINT contacts_user_phone_file_unique UNIQUE(user_id, phone, source_file);
                END IF;
            END $$;
        """)
        # A bulk-send job's own template language/header/body-variable choices
        # weren't persisted anywhere — only template_name and delay were. That
        # meant a job interrupted by a process restart (deploy, crash) could
        # never be safely resumed, since the exact send parameters were gone.
        # Storing them here is what makes resume_pending_send_jobs() possible.
        cur.execute("ALTER TABLE send_jobs ADD COLUMN IF NOT EXISTS template_lang     VARCHAR(10) DEFAULT 'en';")
        cur.execute("ALTER TABLE send_jobs ADD COLUMN IF NOT EXISTS header_format     VARCHAR(20);")
        cur.execute("ALTER TABLE send_jobs ADD COLUMN IF NOT EXISTS header_media_url  TEXT;")
        cur.execute("ALTER TABLE send_jobs ADD COLUMN IF NOT EXISTS header_filename   VARCHAR(255);")
        cur.execute("ALTER TABLE send_jobs ADD COLUMN IF NOT EXISTS body_params       JSONB DEFAULT '[]'::jsonb;")
        conn.commit()
        cur.close()
        print('DB ready - whatsapp_connections table exists')
    except Exception as e:
        print(f'DB init failed: {e}')
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        put_conn(conn)
