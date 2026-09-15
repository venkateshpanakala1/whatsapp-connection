"""WhatsApp Cloud API Calling signalling for browser-based Shortcut agents.

The Flask service deliberately only relays signalling/control to Meta. Media
flows directly between the browser's WebRTC peer connection and Meta; Railway
is not used as an RTP/SIP server.
"""
import os
import threading
from datetime import datetime
from urllib.parse import quote

import requests as http
from flask import Blueprint, jsonify, request, session

from db import get_conn, put_conn
from routes.push import send_push_to_user

calls_bp = Blueprint('calls', __name__)
META_API = f"https://graph.facebook.com/{os.getenv('WHATSAPP_CALLING_GRAPH_API_VERSION', 'v26.0')}"
CALLING_ENABLED = os.getenv('WHATSAPP_CALLING_ENABLED', '').lower() in ('1', 'true', 'yes', 'on')
# Optional tenant/number fence for deployments that host more than one app.
# Set this to the Shortcut business phone-number ID to ensure calls for any
# other tenant remain untouched.
CALLING_PHONE_NUMBER_ID = os.getenv('WHATSAPP_CALLING_PHONE_NUMBER_ID', '').strip()
TERMINAL_STATUSES = ('COMPLETED', 'FAILED', 'REJECTED', 'TERMINATED')


def get_wa_credentials(user_id):
    already_exists = True
    saved = False
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT access_token, phone_number_id FROM whatsapp_connections
                       WHERE status='active' AND user_id=%s ORDER BY id DESC LIMIT 1""", (user_id,))
        row = cur.fetchone()
        cur.close()
        return {'access_token': row[0], 'phone_number_id': row[1]} if row else None
    finally:
        put_conn(conn)


def save_call_event(user_id, phone_number_id, call, profile_names=None):
    """Persist a calls-webhook event without logging SDP or credentials."""
    call_id = (call.get('id') or '').strip()
    if not call_id or not user_id:
        return
    event = (call.get('event') or '').lower()
    status = (call.get('status') or event or 'incoming').upper()
    # A terminate webhook is sometimes delivered without a separate terminal
    # status. Keep one canonical terminal value so the agent UI immediately
    # stops ringing instead of treating literal "TERMINATE" as an active call.
    if event == 'terminate':
        status = 'TERMINATED'
    caller = (call.get('from') or call.get('caller') or '').strip()
    session_data = call.get('session') or {}
    offer = session_data.get('sdp') if event == 'connect' and session_data.get('sdp_type') == 'offer' else None
    ended = 'NOW()' if event == 'terminate' or status in TERMINAL_STATUSES else 'NULL'
    is_new_incoming_call = event == 'connect' and (call.get('direction') == 'USER_INITIATED')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM whatsapp_calls WHERE call_id = %s', (call_id,))
        already_exists = cur.fetchone() is not None
        cur.execute(f"""
            INSERT INTO whatsapp_calls
              (call_id, user_id, phone_number_id, caller_phone, caller_name, direction, event, status, offer_sdp, started_at, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),{ended})
            ON CONFLICT (call_id) DO UPDATE SET
              caller_phone=COALESCE(EXCLUDED.caller_phone, whatsapp_calls.caller_phone),
              caller_name=COALESCE(EXCLUDED.caller_name, whatsapp_calls.caller_name),
              event=EXCLUDED.event, status=EXCLUDED.status,
              offer_sdp=COALESCE(EXCLUDED.offer_sdp, whatsapp_calls.offer_sdp),
              ended_at=CASE WHEN {ended} IS NOT NULL THEN NOW() ELSE whatsapp_calls.ended_at END,
              updated_at=NOW()
        """, (call_id, user_id, phone_number_id, caller,
              (profile_names or {}).get(caller), call.get('direction') or '', event, status, offer))
        conn.commit()
        cur.close()
        saved = True
    except Exception as e:
        conn.rollback()
        print(f'[calls] save event failed call_id={call_id}: {e}')
    finally:
        put_conn(conn)

    # A service worker can display this notification while the PWA is closed.
    # Do not include SDP or any call-control information in a push payload.
    if saved and is_new_incoming_call and not already_exists:
        caller_name = (profile_names or {}).get(caller) or caller or 'WhatsApp contact'
        threading.Thread(target=send_push_to_user, args=(
            user_id, 'Incoming WhatsApp call', f'{caller_name} is calling',
            f'/replies?call={quote(call_id, safe="")}'
        ), daemon=True).start()


@calls_bp.route('/incoming', methods=['GET'])
def incoming_calls():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401
    if not CALLING_ENABLED:
        return jsonify({'enabled': False, 'calls': []})
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT call_id, caller_phone, caller_name, status, offer_sdp, created_at
                       FROM whatsapp_calls
                       WHERE user_id=%s AND direction='USER_INITIATED'
                         AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED')
                       ORDER BY updated_at DESC LIMIT 5""", (user_id,))
        rows = cur.fetchall()
        cur.close()
        return jsonify({'enabled': True, 'calls': [{'call_id': r[0], 'phone': r[1] or '', 'name': r[2] or '',
            'status': r[3], 'offer_sdp': r[4] or '', 'created_at': r[5].isoformat() + 'Z' if r[5] else ''} for r in rows]})
    finally:
        put_conn(conn)


