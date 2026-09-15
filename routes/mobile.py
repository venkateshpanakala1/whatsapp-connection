"""Authenticated API and FCM delivery for the native Android companion app."""
import hashlib
import json
import os
import secrets
from datetime import timedelta

from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash

from db import get_conn, put_conn

mobile_bp = Blueprint('mobile', __name__)
MOBILE_TOKEN_DAYS = max(1, min(int(os.getenv('MOBILE_TOKEN_DAYS', '30')), 90))


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def mobile_user_id():
    """Return a user for a valid Android bearer token, otherwise None."""
    cached = getattr(g, 'mobile_user_id', None)
    if cached:
        return cached
    header = request.headers.get('Authorization', '')
    scheme, _, token = header.partition(' ')
    if scheme.lower() != 'bearer' or not token or len(token) > 512:
        return None
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE mobile_sessions SET last_used_at=NOW()
                       WHERE token_hash=%s AND revoked_at IS NULL AND expires_at > NOW()
                       RETURNING user_id""", (_hash_token(token),))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row:
            g.mobile_user_id = row[0]
            return row[0]
        return None
    finally:
        put_conn(conn)


def require_mobile_user():
    user_id = mobile_user_id()
    if not user_id:
        return None, (jsonify({'error': 'Valid Android access token required'}), 401)
    return user_id, None


@mobile_bp.route('/auth/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    device_name = (body.get('device_name') or 'Android device').strip()[:120]
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, password_hash, name FROM users WHERE email=%s', (email,))
        row = cur.fetchone()
        if not row or not check_password_hash(row[1], password):
            return jsonify({'error': 'Incorrect email or password'}), 401
        token = secrets.token_urlsafe(48)
        cur.execute("""INSERT INTO mobile_sessions (user_id, token_hash, device_name, expires_at)
                       VALUES (%s,%s,%s,NOW() + (%s * INTERVAL '1 day'))""",
                    (row[0], _hash_token(token), device_name, MOBILE_TOKEN_DAYS))
        conn.commit(); cur.close()
        return jsonify({'access_token': token, 'expires_in_days': MOBILE_TOKEN_DAYS,
                        'user': {'name': row[2] or '', 'email': email}})
    except Exception as exc:
        conn.rollback()
        print(f'[mobile] login failed: {exc}')
        return jsonify({'error': 'Could not sign in'}), 500
    finally:
        put_conn(conn)


@mobile_bp.route('/auth/logout', methods=['POST'])
def logout():
    user_id, error = require_mobile_user()
    if error:
        return error
    token = request.headers['Authorization'].partition(' ')[2]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE mobile_sessions SET revoked_at=NOW() WHERE user_id=%s AND token_hash=%s',
                    (user_id, _hash_token(token)))
        conn.commit(); cur.close()
        return jsonify({'success': True})
    finally:
        put_conn(conn)


@mobile_bp.route('/devices/register', methods=['POST'])
def register_device():
    user_id, error = require_mobile_user()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    fcm_token = (body.get('fcm_token') or '').strip()
    device_name = (body.get('device_name') or 'Android device').strip()[:120]
    if not fcm_token or len(fcm_token) > 4096:
        return jsonify({'error': 'A valid FCM token is required'}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO mobile_devices (user_id, fcm_token, device_name, platform, active, updated_at, last_seen_at)
                       VALUES (%s,%s,%s,'android',TRUE,NOW(),NOW())
                       ON CONFLICT (fcm_token) DO UPDATE SET user_id=EXCLUDED.user_id,
                         device_name=EXCLUDED.device_name, active=TRUE, updated_at=NOW(), last_seen_at=NOW()""",
                    (user_id, fcm_token, device_name))
        conn.commit(); cur.close()
        return jsonify({'success': True})
    finally:
        put_conn(conn)


def _firebase_messaging():
    """Initialise Firebase only on the server when credentials are configured."""
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
        if not firebase_admin._apps:
            raw = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
            if raw:
                firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))
            else:
                credential_file = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
                if not credential_file:
                    return None
                firebase_admin.initialize_app(credentials.Certificate(credential_file))
        return messaging
    except Exception as exc:
        print(f'[mobile] FCM unavailable: {exc}')
        return None


def send_incoming_call_fcm(user_id, call_id, caller_name='', caller_phone=''):
    """Best-effort, minimal-data Android alert. The database remains authoritative."""
    messaging = _firebase_messaging()
    if not messaging:
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, fcm_token FROM mobile_devices WHERE user_id=%s AND active=TRUE', (user_id,))
        devices = cur.fetchall(); cur.close()
    finally:
        put_conn(conn)
    data = {'type': 'incoming_call', 'call_id': str(call_id),
            'caller_name': str(caller_name or ''), 'caller_phone': str(caller_phone or '')}
    for device_id, token in devices:
        try:
            message = messaging.Message(
                data=data, token=token,
                android=messaging.AndroidConfig(priority='high', ttl=timedelta(seconds=55)),
            )
            messaging.send(message)
        except Exception as exc:
            print(f'[mobile] FCM send failed device_id={device_id}: {exc}')
            # Invalid/unregistered tokens should not be retried forever.
            if exc.__class__.__name__ in ('UnregisteredError', 'SenderIdMismatchError'):
                stale = get_conn()
                try:
                    stale_cur = stale.cursor()
                    stale_cur.execute('UPDATE mobile_devices SET active=FALSE, updated_at=NOW() WHERE id=%s', (device_id,))
                    stale.commit(); stale_cur.close()
                finally:
                    put_conn(stale)
