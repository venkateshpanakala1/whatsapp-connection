from flask import Blueprint, request, jsonify
from db import get_conn, put_conn
import os
import json

webhook_bp = Blueprint('webhook', __name__)
VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN', 'myverifytoken123')


# GET /webhook  — Meta challenge verification
@webhook_bp.route('/webhook', methods=['GET'])
def verify():
    mode      = request.args.get('hub.mode')
    token     = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return 'Forbidden', 403


# POST /webhook  — incoming messages + status updates from Meta
@webhook_bp.route('/webhook', methods=['POST'])
def receive():
    data = request.get_json(silent=True) or {}

    # Log every payload so you can debug via Flask console
    print('[WEBHOOK]', json.dumps(data, indent=2)[:2000])

    try:
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})

                # Resolve user from the WA phone number id in this event
                phone_number_id = value.get('metadata', {}).get('phone_number_id', '')
                user_id = lookup_user_by_phone_number_id(phone_number_id)

                if user_id is None:
                    print(f'[WEBHOOK] WARNING: no user found for phone_number_id={phone_number_id!r}')

                # ── Incoming messages (customer → business) ──────────────
                for msg in value.get('messages', []):
                    from_phone = msg.get('from', '')
                    wamid      = msg.get('id', '')
                    msg_type   = msg.get('type', 'text')

                    if msg_type == 'text':
                        msg_body = msg.get('text', {}).get('body', '')
                    elif msg_type == 'image':
                        msg_body = '[Image]'
                    elif msg_type == 'document':
                        msg_body = '[Document]'
                    elif msg_type == 'audio':
                        msg_body = '[Audio]'
                    elif msg_type == 'video':
                        msg_body = '[Video]'
                    else:
                        msg_body = f'[{msg_type}]'

                    print(f'[WEBHOOK] incoming from={from_phone} body={msg_body!r} user_id={user_id}')
                    save_message(from_phone, msg_body, msg_type, wamid, user_id, direction='in')

    except Exception as e:
        print(f'[WEBHOOK] error: {e}')
        import traceback; traceback.print_exc()

    return 'OK', 200


def lookup_user_by_phone_number_id(phone_number_id):
    """
    Find user_id by matching phone_number_id in whatsapp_connections.
    Falls back to the single active user if there is exactly one — handles
    cases where the ID in the DB and the webhook payload differ slightly.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        if phone_number_id:
            cur.execute(
                "SELECT user_id FROM whatsapp_connections "
                "WHERE phone_number_id = %s AND status = 'active' LIMIT 1",
                (phone_number_id,)
            )
            row = cur.fetchone()
            if row and row[0]:
                cur.close()
                return row[0]

        # Fallback: if only one active connection exists, use its user
        cur.execute(
            "SELECT user_id FROM whatsapp_connections "
            "WHERE status = 'active' AND user_id IS NOT NULL"
        )
        rows = cur.fetchall()
        cur.close()
        if len(rows) == 1:
            return rows[0][0]
        return None
    except Exception as e:
        print(f'[WEBHOOK] lookup_user error: {e}')
        return None
    finally:
        put_conn(conn)


def save_message(from_phone, message_body, message_type, wamid, user_id, direction='in'):
    """Save an incoming or outgoing WhatsApp message to the replies table."""
    if not user_id:
        print(f'[WEBHOOK] save_message skipped: user_id is None (from={from_phone})')
        return

    conn = get_conn()
    try:
        cur = conn.cursor()

        # Deduplicate by wamid
        if wamid:
            cur.execute('SELECT id FROM replies WHERE wamid = %s', (wamid,))
            if cur.fetchone():
                cur.close()
                return

        # For incoming messages — find contact name + template from send_logs
        if direction == 'in':
            cur.execute("""
                SELECT template_name, name FROM send_logs
                WHERE phone = %s AND status = 'sent' AND user_id = %s
                ORDER BY sent_at DESC LIMIT 1
            """, (from_phone, user_id))
            row = cur.fetchone()
            template_name = row[0] if row else 'direct'
            contact_name  = row[1] if row else ''
        else:
            # Outgoing — the caller sets these
            template_name = 'outgoing'
            contact_name  = ''

        cur.execute("""
            INSERT INTO replies
                (user_id, from_phone, message_body, message_type, template_name, contact_name, wamid, direction)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, from_phone, message_body, message_type, template_name, contact_name, wamid, direction))
        conn.commit()
        cur.close()
        print(f'[WEBHOOK] saved {direction} message from={from_phone}')
    except Exception as e:
        conn.rollback()
        print(f'[WEBHOOK] save_message error: {e}')
        import traceback; traceback.print_exc()
    finally:
        put_conn(conn)
