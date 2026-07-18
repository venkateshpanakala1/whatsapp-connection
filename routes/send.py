from flask import Blueprint, request, jsonify, Response, session
import requests as http
from db import get_conn, put_conn
import json
import time
import threading
import uuid

send_bp = Blueprint('send', __name__)
META_API = 'https://graph.facebook.com/v22.0'


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


def get_contacts_by_file(source_file, user_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT phone, name FROM contacts WHERE user_id = %s AND source_file = %s ORDER BY id ASC',
            (user_id, source_file)
        )
        rows = cur.fetchall()
        cur.close()
        return [{'phone': r[0], 'name': r[1] or ''} for r in rows]
    finally:
        put_conn(conn)


def log_send(job_id, phone, name, template_name, status, user_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO send_logs (user_id, job_id, phone, name, template_name, status)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, job_id, phone, name or '', template_name, status))
        conn.commit()
        cur.close()
    except Exception:
        pass
    finally:
        try: put_conn(conn)
        except: pass


# ── Job state lives in the DB (not a process-local dict) so pause/resume/
# status/progress work no matter which gunicorn worker handles the request. ──

def create_job(job_id, user_id, source_file, template_name, total, delay):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO send_jobs (id, user_id, source_file, template_name, status, total, delay)
            VALUES (%s, %s, %s, %s, 'pending', %s, %s)
        """, (job_id, user_id, source_file, template_name, total, delay))
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


def set_job_status(job_id, status):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE send_jobs SET status=%s, updated_at=NOW() WHERE id=%s', (status, job_id))
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


def bump_job_progress(job_id, success, error_msg=None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if success:
            cur.execute("""
                UPDATE send_jobs SET current = current + 1, sent = sent + 1, updated_at = NOW()
                WHERE id = %s
            """, (job_id,))
        else:
            cur.execute("""
                UPDATE send_jobs
                SET current = current + 1, failed = failed + 1, updated_at = NOW(),
                    errors = CASE WHEN jsonb_array_length(errors) < 10
                                  THEN errors || to_jsonb(%s::text)
                                  ELSE errors END
                WHERE id = %s
            """, (error_msg, job_id))
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


def get_job(job_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, current, total, sent, failed, errors
            FROM send_jobs WHERE id = %s
        """, (job_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {
            'status': row[0], 'current': row[1], 'total': row[2],
            'sent': row[3], 'failed': row[4], 'errors': row[5] or []
        }
    finally:
        put_conn(conn)


def wait_if_paused(job_id):
    while True:
        job = get_job(job_id)
        if not job or job['status'] != 'paused':
            return
        time.sleep(1)


def build_template_components(header_format, header_media_url, header_filename, body_params, contact):
    """
    Build the `components` array WhatsApp requires on every send for templates
    that have a media header and/or {{n}} body variables. header_handle from
    template creation is only used for Meta's approval preview — actual sends
    must supply the real values every time, or Meta returns error #132012.
    """
    components = []

    if header_format in ('IMAGE', 'VIDEO', 'DOCUMENT') and header_media_url:
        key = header_format.lower()
        media_obj = {'link': header_media_url}
        if header_format == 'DOCUMENT' and header_filename:
            media_obj['filename'] = header_filename
        components.append({
            'type': 'header',
            'parameters': [{'type': key, key: media_obj}]
        })

    if body_params:
        parameters = []
        for val in body_params:
            if val == '{{name}}':
                resolved = contact.get('name') or contact['phone']
            elif val == '{{phone}}':
                resolved = contact['phone']
            else:
                resolved = val
            parameters.append({'type': 'text', 'text': resolved or ''})
        components.append({'type': 'body', 'parameters': parameters})

    return components


def send_worker(job_id, contacts, template_name, template_lang, creds, delay, user_id,
                 header_format=None, header_media_url='', header_filename='', body_params=None):
    set_job_status(job_id, 'running')

    for contact in contacts:
        wait_if_paused(job_id)

        phone = contact['phone']
        name  = contact.get('name', '')
        components = build_template_components(header_format, header_media_url, header_filename, body_params, contact)
        template = {'name': template_name, 'language': {'code': template_lang}}
        if components:
            template['components'] = components
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                phone,
            'type':              'template',
            'template':          template
        }
        try:
            res  = http.post(
                f"{META_API}/{creds['phone_number_id']}/messages",
                headers={
                    'Authorization': f"Bearer {creds['access_token']}",
                    'Content-Type':  'application/json'
                },
                json=payload,
                timeout=10
            )
            data = res.json()
            if 'error' in data:
                bump_job_progress(job_id, False, f"{phone}: {data['error']['message']}")
                log_send(job_id, phone, name, template_name, 'failed', user_id)
            else:
                bump_job_progress(job_id, True)
                log_send(job_id, phone, name, template_name, 'sent', user_id)
        except Exception:
            bump_job_progress(job_id, False, f"{phone}: request failed")
            log_send(job_id, phone, name, template_name, 'failed', user_id)

        elapsed = 0
        while elapsed < delay:
            wait_if_paused(job_id)
            time.sleep(0.5)
            elapsed += 0.5

    set_job_status(job_id, 'done')


