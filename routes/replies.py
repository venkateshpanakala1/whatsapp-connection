from flask import Blueprint, request, jsonify, Response, session
import requests as http
from db import get_conn, put_conn
from routes.webhook import save_message
import json
import time
import threading

replies_bp = Blueprint('replies', __name__)
META_API   = 'https://graph.facebook.com/v22.0'

_cr_jobs = {}


def get_wa_credentials(user_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token, waba_id, phone_number_id
            FROM whatsapp_connections WHERE status='active' AND user_id=%s
            ORDER BY id DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {'access_token': row[0], 'waba_id': row[1], 'phone_number_id': row[2]}
    finally:
        put_conn(conn)


def update_cr_status(cr_id, status):
    _cr_jobs[str(cr_id)] = status
    try:
        conn = get_conn()
        cur  = conn.cursor()
        if status == 'sent':
            cur.execute('UPDATE counter_replies SET status=%s, sent_at=NOW() WHERE id=%s', (status, cr_id))
        else:
            cur.execute('UPDATE counter_replies SET status=%s WHERE id=%s', (status, cr_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'update_cr_status error: {e}')
    finally:
        try: put_conn(conn)
        except: pass


MAX_WAIT_SECONDS = 24 * 60 * 60  # safety cap so a stuck template can't loop forever


def counter_reply_worker(cr_id, phone, template_name, template_lang, creds, user_id=None, message_text='', contact_name=''):
    update_cr_status(cr_id, 'pending_approval')

    # Keep checking until Meta approves/rejects the template — no early give-up.
    # Poll often at first (approvals are usually fast), then back off to avoid
    # hammering the Graph API during a long wait.
    elapsed = 0
    while elapsed < MAX_WAIT_SECONDS:
        interval = 15 if elapsed < 300 else (60 if elapsed < 3600 else 300)
        time.sleep(interval)
        elapsed += interval
        try:
            res  = http.get(
                f"{META_API}/{creds['waba_id']}/message_templates",
                params={'name': template_name, 'fields': 'name,status'},
                headers={'Authorization': f"Bearer {creds['access_token']}"},
                timeout=10
            )
            templates = [t for t in res.json().get('data', []) if t['name'] == template_name]
            if not templates:
                continue

            status = templates[0].get('status', '')

            if status == 'APPROVED':
                update_cr_status(cr_id, 'approved')
                send_res  = http.post(
                    f"{META_API}/{creds['phone_number_id']}/messages",
                    headers={
                        'Authorization': f"Bearer {creds['access_token']}",
                        'Content-Type':  'application/json'
                    },
                    json={
                        'messaging_product': 'whatsapp',
                        'recipient_type':    'individual',
                        'to':                phone,
                        'type':              'template',
                        'template': {
                            'name':     template_name,
                            'language': {'code': template_lang}
                        }
                    },
                    timeout=10
                )
                send_data = send_res.json()
                if 'error' in send_data:
                    update_cr_status(cr_id, f"send_failed: {send_data['error']['message']}")
                else:
                    update_cr_status(cr_id, 'sent')
                    # Log outgoing reply in the replies table for conversation view
                    wamid = (send_data.get('messages') or [{}])[0].get('id', '')
                    save_message(phone, message_text, 'text', wamid, user_id, direction='out', contact_name=contact_name)
                return

            elif status == 'REJECTED':
                update_cr_status(cr_id, 'rejected')
                return

        except Exception:
            continue

    update_cr_status(cr_id, 'timeout')


def get_active_counter_reply(user_id, phone, message_text=None):
    """Most recent non-terminal counter-reply for this phone (optionally
    matching an exact message_text, to dedupe accidental double-sends)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if message_text is not None:
            cur.execute("""
                SELECT id, template_name, status FROM counter_replies
                WHERE user_id = %s AND phone = %s AND message_text = %s
                  AND status NOT IN ('sent', 'rejected', 'timeout')
                  AND status NOT LIKE 'send_failed%%'
                ORDER BY id DESC LIMIT 1
            """, (user_id, phone, message_text))
        else:
            cur.execute("""
                SELECT id, template_name, status FROM counter_replies
                WHERE user_id = %s AND phone = %s
                  AND status NOT IN ('sent', 'rejected', 'timeout')
                  AND status NOT LIKE 'send_failed%%'
                ORDER BY id DESC LIMIT 1
            """, (user_id, phone))
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        put_conn(conn)


def resume_pending_counter_replies():
    """
    Re-attach background workers for counter-replies that were still waiting
    on template approval when the process last stopped (e.g. a deploy), so
    they keep checking instead of being silently abandoned. Uses an atomic
    UPDATE...RETURNING so if multiple gunicorn workers boot at once, each
    in-flight job is only claimed — and resumed — by exactly one of them.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE counter_replies
            SET status = 'resuming'
            WHERE status NOT IN ('sent', 'rejected', 'timeout', 'resuming')
              AND status NOT LIKE 'send_failed%'
            RETURNING id, phone, contact_name, message_text, template_name, template_lang, user_id
        """)
        rows = cur.fetchall()
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)

    for cr_id, phone, contact_name, message_text, template_name, template_lang, user_id in rows:
        creds = get_wa_credentials(user_id)
        if not creds:
            update_cr_status(cr_id, 'send_failed: WhatsApp disconnected')
            continue
        threading.Thread(
            target=counter_reply_worker,
            args=(cr_id, phone, template_name, template_lang, creds, user_id, message_text, contact_name),
            daemon=True
        ).start()

    if rows:
        print(f'[replies] resumed {len(rows)} in-flight counter-repl{"y" if len(rows) == 1 else "ies"}')


def backfill_reply_contact_names():
    """
    One-time-safe: fills in blank contact_name on existing replies using the
    local Contacts list, for numbers we already have a name for. Doesn't call
    Meta — WhatsApp only exposes a sender's profile name on the webhook event
    itself, so older rows can only be backfilled from data we already have.

    Matches on the last 10 digits rather than exact string equality — some
    imported contacts are missing the country code or have a leading '+',
    while WhatsApp always sends the full number with country code.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE replies r
            SET contact_name = c.name
            FROM contacts c
            WHERE r.user_id = c.user_id
              AND RIGHT(regexp_replace(r.from_phone, '\\D', '', 'g'), 10) = RIGHT(regexp_replace(c.phone, '\\D', '', 'g'), 10)
              AND (r.contact_name IS NULL OR r.contact_name = '')
              AND c.name IS NOT NULL AND c.name != ''
        """)
        updated = cur.rowcount
        conn.commit()
        cur.close()
        if updated:
            print(f'[replies] backfilled contact_name for {updated} replies from local contacts')
    except Exception as e:
        print(f'[replies] backfill_reply_contact_names error: {e}')
    finally:
        put_conn(conn)


# GET /api/replies/conversations
# One row per contact (most-recent activity first), like a WhatsApp chat list.
@replies_bp.route('/conversations', methods=['GET'])
def list_conversations():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (from_phone)
                    from_phone, contact_name, message_body, message_type, direction, received_at
                FROM replies
                WHERE user_id = %s
                ORDER BY from_phone, received_at DESC
            ),
            unread AS (
                SELECT from_phone, COUNT(*) as cnt
                FROM replies
                WHERE user_id = %s AND direction = 'in' AND is_read = FALSE
                GROUP BY from_phone
            )
            SELECT latest.from_phone, latest.contact_name, latest.message_body,
                   latest.message_type, latest.direction, latest.received_at,
                   COALESCE(unread.cnt, 0) as unread_count
            FROM latest
            LEFT JOIN unread ON unread.from_phone = latest.from_phone
            ORDER BY latest.received_at DESC
        """, (user_id, user_id))
        rows = cur.fetchall()
        cur.close()
        return jsonify({
            'success': True,
            'conversations': [
                {
                    'phone':         r[0],
                    'contact_name':  r[1] or '',
                    'message_body':  r[2],
                    'message_type':  r[3],
                    'direction':     r[4],
                    # received_at is a naive TIMESTAMP written by Postgres's NOW(),
                    # which is UTC — mark it explicitly with 'Z' so the browser's
                    # Date parser converts to the viewer's local time instead of
                    # misreading the naive string as if it were already local.
                    'received_at':   (r[5].isoformat() + 'Z') if r[5] else '',
                    'unread_count':  r[6],
                }
                for r in rows
            ]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/replies/conversation/<phone>
# Full chat history with one contact; opening it marks their unread
# incoming messages as read, clearing the unread badge immediately.
@replies_bp.route('/conversation/<phone>', methods=['GET'])
def get_conversation(phone):
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE replies SET is_read = TRUE
            WHERE user_id = %s AND from_phone = %s AND direction = 'in' AND is_read = FALSE
        """, (user_id, phone))
        conn.commit()

        cur.execute("""
            SELECT r.id, r.message_body, r.message_type, r.direction, r.received_at,
                   (rm.reply_id IS NOT NULL) AS has_media
            FROM replies r
            LEFT JOIN reply_media rm ON rm.reply_id = r.id
            WHERE r.user_id = %s AND r.from_phone = %s
            ORDER BY r.received_at ASC
        """, (user_id, phone))
        rows = cur.fetchall()
        cur.close()
        return jsonify({
            'success': True,
            'messages': [
                {
                    'id':           r[0],
                    'message_body': r[1],
                    'message_type': r[2],
                    'direction':    r[3],
                    # See note in list_conversations() — naive TIMESTAMP is UTC,
                    # mark it so the browser converts to local time correctly.
                    'received_at':  (r[4].isoformat() + 'Z') if r[4] else '',
                    'has_media':    r[5],
                }
                for r in rows
            ]
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/replies/media/<reply_id>  — serves the downloaded WhatsApp media
# (image/video/audio/document) for one message. Scoped to the logged-in
# user's own replies so one account can't fetch another's media by guessing ids.
@replies_bp.route('/media/<int:reply_id>', methods=['GET'])
def reply_media(reply_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT rm.data, rm.mime_type, rm.filename
            FROM reply_media rm
            JOIN replies r ON r.id = rm.reply_id
            WHERE rm.reply_id = %s AND r.user_id = %s
        """, (reply_id, user_id))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        data, mime_type, filename = row
        response = Response(bytes(data), mimetype=mime_type or 'application/octet-stream')
        if filename:
            response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# POST /api/replies/counter-reply
@replies_bp.route('/counter-reply', methods=['POST'])
def counter_reply():
    user_id      = session.get('user_id')
    body         = request.get_json()
    phone        = (body.get('phone')        or '').strip()
    contact_name = (body.get('contact_name') or '').strip()
    message_text = (body.get('message_text') or '').strip()

    if not phone or not message_text:
        return jsonify({'error': 'phone and message_text are required'}), 400

    # If this exact reply is already in flight (e.g. double-click, or the
    # page was closed/refreshed mid-send), reattach to it instead of creating
    # a second ad-hoc template for the same message.
    existing = get_active_counter_reply(user_id, phone, message_text)
    if existing:
        return jsonify({'success': True, 'cr_id': existing[0], 'template_name': existing[1], 'resumed': True})

    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    digits        = ''.join(c for c in phone if c.isdigit())[-8:]
    template_name = f"cr_{digits}_{str(int(time.time()))[-6:]}"
    template_lang = 'en'

    try:
        res  = http.post(
            f"{META_API}/{creds['waba_id']}/message_templates",
            headers={
                'Authorization': f"Bearer {creds['access_token']}",
                'Content-Type':  'application/json'
            },
            json={
                'name':       template_name,
                'category':   'UTILITY',
                'language':   template_lang,
                'components': [{'type': 'BODY', 'text': message_text}]
            },
            timeout=30
        )
        data = res.json()
        if 'error' in data:
            return jsonify({'error': data['error']['message']}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO counter_replies (user_id, phone, contact_name, message_text, template_name, template_lang, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'creating')
            RETURNING id
        """, (user_id, phone, contact_name, message_text, template_name, template_lang))
        cr_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)

    _cr_jobs[str(cr_id)] = 'pending_approval'
    threading.Thread(
        target=counter_reply_worker,
        args=(cr_id, phone, template_name, template_lang, creds, user_id, message_text, contact_name),
        daemon=True
    ).start()

    return jsonify({'success': True, 'cr_id': cr_id, 'template_name': template_name})


# GET /api/replies/cr-active?phone=<phone>
# Lets the Replies UI reconnect to an in-progress counter-reply for a contact
# when a reply modal is (re)opened — e.g. after a refresh or closing the tab —
# instead of losing track of a job that's still running server-side.
@replies_bp.route('/cr-active', methods=['GET'])
def cr_active():
    user_id = session.get('user_id')
    phone   = (request.args.get('phone') or '').strip()
    if not user_id or not phone:
        return jsonify({'active': False})
    row = get_active_counter_reply(user_id, phone)
    if not row:
        return jsonify({'active': False})
    return jsonify({'active': True, 'cr_id': row[0], 'template_name': row[1], 'status': row[2]})


# GET /api/replies/cr-status/<cr_id>  — SSE stream
@replies_bp.route('/cr-status/<cr_id>')
def cr_status_stream(cr_id):
    def generate():
        while True:
            # Read from DB so it works across multiple gunicorn workers
            try:
                conn = get_conn()
                cur  = conn.cursor()
                cur.execute('SELECT status FROM counter_replies WHERE id = %s', (cr_id,))
                row = cur.fetchone()
                cur.close()
                put_conn(conn)
                status = row[0] if row else 'pending_approval'
            except Exception:
                status = 'pending_approval'
            yield f"data: {json.dumps({'status': status})}\n\n"
            done = status in ('sent', 'rejected', 'timeout') or status.startswith('send_failed')
            if done:
                break
            time.sleep(2)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )
