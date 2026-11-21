"""Plans, entitlement maths and Razorpay payments (prepaid passes, no auto-renewal).

Prices and limits live in Supabase table public.plans - the owner edits them in the Table Editor and the
site picks the change up within 60 s (cache in accounts.plans()). DEFAULT_PLANS below is only a fallback
while that table does not exist yet.

Env (never commit values): RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET.
Everything in this file except create_order() is pure logic, unit-tested in tests/test_billing.py."""
import os, hmac, hashlib, datetime as dt, requests

KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '').strip()
KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '').strip()
WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '').strip()
RZP_API = 'https://api.razorpay.com/v1'
MONTH = dt.timedelta(days=30)          # minutes refill every 30 days (yearly pass)
STYLES = ('educational', 'cinematic', 'realistic', 'youtube')
UTC = dt.timezone.utc

def keys_set(): return bool(KEY_ID and KEY_SECRET)

# Same values as the seed rows in supabase/schema.sql (Phase 3).
DEFAULT_PLANS = [
    dict(id='free', kind='free', name_hi='मुफ़्त', name_en='Free', desc_hi='रोज़ 2 वीडियो, हर वीडियो 1 मिनट तक',
         desc_en='2 videos a day, up to 1 min each', price_paise=0, period_days=None, minutes_quota=0, daily_videos=2,
         max_video_minutes=1, resolution=720, styles=['educational', 'youtube'], both_langs_one_charge=False, priority=0, active=True, sort=0),
    dict(id='starter', kind='pass', name_hi='स्टार्टर', name_en='Starter', desc_hi='30 दिन में 60 मिनट, 5 मिनट तक की वीडियो',
         desc_en='60 min in 30 days, videos up to 5 min', price_paise=19900, period_days=30, minutes_quota=60, daily_videos=None,
         max_video_minutes=5, resolution=1080, styles=['educational', 'youtube'], both_langs_one_charge=False, priority=5, active=True, sort=10),
    dict(id='pro', kind='pass', name_hi='प्रो', name_en='Pro', desc_hi='30 दिन में 200 मिनट, 10 मिनट तक, सभी स्टाइल, जल्दी नंबर',
         desc_en='200 min in 30 days, up to 10 min, all styles, priority', price_paise=49900, period_days=30, minutes_quota=200,
         daily_videos=None, max_video_minutes=10, resolution=1080, styles=None, both_langs_one_charge=False, priority=10, active=True, sort=20),
    dict(id='creator', kind='pass', name_hi='क्रिएटर', name_en='Creator', desc_hi='30 दिन में 600 मिनट, हिंदी+English एक साथ, सबसे पहले नंबर',
         desc_en='600 min in 30 days, Hindi+English together, top priority', price_paise=129900, period_days=30, minutes_quota=600,
         daily_videos=None, max_video_minutes=10, resolution=1080, styles=None, both_langs_one_charge=True, priority=20, active=True, sort=30),
    dict(id='pack', kind='pack', name_hi='मिनट पैक', name_en='Minute pack', desc_hi='15 मिनट एक बार — प्लान की तारीख़ नहीं बदलती',
         desc_en='15 minutes once — does not change your plan dates', price_paise=4900, period_days=None, minutes_quota=15,
         daily_videos=None, max_video_minutes=5, resolution=1080, styles=['educational', 'youtube'], both_langs_one_charge=False,
         priority=5, active=True, sort=40),
    dict(id='pro_yearly', kind='pass', name_hi='प्रो सालाना', name_en='Pro yearly', desc_hi='हर महीने 200 मिनट, 12 महीने (2 महीने मुफ़्त)',
         desc_en='200 min every month for 12 months (2 months free)', price_paise=499000, period_days=365, minutes_quota=200,
         daily_videos=None, max_video_minutes=10, resolution=1080, styles=None, both_langs_one_charge=False, priority=10, active=True, sort=50),
]

def normalize(row):
    """Clean one plans row (owner may leave blanks in the Table Editor)."""
    r = dict(row)
    r['kind'] = r.get('kind') or 'pass'
    r['price_paise'] = int(r.get('price_paise') or 0)
    r['period_days'] = int(r['period_days']) if r.get('period_days') else None
    r['minutes_quota'] = float(r.get('minutes_quota') or 0)
    r['daily_videos'] = int(r['daily_videos']) if r.get('daily_videos') is not None else None
    r['max_video_minutes'] = float(r.get('max_video_minutes') or 1)
    r['resolution'] = 1080 if int(r.get('resolution') or 720) >= 1080 else 720
    st = [s for s in (r.get('styles') or []) if s in STYLES]
    r['styles'] = st or None                      # None = all styles
    r['both_langs_one_charge'] = bool(r.get('both_langs_one_charge'))
    r['priority'] = int(r.get('priority') or 0)
    r['active'] = r.get('active') is not False
    r['sort'] = int(r.get('sort') or 0)
    r['name_hi'] = r.get('name_hi') or r['id']; r['name_en'] = r.get('name_en') or r['id']
    return r

def index(rows):
    d = {r['id']: normalize(r) for r in rows}
    if 'free' not in d: d['free'] = normalize(DEFAULT_PLANS[0])
    return d

def public(plan):
    """What the plans page may show."""
    keys = ('id', 'kind', 'name_hi', 'name_en', 'desc_hi', 'desc_en', 'price_paise', 'period_days', 'minutes_quota',
            'daily_videos', 'max_video_minutes', 'resolution', 'styles', 'both_langs_one_charge', 'priority', 'sort')
    return {k: plan.get(k) for k in keys}

def style_ok(plan, style): return not plan.get('styles') or style in plan['styles']

