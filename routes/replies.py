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


def counter_reply_worker(cr_id, phone, template_name, template_lang, creds, user_id=None, message_text=''):
    update_cr_status(cr_id, 'pending_approval')

    for _ in range(60):
        time.sleep(15)
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
                    save_message(phone, message_text, 'text', wamid, user_id, direction='out')
                return

            elif status == 'REJECTED':
                update_cr_status(cr_id, 'rejected')
                return

        except Exception:
            continue

    update_cr_status(cr_id, 'timeout')


# GET /api/replies/templates
@replies_bp.route('/templates', methods=['GET'])
def list_templates():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT template_name, COUNT(*) as sent_count, MAX(sent_at) as last_sent
            FROM send_logs WHERE status = 'sent' AND user_id = %s
              AND template_name NOT LIKE 'cr\\_%%' ESCAPE '\\'
            GROUP BY template_name
            ORDER BY MAX(sent_at) DESC
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        return jsonify({
            'success': True,
            'templates': [
                {'name': r[0], 'sent_count': r[1], 'last_sent': r[2].isoformat() if r[2] else ''}
                for r in rows
            ]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/replies/list?template=xxx
@replies_bp.route('/list', methods=['GET'])
def list_replies():
    user_id  = session.get('user_id')
    template = request.args.get('template', '').strip()
    conn = get_conn()
    try:
        cur = conn.cursor()
        if template and template != 'all':
            cur.execute("""
                SELECT id, from_phone, contact_name, message_body, message_type, received_at,
                       COALESCE(direction, 'in') as direction
                FROM replies WHERE user_id = %s AND template_name = %s
                ORDER BY received_at DESC
            """, (user_id, template))
        else:
            cur.execute("""
                SELECT id, from_phone, contact_name, message_body, message_type, received_at,
                       COALESCE(direction, 'in') as direction
                FROM replies WHERE user_id = %s
                ORDER BY received_at DESC
            """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        return jsonify({
            'success': True,
            'count':   len(rows),
            'replies': [
                {
                    'id':           r[0],
                    'from_phone':   r[1],
                    'contact_name': r[2] or '',
                    'message_body': r[3],
                    'message_type': r[4],
                    'received_at':  r[5].isoformat() if r[5] else '',
                    'direction':    r[6],
                }
                for r in rows
            ]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/replies/count
@replies_bp.route('/count', methods=['GET'])
def reply_count():
    user_id  = session.get('user_id')
    template = request.args.get('template', '').strip()
    conn = get_conn()
    try:
        cur = conn.cursor()
        if template and template != 'all':
            cur.execute(
                'SELECT COUNT(*) FROM replies WHERE user_id = %s AND template_name = %s',
                (user_id, template)
            )
        else:
            cur.execute('SELECT COUNT(*) FROM replies WHERE user_id = %s', (user_id,))
        count = cur.fetchone()[0]
        cur.close()
        return jsonify({'success': True, 'count': count})
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
        args=(cr_id, phone, template_name, template_lang, creds, user_id, message_text),
        daemon=True
    ).start()

    return jsonify({'success': True, 'cr_id': cr_id, 'template_name': template_name})


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
