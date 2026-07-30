from flask import Blueprint, request, jsonify, session
import requests as http
from datetime import datetime, timedelta, timezone
from db import get_conn, put_conn

budget_bp = Blueprint('budget', __name__)
META_API = 'https://graph.facebook.com/v22.0'

# Fixed display order for conversation categories — colors on the frontend
# are assigned to these positions, so the mapping never shifts even if a
# given day has zero cost in one category.
CATEGORY_ORDER = ['MARKETING', 'UTILITY', 'AUTHENTICATION', 'SERVICE']


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


def get_bulk_sent_count(user_id, start_dt, end_dt):
    """Actual messages sent via Bulk Send in this range — separate from
    Meta's own billed-message count above, which also includes counter-
    replies and other conversation types. send_logs.sent_at is a naive
    UTC TIMESTAMP (Postgres's NOW()), so the tz-aware start/end passed in
    have their tzinfo stripped to compare correctly against it."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM send_logs
            WHERE user_id = %s AND status = 'sent'
              AND sent_at >= %s AND sent_at < %s
        """, (user_id, start_dt.replace(tzinfo=None), end_dt.replace(tzinfo=None)))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 0
    finally:
        put_conn(conn)


# GET /api/budget/spend?days=30
#
# Pulls real per-day WhatsApp spend directly from Meta for this WABA,
# deliberately NOT computed from our own send_logs — Meta's per-category/
# per-country pricing changes and varies, and a locally-guessed number would
# drift from the tenant's actual bill.
#
# A live test on this account confirmed Meta's own WhatsApp Manager billing
# page shows real spend, but the older `conversation_analytics` field came
# back completely absent (not just empty) from the Graph API response —
# that's what Meta does for a field that no longer applies to an account,
# not one that's genuinely empty. Since Meta migrated accounts to
# per-message pricing through 2025, this now also requests the newer
# `pricing_analytics` field alongside it and uses whichever one actually
# comes back populated. If numbers still look wrong or missing, check the
# raw response this endpoint logs (and returns inline as `_debug_raw_meta_
# response` when nothing was found) against Meta's current docs for the
# exact per-message pricing analytics field/dimension names.
@budget_bp.route('/spend', methods=['GET'])
def spend():
    user_id = session.get('user_id')
    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        days = 30
    days = max(1, min(days, 90))

    end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start_dt = end_dt - timedelta(days=days)

    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())

    try:
        res = http.get(
            f"{META_API}/{creds['waba_id']}",
            params={
                # Requesting both the older conversation-based field and the
                # newer per-message-pricing field in one call — a live test
                # confirmed Meta returns real spend data on its own WhatsApp
                # Manager billing page for this account, but our first
                # attempt (conversation_analytics) came back completely
                # absent from the response with no error at all, which is
                # what Meta does for a field that doesn't apply to this
                # account rather than one that's just empty. Since Meta
                # migrated accounts to per-message pricing through 2025,
                # `pricing_analytics` is the likely correct field now —
                # asking for both means whichever one Meta actually
                # recognizes shows up, without another guess-and-redeploy
                # round trip.
                'fields': (
                    'currency,'
                    f"conversation_analytics.start({start_ts}).end({end_ts})"
                    '.granularity(DAILY).dimensions(["conversation_category"]),'
                    f"pricing_analytics.start({start_ts}).end({end_ts})"
                    '.granularity(DAILY).dimensions(["pricing_category","pricing_type"])'
                ),
            },
            headers={'Authorization': f"Bearer {creds['access_token']}"},
            timeout=15
        )
        data = res.json()
        print(f"[budget] Meta raw response for waba_id={creds['waba_id']}: {data}")
        if 'error' in data:
            return jsonify({'error': data['error'].get('message', 'Meta API error')}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Whichever of the two fields Meta actually recognized for this account
    # is the one with real data in it — the other comes back absent, same as
    # conversation_analytics did on its own above. Read both, use whichever
    # has data_points.
    try:
        points = []
        for field_name in ('conversation_analytics', 'pricing_analytics'):
            for block in data.get(field_name, {}).get('data', []):
                points.extend(block.get('data_points', []))
    except Exception as e:
        return jsonify({'error': f"Unexpected response shape from Meta: {e}"}), 502

    by_date = {}
    for p in points:
        ts = p.get('start')
        if ts is None:
            continue
        date_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        entry = by_date.setdefault(date_key, {
            'date': date_key, 'total_cost': 0.0, 'total_conversations': 0, 'by_category': {}
        })
        # Field names differ between the two possible sources above
        # (conversation_category/conversation vs pricing_category/volume) —
        # the exact per-message-pricing shape isn't confirmed yet, so read
        # whichever key is actually present rather than assuming one.
        cat = (p.get('conversation_category') or p.get('pricing_category') or 'OTHER').upper()
        cost  = float(p.get('cost') or 0)
        convs = int(p.get('conversation') or p.get('volume') or 0)
        entry['total_cost'] += cost
        entry['total_conversations'] += convs
        entry['by_category'][cat] = entry['by_category'].get(cat, 0) + cost

    days_list = sorted(by_date.values(), key=lambda d: d['date'])
    for d in days_list:
        d['total_cost'] = round(d['total_cost'], 2)
        d['by_category'] = {k: round(v, 2) for k, v in d['by_category'].items()}

    grand_total         = round(sum(d['total_cost'] for d in days_list), 2)
    grand_conversations  = sum(d['total_conversations'] for d in days_list)
    bulk_sent_count      = get_bulk_sent_count(user_id, start_dt, end_dt)

    response = {
        'success': True,
        'currency': data.get('currency') or 'USD',
        'days': days_list,
        'category_order': CATEGORY_ORDER,
        'grand_total': grand_total,
        'grand_conversations': grand_conversations,
        'bulk_sent_count': bulk_sent_count,
    }
    # Temporary: while this endpoint is still unverified against a live
    # account, surface Meta's exact raw response whenever we found nothing,
    # so it's visible right on the page — no Railway log access needed to
    # tell whether Meta returned truly empty analytics vs. something our
    # parsing missed.
    if not days_list:
        response['_debug_raw_meta_response'] = data
    return jsonify(response)
