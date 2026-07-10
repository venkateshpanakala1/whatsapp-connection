from flask import Blueprint, request, jsonify, session
import csv
import io
from db import get_conn, put_conn

contacts_bp = Blueprint('contacts', __name__)


def clean_phone(phone):
    p = str(phone).strip()
    if not p or p.lower() == 'none':
        return ''
    leading_plus = p.startswith('+')
    digits = ''.join(c for c in p if c.isdigit())
    if leading_plus:
        digits = '+' + digits
    return digits if len(digits) >= 7 else ''


PHONE_COLS = {'numbers', 'number', 'phone', 'mobile', 'phone_number', 'phonenumber', 'whatsapp', 'contact'}
NAME_COLS  = {'name', 'full_name', 'contact_name', 'customer_name'}


def parse_rows(rows):
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return []

    first = [str(c).strip().lower() for c in rows[0]]

    phone_col_hit = next((c for c in first if c in PHONE_COLS), None)
    name_col_hit  = next((c for c in first if c in NAME_COLS),  None)

    if phone_col_hit:
        # Header row recognised
        phone_idx = first.index(phone_col_hit)
        name_idx  = first.index(name_col_hit) if name_col_hit else None
        data_rows = rows[1:]
    elif len(first) == 1:
        # Single column, no recognised header — treat all rows as phone numbers
        phone_idx = 0
        name_idx  = None
        data_rows = rows
    else:
        # Multiple columns, no recognised header — first col = phone, second = name if present
        phone_idx = 0
        name_idx  = 1 if len(first) >= 2 else None
        data_rows = rows

    contacts = []
    for r in data_rows:
        phone = clean_phone(r[phone_idx] if phone_idx < len(r) else '')
        name  = str(r[name_idx]).strip() if name_idx is not None and name_idx < len(r) else ''
        if phone:
            contacts.append({'phone': phone, 'name': name})

    return contacts


# POST /api/contacts/parse
@contacts_bp.route('/parse', methods=['POST'])
def parse_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    fname = (file.filename or '').lower()
    raw = file.read()

    try:
        if fname.endswith('.csv'):
            text = raw.decode('utf-8-sig')
            rows = list(csv.reader(io.StringIO(text)))
        elif fname.endswith(('.xlsx', '.xls')):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            ws = wb.active
            rows = [
                [str(cell.value) if cell.value is not None else '' for cell in row]
                for row in ws.iter_rows()
            ]
        else:
            return jsonify({'error': 'Only CSV and Excel (.xlsx) files are supported'}), 400

        contacts = parse_rows(rows)
        return jsonify({'success': True, 'contacts': contacts, 'count': len(contacts)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/contacts/import
@contacts_bp.route('/import', methods=['POST'])
def import_contacts():
    user_id = session.get('user_id')
    body = request.get_json()
    contacts    = body.get('contacts', [])
    source_file = (body.get('source_file') or 'unknown').strip()
    if not contacts:
        return jsonify({'error': 'No contacts to import'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        saved = 0
        for c in contacts:
            phone = (c.get('phone') or '').strip()
            name  = (c.get('name') or '').strip() or None
            if not phone:
                continue
            cur.execute("""
                INSERT INTO contacts (user_id, name, phone, source_file)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, phone) DO UPDATE
                    SET name = EXCLUDED.name, source_file = EXCLUDED.source_file, updated_at = NOW()
            """, (user_id, name, phone, source_file))
            saved += 1
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'{saved} contacts imported successfully'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/contacts/files
@contacts_bp.route('/files', methods=['GET'])
def list_files():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT source_file, COUNT(*) as cnt, MAX(created_at) as imported_at
            FROM contacts WHERE user_id = %s
            GROUP BY source_file
            ORDER BY MAX(created_at) DESC
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        files = [
            # Naive TIMESTAMP columns are written in UTC (Postgres's NOW()) —
            # mark it so the browser converts to local time/date correctly.
            {'file': r[0], 'count': r[1], 'imported_at': (r[2].isoformat() + 'Z') if r[2] else ''}
            for r in rows
        ]
        return jsonify({'success': True, 'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/contacts/numbers
@contacts_bp.route('/numbers', methods=['GET'])
def get_numbers():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT phone, name FROM contacts WHERE user_id = %s ORDER BY id ASC', (user_id,))
        rows = cur.fetchall()
        cur.close()
        numbers = [{'phone': r[0], 'name': r[1] or ''} for r in rows]
        return jsonify({'success': True, 'numbers': numbers, 'count': len(numbers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# GET /api/contacts/list
@contacts_bp.route('/list', methods=['GET'])
def list_contacts():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT id, name, phone, created_at FROM contacts WHERE user_id = %s ORDER BY id DESC',
            (user_id,)
        )
        rows = cur.fetchall()
        cur.close()
        contacts = [
            # See note above — naive TIMESTAMP is UTC, mark it explicitly.
            {'id': r[0], 'name': r[1] or '', 'phone': r[2], 'created_at': (r[3].isoformat() + 'Z') if r[3] else ''}
            for r in rows
        ]
        return jsonify({'success': True, 'contacts': contacts, 'count': len(contacts)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# DELETE /api/contacts/file
@contacts_bp.route('/file', methods=['DELETE'])
def delete_file():
    user_id = session.get('user_id')
    source_file = (request.args.get('name') or '').strip()
    if not source_file:
        return jsonify({'error': 'File name is required'}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'DELETE FROM contacts WHERE user_id = %s AND source_file = %s',
            (user_id, source_file)
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'{deleted} contacts deleted'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


# DELETE /api/contacts/clear
@contacts_bp.route('/clear', methods=['DELETE'])
def clear_contacts():
    user_id = session.get('user_id')
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM contacts WHERE user_id = %s', (user_id,))
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'message': 'All contacts cleared'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)
