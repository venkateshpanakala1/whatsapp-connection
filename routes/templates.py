from flask import Blueprint, request, jsonify, session, Response
import requests as http
from db import get_conn, put_conn
import secrets

templates_bp = Blueprint('templates', __name__)
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


def get_app_id(access_token):
    try:
        res = http.get(f'{META_API}/debug_token', params={
            'input_token': access_token,
            'access_token': access_token
        }, timeout=10)
        return res.json().get('data', {}).get('app_id')
    except:
        return None


# GET /api/templates/list
@templates_bp.route('/list', methods=['GET'])
def list_templates():
    creds = get_wa_credentials(session.get('user_id'))
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400
    try:
        res = http.get(
            f"{META_API}/{creds['waba_id']}/message_templates",
            params={'limit': 2, 'fields': 'id,name,category,language,status,components,created_time'},
            headers={'Authorization': f"Bearer {creds['access_token']}"},
            timeout=10
        )
        data = res.json()
        if 'error' in data:
            return jsonify({'error': data['error']['message']}), 400

        templates = data.get('data', [])
        templates.sort(key=lambda t: t.get('created_time', ''), reverse=True)

        return jsonify({'success': True, 'data': templates[:2]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/templates/upload-media
@templates_bp.route('/upload-media', methods=['POST'])
def upload_media():
    creds = get_wa_credentials(session.get('user_id'))
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    file_data = file.read()
    file_size = len(file_data)
    mime_type = file.mimetype

    app_id = get_app_id(creds['access_token'])
    if not app_id:
        return jsonify({'error': 'Could not get App ID from token'}), 400

    try:
        # Step 1: Create upload session
        session_res = http.post(
            f"{META_API}/{app_id}/uploads",
            params={
                'file_length': file_size,
                'file_type': mime_type,
                'access_token': creds['access_token']
            },
            timeout=30
        )
        session_data = session_res.json()
        if 'error' in session_data:
            return jsonify({'error': session_data['error']['message']}), 400

        upload_session_id = session_data['id']

        # Step 2: Upload file binary
        upload_res = http.post(
            f"{META_API}/{upload_session_id}",
            headers={
                'Authorization': f"OAuth {creds['access_token']}",
                'file_offset': '0',
                'Content-Type': mime_type
            },
            data=file_data,
            timeout=60
        )
        upload_data = upload_res.json()
        if 'error' in upload_data:
            return jsonify({'error': upload_data['error']['message']}), 400

        handle = upload_data.get('h')
        if not handle:
            return jsonify({'error': 'Upload failed — no handle returned'}), 400

        return jsonify({'success': True, 'handle': handle})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/templates/create
@templates_bp.route('/create', methods=['POST'])
def create_template():
    user_id = session.get('user_id')
    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    body = request.get_json()
    name          = (body.get('name') or '').strip().lower().replace(' ', '_')
    category      = body.get('category', 'MARKETING')
    language      = body.get('language', 'en')
    header_type   = body.get('header_type', 'NONE')   # NONE / IMAGE / DOCUMENT
    header_handle = body.get('header_handle', '')
    body_text     = (body.get('body_text') or '').strip()
    footer_text   = (body.get('footer_text') or '').strip()

    if not name or not body_text:
        return jsonify({'error': 'Template name and body text are required'}), 400

    # Build Meta API payload
    components = []

    if header_type in ('IMAGE', 'DOCUMENT') and header_handle:
        components.append({
            'type': 'HEADER',
            'format': header_type,
            'example': {'header_handle': [header_handle]}
        })

    components.append({'type': 'BODY', 'text': body_text})

    if footer_text:
        components.append({'type': 'FOOTER', 'text': footer_text})

    payload = {
        'name': name,
        'category': category,
        'language': language,
        'components': components
    }

    try:
        res = http.post(
            f"{META_API}/{creds['waba_id']}/message_templates",
            headers={
                'Authorization': f"Bearer {creds['access_token']}",
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=30
        )
        data = res.json()
        if 'error' in data:
            return jsonify({'error': data['error']['message']}), 400

        # Save to local DB
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO whatsapp_templates
                    (user_id, name, category, language, header_type, body_text, footer_text, status, meta_template_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, name, category, language, header_type,
                body_text, footer_text or None,
                data.get('status', 'PENDING'), data.get('id')
            ))
            conn.commit()
            cur.close()
        finally:
            put_conn(conn)

        return jsonify({
            'success': True,
            'message': f'Template "{name}" submitted for Meta approval',
            'data': data
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/templates/attach-media
# Stores a template's header image/document in our own DB so Bulk Send can
# auto-reuse it. Meta only keeps the file we send during creation as an
# approval-preview example — it is never reusable for actual sends, so we
# keep our own copy and serve it back by URL at send time.
@templates_bp.route('/attach-media', methods=['POST'])
def attach_media():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401

    name = (request.form.get('name') or '').strip().lower()
    if not name:
        return jsonify({'error': 'Template name is required'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file      = request.files['file']
    data      = file.read()
    mime_type = file.mimetype
    token     = secrets.token_urlsafe(16)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO template_media (user_id, template_name, token, mime_type, data)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, template_name) DO UPDATE
                SET token = EXCLUDED.token, mime_type = EXCLUDED.mime_type,
                    data = EXCLUDED.data, created_at = NOW()
        """, (user_id, name, token, mime_type, data))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/templates/media-url?name=<template_name>
# Tells the caller (Bulk Send) whether we have a stored header image/document
# for this template, and if so, its public URL.
@templates_bp.route('/media-url', methods=['GET'])
def media_url():
    user_id = session.get('user_id')
    name = (request.args.get('name') or '').strip().lower()
    if not user_id or not name:
        return jsonify({'success': True, 'has_media': False})

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT token FROM template_media WHERE user_id = %s AND template_name = %s',
            (user_id, name)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'success': True, 'has_media': False})
        url = f"{request.host_url.rstrip('/')}/api/templates/media/{row[0]}"
        return jsonify({'success': True, 'has_media': True, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/templates/media/<token>  — public, no auth (Meta's servers fetch this directly)
@templates_bp.route('/media/<token>', methods=['GET'])
def media_file(token):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT data, mime_type FROM template_media WHERE token = %s', (token,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return Response(bytes(row[0]), mimetype=row[1] or 'application/octet-stream')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)
