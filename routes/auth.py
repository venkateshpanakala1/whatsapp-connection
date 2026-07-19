import os
import re
from functools import wraps
from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_conn, put_conn

auth_bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Single hardcoded super-admin account — not a row in `users`. It exists only
# to reach the tenant-registration screen; it can't use any of the tenant app
# pages itself. Defaults match what was asked for, but can be overridden via
# env vars without a code change.
ADMIN_EMAIL    = os.getenv('ADMIN_EMAIL', 'admin@v3.com').strip().lower()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '123456')


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'error': 'Admin access required'}), 401
        return fn(*args, **kwargs)
    return wrapper


@auth_bp.route('/login', methods=['POST'])
def login():
    body     = request.get_json()
    email    = (body.get('email')    or '').strip().lower()
    password = (body.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        session.clear()
        session.permanent = True
        session['is_admin'] = True
        return jsonify({'success': True, 'admin': True, 'message': 'Welcome, admin'})

    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address', 'field': 'email'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, password_hash, name FROM users WHERE email = %s', (email,))
        row = cur.fetchone()

        if row is None:
            # No auto-signup — a login only succeeds against an existing account.
            return jsonify({'error': 'No account found with this email', 'field': 'email'}), 404

        user_id, pw_hash, name = row
        if check_password_hash(pw_hash, password):
            session.clear()
            session.permanent = True
            session['user_id'] = user_id
            session['email']   = email
            session['name']    = name or ''
            return jsonify({'success': True, 'message': 'Welcome back!'})
        else:
            return jsonify({'error': 'Incorrect password', 'field': 'password'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: cur.close()
        except: pass
        put_conn(conn)


# ── Admin-only: tenant provisioning ─────────────────────────────────────
# The only way a new tenant account gets created — replaces the old
# auto-signup-on-login behavior. Gated entirely behind the hardcoded admin
# login above; a regular tenant session never has `is_admin` set.

@auth_bp.route('/admin/register-tenant', methods=['POST'])
@admin_required
def register_tenant():
    body     = request.get_json()
    name     = (body.get('name')     or '').strip()
    email    = (body.get('email')    or '').strip().lower()
    password = (body.get('password') or '').strip()

    if not name or not email or not password:
        return jsonify({'error': 'Name, email and password are required'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address', 'field': 'email'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters', 'field': 'password'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cur.fetchone():
            return jsonify({'error': 'A tenant with this email already exists', 'field': 'email'}), 409

        pw_hash = generate_password_hash(password)
        cur.execute(
            'INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING id',
            (email, pw_hash, name)
        )
        tenant_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'Tenant "{name}" registered successfully', 'id': tenant_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


@auth_bp.route('/admin/tenants', methods=['GET'])
@admin_required
def list_tenants():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, name, email, created_at FROM users ORDER BY id DESC')
        rows = cur.fetchall()
        cur.close()
        tenants = [
            {
                'id':         r[0],
                'name':       r[1] or '',
                'email':      r[2],
                'created_at': (r[3].isoformat() + 'Z') if r[3] else '',
            }
            for r in rows
        ]
        return jsonify({'success': True, 'tenants': tenants})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# Every table a tenant's data can end up in, in FK-safe order (children
# before parents) — reply_media has no direct user_id, it hangs off
# replies.id, so it has to go first or the replies delete would violate
# that foreign key.
TENANT_DATA_TABLES = [
    ('reply_media', 'reply_id IN (SELECT id FROM replies WHERE user_id = %s)'),
    ('replies', 'user_id = %s'),
    ('whatsapp_connections', 'user_id = %s'),
    ('whatsapp_templates', 'user_id = %s'),
    ('contacts', 'user_id = %s'),
    ('send_logs', 'user_id = %s'),
    ('counter_replies', 'user_id = %s'),
    ('send_jobs', 'user_id = %s'),
    ('template_media', 'user_id = %s'),
    ('push_subscriptions', 'user_id = %s'),
]


@auth_bp.route('/admin/tenants/<int:tenant_id>', methods=['DELETE'])
@admin_required
def delete_tenant(tenant_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT email FROM users WHERE id = %s', (tenant_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Tenant not found'}), 404

        # One transaction — either every table is cleaned up and the tenant
        # row is gone, or (on any failure) nothing is touched at all.
        for table, where in TENANT_DATA_TABLES:
            cur.execute(f'DELETE FROM {table} WHERE {where}', (tenant_id,))
        cur.execute('DELETE FROM users WHERE id = %s', (tenant_id,))

        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'Tenant "{row[0]}" and all their data have been deleted'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


@auth_bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    body         = request.get_json()
    email        = (body.get('email')        or '').strip().lower()
    new_password = (body.get('new_password') or '').strip()

    if not email or not new_password:
        return jsonify({'error': 'Email and new password are required'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address', 'field': 'email'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters', 'field': 'password'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cur.fetchone() is None:
            return jsonify({'error': 'No account found with this email', 'field': 'email'}), 404

        pw_hash = generate_password_hash(new_password)
        cur.execute('UPDATE users SET password_hash = %s WHERE email = %s', (pw_hash, email))
        conn.commit()
        return jsonify({'success': True, 'message': 'Password updated — you can now sign in.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: cur.close()
        except: pass
        put_conn(conn)


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@auth_bp.route('/me', methods=['GET'])
def me():
    if 'user_id' in session:
        return jsonify({
            'logged_in': True,
            'email': session.get('email'),
            'name':  session.get('name') or '',
        })
    return jsonify({'logged_in': False})