# GET /api/send/templates
@send_bp.route('/templates', methods=['GET'])
def get_templates():
    user_id = session.get('user_id')
    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400
    try:
        all_templates = []
        url    = f"{META_API}/{creds['waba_id']}/message_templates"
        params = {'limit': 100, 'fields': 'id,name,category,language,status,components'}
        headers = {'Authorization': f"Bearer {creds['access_token']}"}

        while url:
            res  = http.get(url, params=params, headers=headers, timeout=10)
            data = res.json()
            if 'error' in data:
                return jsonify({'error': data['error']['message']}), 400
            all_templates.extend(data.get('data', []))
            url    = data.get('paging', {}).get('next')
            params = {}

        approved = [
            t for t in all_templates
            if t.get('status') in ('APPROVED', 'PENDING')
            and not t.get('name', '').startswith('cr_')
        ]
        return jsonify({'success': True, 'templates': approved[:5], 'all': approved})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/send/start
@send_bp.route('/start', methods=['POST'])
def start_send():
    user_id          = session.get('user_id')
    body             = request.get_json()
    source_file      = (body.get('source_file')     or '').strip()
    template_name    = (body.get('template_name')   or '').strip()
    template_lang    = (body.get('template_lang')   or 'en').strip()
    delay            = int(body.get('delay', 1))
    header_format    = (body.get('header_format')    or '').strip().upper()
    header_media_url = (body.get('header_media_url') or '').strip()
    header_filename  = (body.get('header_filename')  or '').strip()
    body_params      = body.get('body_params') or []

    if not source_file or not template_name:
        return jsonify({'error': 'source_file and template_name are required'}), 400

    if header_format in ('IMAGE', 'VIDEO', 'DOCUMENT') and not header_media_url:
        return jsonify({'error': 'This template has a media header — a header media URL is required'}), 400

    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    contacts = get_contacts_by_file(source_file, user_id)
    if not contacts:
        return jsonify({'error': f'No contacts found in "{source_file}"'}), 400

    job_id = str(uuid.uuid4())
    create_job(job_id, user_id, source_file, template_name, len(contacts), delay)

    t = threading.Thread(
        target=send_worker,
        args=(job_id, contacts, template_name, template_lang, creds, delay, user_id),
        kwargs={
            'header_format':    header_format,
            'header_media_url': header_media_url,
            'header_filename':  header_filename,
            'body_params':      body_params,
        },
        daemon=True
    )
    t.start()

    return jsonify({'success': True, 'job_id': job_id, 'total': len(contacts)})


# POST /api/send/pause/<job_id>
@send_bp.route('/pause/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'running':
        return jsonify({'error': f"Can't pause a job that is {job['status']}"}), 400
    set_job_status(job_id, 'paused')
    return jsonify({'success': True})


# POST /api/send/resume/<job_id>
@send_bp.route('/resume/<job_id>', methods=['POST'])
def resume_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'paused':
        return jsonify({'error': f"Can't resume a job that is {job['status']}"}), 400
    set_job_status(job_id, 'running')
    return jsonify({'success': True})


# GET /api/send/status/<job_id>
@send_bp.route('/status/<job_id>')
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'found': False}), 404
    return jsonify({'found': True, **job})


# GET /api/send/progress/<job_id>  — SSE stream
@send_bp.route('/progress/<job_id>')
def progress(job_id):
    if not get_job(job_id):
        return jsonify({'error': 'Job not found'}), 404

    def generate():
        while True:
            job = get_job(job_id) or {}
            yield f"data: {json.dumps(job)}\n\n"
            if job.get('status') == 'done':
                break
            time.sleep(1)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )
