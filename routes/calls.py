"""WhatsApp Cloud API Calling signalling for browser-based Shortcut agents.

The Flask service deliberately only relays signalling/control to Meta. Media
flows directly between the browser's WebRTC peer connection and Meta; Railway
is not used as an RTP/SIP server.
"""
import os
import threading
import re
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
# Meta's caller controls the actual ringing window. This is only a local UI
# safety net so a missed terminal webhook cannot leave an agent ringing.
CALL_EXPIRY_SECONDS = max(15, min(int(os.getenv('WHATSAPP_CALL_EXPIRY_SECONDS', '60')), 300))


def get_wa_credentials(user_id):
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
    direction = (call.get('direction') or '').upper()
    # For outbound calls the customer is the callee, not the business number.
    caller = (call.get('to') if direction == 'BUSINESS_INITIATED' else (call.get('from') or call.get('caller') or '')).strip()
    session_data = call.get('session') or {}
    offer = session_data.get('sdp') if event == 'connect' and session_data.get('sdp_type') == 'offer' else None
    answer = session_data.get('sdp') if event == 'connect' and session_data.get('sdp_type') == 'answer' else None
    ended = 'NOW()' if event == 'terminate' or status in TERMINAL_STATUSES else 'NULL'
    is_new_incoming_call = event == 'connect' and (call.get('direction') == 'USER_INITIATED')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM whatsapp_calls WHERE call_id = %s', (call_id,))
        already_exists = cur.fetchone() is not None
        cur.execute(f"""
            INSERT INTO whatsapp_calls
              (call_id, user_id, phone_number_id, caller_phone, caller_name, direction, event, status, offer_sdp, answer_sdp, started_at, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),{ended})
            ON CONFLICT (call_id) DO UPDATE SET
              caller_phone=COALESCE(EXCLUDED.caller_phone, whatsapp_calls.caller_phone),
              caller_name=COALESCE(EXCLUDED.caller_name, whatsapp_calls.caller_name),
              event=EXCLUDED.event,
              status=CASE
                -- Do not let a late non-terminal webhook undo the browser
                -- agent's atomic claim while it is preparing WebRTC.
                WHEN whatsapp_calls.status IN ('CLAIMED','ACCEPTING')
                  AND EXCLUDED.status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED')
                  THEN whatsapp_calls.status
                ELSE EXCLUDED.status
              END,
              offer_sdp=COALESCE(EXCLUDED.offer_sdp, whatsapp_calls.offer_sdp),
              answer_sdp=COALESCE(EXCLUDED.answer_sdp, whatsapp_calls.answer_sdp),
              ended_at=CASE WHEN {ended} IS NOT NULL THEN NOW() ELSE whatsapp_calls.ended_at END,
              updated_at=NOW()
        """, (call_id, user_id, phone_number_id, caller,
              (profile_names or {}).get(caller), direction, event, status, offer, answer))
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
            f'/replies?call={quote(call_id, safe="")}', 'incoming-call', call_id,
        ), daemon=True).start()


def agent_id_from_request():
    """A stable random id held by one Replies browser profile/tab family."""
    agent_id = (request.headers.get('X-Call-Agent-ID') or '').strip()
    return agent_id[:80] if agent_id else ''


def expire_stale_calls(cur, user_id):
    cur.execute("""UPDATE whatsapp_calls
                   SET status='EXPIRED', ended_at=NOW(), updated_at=NOW()
                   WHERE user_id=%s
                     AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED','EXPIRED','ACCEPTING','ACCEPTED')
                     AND created_at < NOW() - (%s * INTERVAL '1 second')""",
                (user_id, CALL_EXPIRY_SECONDS))


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
        expire_stale_calls(cur, user_id)
        cur.execute("""SELECT call_id, caller_phone, caller_name, status, offer_sdp, created_at
                       FROM whatsapp_calls
                       WHERE user_id=%s AND direction='USER_INITIATED'
                         AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED','EXPIRED')
                         AND (claimed_by IS NULL OR claimed_by=%s)
                       ORDER BY updated_at DESC LIMIT 5""", (user_id, agent_id_from_request()))
        rows = cur.fetchall()
        cur.close()
        response = {'enabled': True, 'calls': [{'call_id': r[0], 'phone': r[1] or '', 'name': r[2] or '',
            'status': r[3], 'offer_sdp': r[4] or '', 'created_at': r[5].isoformat() + 'Z' if r[5] else ''} for r in rows]}
        conn.commit()
        return jsonify(response)
    finally:
        put_conn(conn)


def can_start_call(permission):
    actions = permission.get('actions') or []
    return permission.get('status') == 'granted' and any(
        action.get('action_name') == 'start_call' and action.get('can_perform_action')
        for action in actions
    )


@calls_bp.route('/outgoing', methods=['GET', 'POST'])
def outgoing_call():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401
    if not CALLING_ENABLED:
        return jsonify({'error': 'WhatsApp Calling is disabled'}), 403
    agent_id = agent_id_from_request()
    if not agent_id:
        return jsonify({'error': 'Refresh Replies and try again.'}), 400
    creds = get_wa_credentials(user_id)
    if not creds or (CALLING_PHONE_NUMBER_ID and creds['phone_number_id'] != CALLING_PHONE_NUMBER_ID):
        return jsonify({'error': 'Calling is not enabled for this WhatsApp number'}), 403

    if request.method == 'GET':
        conn = get_conn()
        try:
            cur = conn.cursor(); expire_stale_calls(cur, user_id)
            cur.execute("""SELECT call_id, caller_phone, status, answer_sdp
                           FROM whatsapp_calls WHERE user_id=%s AND direction='BUSINESS_INITIATED'
                             AND claimed_by=%s AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED','EXPIRED')
                           ORDER BY updated_at DESC LIMIT 1""", (user_id, agent_id))
            row = cur.fetchone(); conn.commit(); cur.close()
            return jsonify({'call': {'call_id': row[0], 'phone': row[1], 'status': row[2], 'answer_sdp': row[3] or ''} if row else None})
        finally:
            put_conn(conn)

    body = request.get_json(silent=True) or {}
    phone = re.sub(r'\D', '', str(body.get('phone') or ''))
    sdp = body.get('sdp') or ''
    if not re.fullmatch(r'\d{7,15}', phone) or not isinstance(sdp, str) or not sdp.startswith('v=0'):
        return jsonify({'error': 'Invalid customer phone number or WebRTC offer'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        expire_stale_calls(cur, user_id)
        cur.execute('SELECT 1 FROM replies WHERE user_id=%s AND from_phone=%s LIMIT 1', (user_id, phone))
        known_customer = cur.fetchone()
        cur.execute("""SELECT 1 FROM whatsapp_calls WHERE user_id=%s
                       AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED','EXPIRED') LIMIT 1""", (user_id,))
        active_call = cur.fetchone(); conn.commit(); cur.close()
        if not known_customer:
            return jsonify({'error': 'This customer is not available in your conversations.'}), 404
        if active_call:
            return jsonify({'error': 'Another WhatsApp call is already active.'}), 409
    finally:
        put_conn(conn)

    try:
        permission_res = http.get(f"{META_API}/{creds['phone_number_id']}/call_permissions",
                                  params={'user_wa_id': phone}, headers={'Authorization': f"Bearer {creds['access_token']}"}, timeout=15)
        permission_data = permission_res.json()
        permission = permission_data.get('permission') or {}
        if permission_res.status_code >= 400 or not can_start_call(permission):
            return jsonify({'error': 'This customer is not currently eligible for WhatsApp calling.', 'eligible': False}), 409
        res = http.post(f"{META_API}/{creds['phone_number_id']}/calls", headers={
            'Authorization': f"Bearer {creds['access_token']}", 'Content-Type': 'application/json'}, json={
            'messaging_product': 'whatsapp', 'to': phone, 'action': 'connect',
            'session': {'sdp_type': 'offer', 'sdp': sdp},
            'biz_opaque_callback_data': f'agent:{agent_id}',
        }, timeout=15)
        data = res.json()
        call_id = ((data.get('calls') or [{}])[0].get('id') or '').strip()
        if res.status_code >= 400 or data.get('error') or not call_id:
            return jsonify({'error': data.get('error', {}).get('message', 'Meta could not start this call')}), 400
    except Exception as e:
        return jsonify({'error': f'Calling API request failed: {e}'}), 502

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO whatsapp_calls (call_id,user_id,phone_number_id,caller_phone,direction,event,status,offer_sdp,claimed_by,claimed_at,started_at)
                       VALUES (%s,%s,%s,%s,'BUSINESS_INITIATED','connect','CALLING',%s,%s,NOW(),NOW())""",
                    (call_id, user_id, creds['phone_number_id'], phone, sdp, agent_id))
        conn.commit(); cur.close()
    finally:
        put_conn(conn)
    return jsonify({'success': True, 'call_id': call_id, 'status': 'CALLING'})


@calls_bp.route('/<call_id>/action', methods=['POST'])
def call_action(call_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401
    if not CALLING_ENABLED:
        return jsonify({'error': 'WhatsApp Calling is disabled'}), 403
    body = request.get_json(silent=True) or {}
    action = (body.get('action') or '').lower()
    if action not in ('claim', 'release', 'pre_accept', 'accept', 'reject', 'terminate'):
        return jsonify({'error': 'Invalid call action'}), 400
    sdp = body.get('sdp') or ''
    if action in ('pre_accept', 'accept') and (not isinstance(sdp, str) or not sdp.startswith('v=0')):
        return jsonify({'error': 'A WebRTC SDP answer is required to accept this call'}), 400
    agent_id = agent_id_from_request()
    if not agent_id:
        return jsonify({'error': 'This browser cannot identify its call session. Refresh Replies and try again.'}), 400

    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400
    if CALLING_PHONE_NUMBER_ID and creds['phone_number_id'] != CALLING_PHONE_NUMBER_ID:
        return jsonify({'error': 'Calling is not enabled for this WhatsApp number'}), 403
    conn = get_conn()
    try:
        cur = conn.cursor()
        expire_stale_calls(cur, user_id)
        # Claim before microphone/WebRTC work. PostgreSQL's row lock makes
        # exactly one agent win if two Accept buttons are pressed together.
        if action == 'claim':
            cur.execute("""UPDATE whatsapp_calls
                           SET claimed_by=%s, claimed_at=NOW(), status='CLAIMED', updated_at=NOW()
                           WHERE call_id=%s AND user_id=%s AND phone_number_id=%s
                             AND claimed_by IS NULL
                             AND status NOT IN ('COMPLETED','FAILED','REJECTED','TERMINATED','EXPIRED')
                           RETURNING call_id""", (agent_id, call_id, user_id, creds['phone_number_id']))
            claimed = cur.fetchone()
            conn.commit()
            cur.close()
            if not claimed:
                return jsonify({'error': 'Another agent already handled this call, or it has expired.'}), 409
            return jsonify({'success': True, 'claimed': True})

        cur.execute('SELECT phone_number_id, claimed_by FROM whatsapp_calls WHERE call_id=%s AND user_id=%s FOR UPDATE', (call_id, user_id))
        row = cur.fetchone()
        if not row or row[0] != creds['phone_number_id']:
            conn.commit(); cur.close()
            return jsonify({'error': 'Call not found'}), 404
        if action in ('pre_accept', 'accept', 'terminate') and row[1] != agent_id:
            conn.commit(); cur.close()
            return jsonify({'error': 'This call is being handled by another agent.'}), 409
        if action == 'reject' and row[1] not in (None, agent_id):
            conn.commit(); cur.close()
            return jsonify({'error': 'This call is being handled by another agent.'}), 409
        if action == 'reject' and row[1] is None:
            # Reject participates in the same lock/claim protocol as Accept,
            # so it cannot race an agent that is beginning to answer.
            cur.execute("""UPDATE whatsapp_calls SET claimed_by=%s, claimed_at=NOW(), status='CLAIMED', updated_at=NOW()
                           WHERE call_id=%s AND user_id=%s""", (agent_id, call_id, user_id))
        cur.close()
        conn.commit()
    finally:
        put_conn(conn)

    if action == 'release':
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE whatsapp_calls SET claimed_by=NULL, claimed_at=NULL, status='CONNECT', updated_at=NOW()
                           WHERE call_id=%s AND user_id=%s AND claimed_by=%s AND status='CLAIMED'""",
                        (call_id, user_id, agent_id))
            conn.commit(); cur.close()
        finally:
            put_conn(conn)
        return jsonify({'success': True})

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
