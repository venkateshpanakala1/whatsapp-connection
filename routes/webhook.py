from flask import Blueprint, request, jsonify
from db import get_conn, put_conn
from routes.push import send_push_to_user
import os
import json
import threading
import requests as http

webhook_bp = Blueprint('webhook', __name__)
VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN', 'myverifytoken123')
META_API = 'https://graph.facebook.com/v22.0'

# Message types that carry downloadable media, keyed to where WhatsApp puts
# the media id/mime/filename inside the webhook payload for that type.
MEDIA_TYPES = ('image', 'video', 'audio', 'document', 'sticker')


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

                # Meta includes the sender's own WhatsApp display name here,
                # keyed by wa_id, alongside every incoming message batch —
                # no separate API call needed to get "the name of the person".
                profile_names = {
                    c.get('wa_id'): c.get('profile', {}).get('name')
                    for c in value.get('contacts', [])
                    if c.get('wa_id')
                }

                # ── Incoming messages (customer → business) ──────────────
                for msg in value.get('messages', []):
                    from_phone = msg.get('from', '')
                    wamid      = msg.get('id', '')
                    msg_type   = msg.get('type', 'text')

                    media = msg.get(msg_type) if msg_type in MEDIA_TYPES else None

                    if msg_type == 'text':
                        msg_body = msg.get('text', {}).get('body', '')
                    elif media:
                        caption  = media.get('caption', '')
                        msg_body = caption or f'[{msg_type.capitalize()}]'
                    elif msg_type == 'unsupported':
                        # Meta sends type "unsupported" (instead of image/video/etc.)
                        # for content it won't hand over via the Cloud API at all —
                        # most commonly "View Once" photos/videos, which WhatsApp
                        # blocks from ever reaching any Business API for privacy
                        # reasons. Logging the real reason so it shows up in Railway
                        # logs and isn't just a mystery "[unsupported]" tag.
                        errors = msg.get('errors', [])
                        detail = errors[0].get('title') or errors[0].get('message') if errors else None
                        print(f'[WEBHOOK] unsupported message details: {errors}')
                        msg_body = f'[Unsupported: {detail}]' if detail else '[Unsupported message]'
                    else:
                        msg_body = f'[{msg_type}]'

                    print(f'[WEBHOOK] incoming from={from_phone} body={msg_body!r} user_id={user_id}')
                    reply_id = save_message(
                        from_phone, msg_body, msg_type, wamid, user_id, direction='in',
                        profile_name=profile_names.get(from_phone)
                    )

                    if reply_id and media and media.get('id'):
                        # Fetching + downloading media is a couple of network
                        # round-trips — do it off-thread so the webhook still
                        # responds to Meta immediately.
                        threading.Thread(
                            target=download_and_store_media,
                            args=(reply_id, media.get('id'), user_id, media.get('mime_type'), media.get('filename')),
                            daemon=True
                        ).start()

                # ── Status callbacks for messages we sent (bulk send + counter-
                # replies) — Meta reports each one's lifecycle as sent → delivered
                # → read (or failed) via these, keyed by the message id we got
                # back when we sent it. This is what powers the "delivered"/"read"
                # counts in the Bulk Send analytics — without it we'd only ever
                # know a message left our side, never whether it arrived or was seen.
                for status_event in value.get('statuses', []):
                    update_send_log_status(status_event.get('id', ''), status_event.get('status', ''))

    except Exception as e:
        print(f'[WEBHOOK] error: {e}')
        import traceback; traceback.print_exc()

    return 'OK', 200


def update_send_log_status(wamid, status):
    """Records delivered/read timestamps on the matching send_logs row.
    'sent' is already set when we log the send itself; 'failed' status
    callbacks are ignored here since a failed *send* is already logged as
    such at send time — this only tracks what happens after a successful send."""
    if not wamid or status not in ('delivered', 'read'):
        return
    column = 'delivered_at' if status == 'delivered' else 'read_at'
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE send_logs SET {column} = NOW()
            WHERE wamid = %s AND {column} IS NULL
        """, (wamid,))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'[WEBHOOK] update_send_log_status error: {e}')
    finally:
        put_conn(conn)


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


def resolve_contact_name(cur, user_id, phone, profile_name=None):
    """
    Best available name for a phone number, in priority order:
    1. The contact's own WhatsApp display name (from the webhook payload)
    2. The name you gave them in your imported Contacts list
    3. The name used the last time you bulk-sent to this number

    Matches on the last 10 digits rather than exact string equality —
    imported contacts are sometimes missing the country code, or include a
    leading '+', while WhatsApp's webhook always sends the full number with
    country code and no symbols. Exact matching silently misses those.
    """
    if profile_name:
        return profile_name

    cur.execute("""
        SELECT name FROM contacts
        WHERE user_id = %s
          AND RIGHT(regexp_replace(phone, '\\D', '', 'g'), 10) = RIGHT(regexp_replace(%s, '\\D', '', 'g'), 10)
        ORDER BY updated_at DESC LIMIT 1
    """, (user_id, phone))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]

    cur.execute("""
        SELECT name FROM send_logs
        WHERE status = 'sent' AND user_id = %s
          AND RIGHT(regexp_replace(phone, '\\D', '', 'g'), 10) = RIGHT(regexp_replace(%s, '\\D', '', 'g'), 10)
        ORDER BY sent_at DESC LIMIT 1
    """, (user_id, phone))
    row = cur.fetchone()
    return row[0] if row and row[0] else ''


def save_message(from_phone, message_body, message_type, wamid, user_id, direction='in',
                  profile_name=None, contact_name=None):
    """Save an incoming or outgoing WhatsApp message to the replies table.
    Returns the new row's id, or None if it was skipped/a duplicate."""
    if not user_id:
        print(f'[WEBHOOK] save_message skipped: user_id is None (from={from_phone})')
        return None

    conn = get_conn()
    try:
        cur = conn.cursor()

        # Deduplicate by wamid
        if wamid:
            cur.execute('SELECT id FROM replies WHERE wamid = %s', (wamid,))
            if cur.fetchone():
                cur.close()
                return None

        if direction == 'in':
            cur.execute("""
                SELECT template_name FROM send_logs
                WHERE status = 'sent' AND user_id = %s
                  AND RIGHT(regexp_replace(phone, '\\D', '', 'g'), 10) = RIGHT(regexp_replace(%s, '\\D', '', 'g'), 10)
                ORDER BY sent_at DESC LIMIT 1
            """, (user_id, from_phone))
            row = cur.fetchone()
            template_name = row[0] if row else 'direct'
            contact_name  = contact_name or resolve_contact_name(cur, user_id, from_phone, profile_name)
            is_read = False
        else:
            # Outgoing — caller passes contact_name in explicitly (e.g. counter-reply)
            template_name = 'outgoing'
            contact_name  = contact_name or ''
            is_read = True

        cur.execute("""
            INSERT INTO replies
                (user_id, from_phone, message_body, message_type, template_name, contact_name, wamid, direction, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, from_phone, message_body, message_type, template_name, contact_name, wamid, direction, is_read))
        reply_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        print(f'[WEBHOOK] saved {direction} message from={from_phone}')

        if direction == 'in':
            # Push notification shouldn't delay the webhook's response to Meta
            threading.Thread(
                target=send_push_to_user,
                args=(user_id, contact_name or from_phone, message_body),
                daemon=True
            ).start()

        return reply_id
    except Exception as e:
        conn.rollback()
        print(f'[WEBHOOK] save_message error: {e}')
        import traceback; traceback.print_exc()
        return None
    finally:
        put_conn(conn)


def get_wa_access_token(user_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token FROM whatsapp_connections
            WHERE status = 'active' AND user_id = %s ORDER BY id DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        put_conn(conn)


def download_and_store_media(reply_id, media_id, user_id, fallback_mime=None, filename=None):
    """
    WhatsApp media messages only carry a media id in the webhook payload —
    the actual file has to be fetched separately from Meta (a lookup call for
    a short-lived download URL, then the download itself), both requiring the
    business's access token. Runs in a background thread so the webhook can
    still ack Meta immediately; failures here just mean the message keeps its
    '[Image]'-style placeholder text instead of the real media.
    """
    access_token = get_wa_access_token(user_id)
    if not access_token:
        print(f'[WEBHOOK] media download skipped: no active WhatsApp connection for user_id={user_id}')
        return

    try:
        meta_res = http.get(
            f'{META_API}/{media_id}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15
        )
        meta_data = meta_res.json()
        url = meta_data.get('url')
        mime_type = meta_data.get('mime_type') or fallback_mime
        if not url:
            print(f'[WEBHOOK] media lookup failed for media_id={media_id}: {meta_data}')
            return

        file_res = http.get(url, headers={'Authorization': f'Bearer {access_token}'}, timeout=60)
        if file_res.status_code != 200:
            print(f'[WEBHOOK] media download failed for media_id={media_id}: HTTP {file_res.status_code}')
            return

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO reply_media (reply_id, mime_type, filename, data)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (reply_id) DO NOTHING
            """, (reply_id, mime_type, filename, file_res.content))
            conn.commit()
            cur.close()
            print(f'[WEBHOOK] stored media for reply_id={reply_id} ({mime_type}, {len(file_res.content)} bytes)')
        finally:
            put_conn(conn)
    except Exception as e:
        print(f'[WEBHOOK] media download error for media_id={media_id}: {e}')