# ---------------- time helpers ----------------
def now(): return dt.datetime.now(UTC)
def parse(ts):
    if not ts: return None
    if isinstance(ts, dt.datetime): return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    s = str(ts).replace('Z', '+00:00')
    if '.' in s:                                  # trim to microseconds for fromisoformat on older Pythons
        head, rest = s.split('.', 1); frac = ''.join(c for c in rest if c.isdigit()); tz = rest[len(frac):]
        s = f'{head}.{frac[:6].ljust(6, "0")}{tz}'
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)
def iso(d): return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ') if d else None
def num(x): return round(float(x or 0), 1)

def state(p):
    """The entitlement fields of a profile row, normalized."""
    return {'plan': p.get('plan') or 'free', 'plan_expires_at': p.get('plan_expires_at'),
            'minutes_left': num(p.get('minutes_left')), 'bonus_minutes': num(p.get('bonus_minutes')),
            'period_resets_at': p.get('period_resets_at')}

# ---------------- entitlement maths ----------------
def refresh(p, plans, t=None):
    """Apply expiry (-> free) and the monthly refill. Returns (plan_row, new_state, changed)."""
    t = t or now(); s = state(p); changed = False
    if s['plan'] != 'free':
        exp = parse(s['plan_expires_at'])
        if exp is None or exp <= t:                         # pass ended: back to free, unused pass minutes end too
            s.update(plan='free', plan_expires_at=None, minutes_left=0.0, period_resets_at=None); changed = True
        else:
            plan = plans.get(s['plan'])
            r = parse(s['period_resets_at'])
            if plan and r and r <= t:                        # yearly pass: fresh monthly minutes
                while r <= t: r += MONTH
                s['minutes_left'] = num(plan['minutes_quota'])
                s['period_resets_at'] = iso(r) if r < exp else None
                changed = True
    plan = plans.get(s['plan']) or plans['free']
    return plan, s, changed

def job_cost(plan, minutes, langs):
    """Minutes charged: video length x number of languages (Creator: Hindi+English counted once)."""
    n = len([l for l in (langs or 'hi').split(',') if l])
    if plan.get('both_langs_one_charge'): n = 1
    return num(float(minutes) * max(1, n))

def split_spend(minutes_left, bonus, cost):
    """Take cost from this period's minutes first, then from pack minutes. None if not enough."""
    minutes_left, bonus, cost = num(minutes_left), num(bonus), num(cost)
    if minutes_left + bonus + 1e-9 < cost: return None
    a = min(minutes_left, cost); b = num(cost - a)
    return num(a), b

def apply_purchase(p, buy, plans, t=None):
    """New entitlement after buying plan `buy`. p = profile row as stored (refresh is applied first)."""
    t = t or now()
    _, s, _ = refresh(p, plans, t)
    s = dict(s)
    q = num(buy['minutes_quota'])
    if buy['kind'] == 'pack' or not buy.get('period_days'):          # pack: add minutes, dates unchanged
        s['bonus_minutes'] = num(s['bonus_minutes'] + q)
        return s
    period = dt.timedelta(days=buy['period_days'])
    if s['plan'] == buy['id']:                                        # same pass again: extend + add minutes
        exp = parse(s['plan_expires_at'])
        s['plan_expires_at'] = iso(exp + period)
        s['minutes_left'] = num(s['minutes_left'] + q)
        if buy['period_days'] > 30 and not s['period_resets_at']: s['period_resets_at'] = iso(t + MONTH)
        return s
    # new / different pass: starts now; unused minutes of the old pass are kept as pack minutes
    s['bonus_minutes'] = num(s['bonus_minutes'] + (s['minutes_left'] if s['plan'] != 'free' else 0))
    s['plan'] = buy['id']
    s['plan_expires_at'] = iso(t + period)
    s['minutes_left'] = q
    s['period_resets_at'] = iso(t + MONTH) if buy['period_days'] > 30 else None
    return s

# ---------------- Razorpay ----------------
def hmac_hex(msg, secret):
    if isinstance(msg, str): msg = msg.encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

def verify_payment(order_id, payment_id, signature, secret=None):
    """Checkout handler signature = HMAC-SHA256(order_id + '|' + payment_id, key_secret)."""
    secret = KEY_SECRET if secret is None else secret
    if not (secret and order_id and payment_id and signature): return False
    return hmac.compare_digest(hmac_hex(f'{order_id}|{payment_id}', secret), str(signature))

def verify_webhook(body, signature, secret=None):
    """X-Razorpay-Signature = HMAC-SHA256(raw request body, webhook secret)."""
    secret = WEBHOOK_SECRET if secret is None else secret
    if not (secret and signature and body is not None): return False
    return hmac.compare_digest(hmac_hex(body, secret), str(signature))

def parse_webhook(ev):
    """payment.captured / order.paid -> dict(order_id, payment_id, amount, currency); other events -> None."""
    name = ev.get('event')
    if name not in ('payment.captured', 'order.paid'): return None
    pl = ev.get('payload') or {}
    pay = (pl.get('payment') or {}).get('entity') or {}
    order = (pl.get('order') or {}).get('entity') or {}
    oid = pay.get('order_id') or order.get('id')
    amount = pay.get('amount') if pay.get('amount') is not None else order.get('amount_paid')
    if not oid: return None
    return {'event': name, 'order_id': oid, 'payment_id': pay.get('id'), 'amount': int(amount or 0),
            'currency': pay.get('currency') or order.get('currency') or 'INR'}

def create_order(amount_paise, receipt, notes):
    r = requests.post(f'{RZP_API}/orders', auth=(KEY_ID, KEY_SECRET), timeout=20,
                      json={'amount': int(amount_paise), 'currency': 'INR', 'receipt': receipt[:40], 'notes': notes})
    if r.status_code >= 300:
        raise RuntimeError(f'razorpay order {r.status_code}: {r.text[:200]}')
    return r.json()
