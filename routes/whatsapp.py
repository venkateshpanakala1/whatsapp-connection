from flask import Blueprint, request, jsonify, session
import requests as http
from db import get_conn, put_conn

whatsapp_bp = Blueprint('whatsapp', __name__)

META_API = 'https://graph.facebook.com/v22.0'


def verify_token(access_token, waba_id):
    try:
        res = http.get(
            f'{META_API}/{waba_id}/message_templates',
            params={'limit': 1},
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )
        if res.status_code == 200:
            return {'valid': True}
        error = res.json().get('error', {}).get('message', 'Unknown error')
        return {'valid': False, 'error': error}
    except Exception as e:
        return {'valid': False, 'error': str(e)}


# GET /api/whatsapp/status
@whatsapp_bp.route('/status', methods=['GET'])
def status():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, phone_number_id, waba_id, token_type, status, verified_at, created_at
            FROM whatsapp_connections WHERE user_id = %s ORDER BY id DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return jsonify({'connected': False})

        keys = ['id', 'phone_number_id', 'waba_id', 'token_type', 'status', 'verified_at', 'created_at']
        data = dict(zip(keys, row))
        for k in ['verified_at', 'created_at']:
            if data[k]:
                data[k] = data[k].isoformat()
        return jsonify({'connected': True, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# POST /api/whatsapp/connect
@whatsapp_bp.route('/connect', methods=['POST'])
def connect():
    user_id = session.get('user_id')
    body = request.get_json()
    phone_number_id = (body.get('phone_number_id') or '').strip()
    waba_id         = (body.get('waba_id') or '').strip()
    access_token    = (body.get('access_token') or '').strip()

    if not phone_number_id or not waba_id or not access_token:
        return jsonify({'error': 'phone_number_id, waba_id and access_token are required'}), 400

    check = verify_token(access_token, waba_id)
    if not check['valid']:
        return jsonify({'error': f"Token verification failed: {check['error']}"}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM whatsapp_connections WHERE user_id = %s ORDER BY id DESC LIMIT 1', (user_id,))
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE whatsapp_connections
                SET phone_number_id=%s, waba_id=%s, access_token=%s,
                    token_type='user', status='active', verified_at=NOW(), updated_at=NOW()
                WHERE id=%s
            """, (phone_number_id, waba_id, access_token, existing[0]))
            msg = 'WhatsApp connection updated successfully'
        else:
            cur.execute("""
                INSERT INTO whatsapp_connections
                    (user_id, phone_number_id, waba_id, access_token, token_type, status, verified_at)
                VALUES (%s, %s, %s, %s, 'user', 'active', NOW())
            """, (user_id, phone_number_id, waba_id, access_token))
            msg = 'WhatsApp connected successfully'

        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# POST /api/whatsapp/generate-token
@whatsapp_bp.route('/generate-token', methods=['POST'])
def generate_token():
    body = request.get_json()
    short_lived_token = (body.get('short_lived_token') or '').strip()
    app_id            = (body.get('app_id') or '').strip()
    app_secret        = (body.get('app_secret') or '').strip()

    if not short_lived_token or not app_id or not app_secret:
        return jsonify({'error': 'short_lived_token, app_id and app_secret are required'}), 400

    try:
        res = http.get(f'{META_API}/oauth/access_token', params={
            'grant_type': 'fb_exchange_token',
            'client_id': app_id,
            'client_secret': app_secret,
            'fb_exchange_token': short_lived_token,
        }, timeout=10)

        data = res.json()
        if 'error' in data:
            return jsonify({'error': data['error']['message']}), 400

        token      = data['access_token']
        expires_in = data.get('expires_in', 5184000)
        days       = expires_in // 86400

        return jsonify({
            'success': True,
            'long_lived_token': token,
            'expires_in_days': days,
            'message': f'Long-lived token generated. Valid for ~{days} days.',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# POST /api/whatsapp/disconnect
@whatsapp_bp.route('/disconnect', methods=['POST'])
def disconnect():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_connections SET status='inactive', updated_at=NOW() WHERE user_id=%s",
            (user_id,)
        )
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': 'WhatsApp disconnected'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)