@calls_bp.route('/<call_id>/action', methods=['POST'])
def call_action(call_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401
    if not CALLING_ENABLED:
        return jsonify({'error': 'WhatsApp Calling is disabled'}), 403
    body = request.get_json(silent=True) or {}
    action = (body.get('action') or '').lower()
    if action not in ('pre_accept', 'accept', 'reject', 'terminate'):
        return jsonify({'error': 'Invalid call action'}), 400
    sdp = body.get('sdp') or ''
    if action in ('pre_accept', 'accept') and (not isinstance(sdp, str) or not sdp.startswith('v=0')):
        return jsonify({'error': 'A WebRTC SDP answer is required to accept this call'}), 400

    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400
    if CALLING_PHONE_NUMBER_ID and creds['phone_number_id'] != CALLING_PHONE_NUMBER_ID:
        return jsonify({'error': 'Calling is not enabled for this WhatsApp number'}), 403
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT phone_number_id FROM whatsapp_calls WHERE call_id=%s AND user_id=%s', (call_id, user_id))
        row = cur.fetchone()
        cur.close()
        if not row or row[0] != creds['phone_number_id']:
            return jsonify({'error': 'Call not found'}), 404
    finally:
        put_conn(conn)

    payload = {'messaging_product': 'whatsapp', 'action': action, 'call_id': call_id}
    if action in ('pre_accept', 'accept'):
        payload['session'] = {'sdp_type': 'answer', 'sdp': sdp}
    try:
        res = http.post(f"{META_API}/{creds['phone_number_id']}/calls", headers={
            'Authorization': f"Bearer {creds['access_token']}", 'Content-Type': 'application/json'}, json=payload, timeout=15)
        data = res.json()
        if res.status_code >= 400 or data.get('error'):
            detail = data.get('error', {}).get('message', 'Meta rejected the call action')
            return jsonify({'error': detail}), 400
    except Exception as e:
        return jsonify({'error': f'Calling API request failed: {e}'}), 502

    conn = get_conn()
    try:
        cur = conn.cursor()
        status = {
            'pre_accept': 'ACCEPTING',
            'accept': 'ACCEPTING',
            'reject': 'REJECTED',
            'terminate': 'TERMINATED',
        }[action]
        cur.execute('UPDATE whatsapp_calls SET status=%s, answer_sdp=%s, updated_at=NOW() WHERE call_id=%s AND user_id=%s',
                    (status, sdp if action in ('pre_accept', 'accept') else None, call_id, user_id))
        conn.commit(); cur.close()
    finally:
        put_conn(conn)
    print(f'[calls] action={action} accepted by Meta call_id={call_id}')
    return jsonify({'success': True, 'data': data})
