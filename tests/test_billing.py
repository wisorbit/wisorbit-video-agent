"""Unit tests for plans / payments logic. Fake keys only - no network, no real payments.
Run: python -m unittest discover -s tests -v"""
import os, sys, json, hmac, hashlib, unittest, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SUPABASE_URL', 'http://fake.local')
os.environ.setdefault('SUPABASE_SECRET_KEY', 'fake')
import billing

KEY_SECRET = 'test_key_secret_FAKE'
WEBHOOK_SECRET = 'test_webhook_secret_FAKE'
P = billing.index(billing.DEFAULT_PLANS)
T0 = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)

def sig(msg, secret): return hmac.new(secret.encode(), msg if isinstance(msg, bytes) else msg.encode(), hashlib.sha256).hexdigest()

def webhook_body(event, order_id='order_TEST1', payment_id='pay_TEST1', amount=49900):
    ev = {'entity': 'event', 'event': event, 'payload': {
        'payment': {'entity': {'id': payment_id, 'order_id': order_id, 'amount': amount, 'currency': 'INR', 'status': 'captured'}}}}
    if event == 'order.paid':
        ev['payload']['order'] = {'entity': {'id': order_id, 'amount': amount, 'amount_paid': amount, 'currency': 'INR', 'status': 'paid'}}
    return json.dumps(ev).encode()

class Signatures(unittest.TestCase):
    def test_checkout_signature(self):
        good = sig('order_ABC|pay_XYZ', KEY_SECRET)
        self.assertTrue(billing.verify_payment('order_ABC', 'pay_XYZ', good, KEY_SECRET))
        self.assertFalse(billing.verify_payment('order_ABC', 'pay_OTHER', good, KEY_SECRET))
        self.assertFalse(billing.verify_payment('order_ABC', 'pay_XYZ', good, 'wrong_secret'))
        self.assertFalse(billing.verify_payment('order_ABC', 'pay_XYZ', '', KEY_SECRET))
        self.assertFalse(billing.verify_payment('order_ABC', 'pay_XYZ', good, ''))     # no key set -> never valid

    def test_webhook_signature(self):
        body = webhook_body('payment.captured')
        good = sig(body, WEBHOOK_SECRET)
        self.assertTrue(billing.verify_webhook(body, good, WEBHOOK_SECRET))
        self.assertFalse(billing.verify_webhook(body + b' ', good, WEBHOOK_SECRET))     # body changed
        self.assertFalse(billing.verify_webhook(body, good, 'other'))
        self.assertFalse(billing.verify_webhook(body, None, WEBHOOK_SECRET))

    def test_parse_webhook(self):
        a = billing.parse_webhook(json.loads(webhook_body('payment.captured')))
        b = billing.parse_webhook(json.loads(webhook_body('order.paid')))
        for x in (a, b):
            self.assertEqual((x['order_id'], x['payment_id'], x['amount'], x['currency']), ('order_TEST1', 'pay_TEST1', 49900, 'INR'))
        self.assertIsNone(billing.parse_webhook({'event': 'payment.failed', 'payload': {}}))
        self.assertIsNone(billing.parse_webhook({'event': 'refund.created'}))

class Entitlement(unittest.TestCase):
    def prof(self, **kw):
        p = {'id': 'u1', 'plan': 'free', 'plan_expires_at': None, 'minutes_left': 0, 'bonus_minutes': 0, 'period_resets_at': None}
        p.update(kw); return p

    def test_buy_pro_from_free(self):
        s = billing.apply_purchase(self.prof(), P['pro'], P, T0)
        self.assertEqual(s['plan'], 'pro'); self.assertEqual(s['minutes_left'], 200)
        self.assertEqual(billing.parse(s['plan_expires_at']), T0 + dt.timedelta(days=30))
        self.assertIsNone(s['period_resets_at'])

    def test_pack_adds_minutes_without_date_change(self):
        exp = billing.iso(T0 + dt.timedelta(days=10))
        s = billing.apply_purchase(self.prof(plan='starter', plan_expires_at=exp, minutes_left=20), P['pack'], P, T0)
        self.assertEqual((s['plan'], s['plan_expires_at'], s['minutes_left'], s['bonus_minutes']), ('starter', exp, 20, 15))
        s2 = billing.apply_purchase(self.prof(), P['pack'], P, T0)                         # free user + pack
        self.assertEqual((s2['plan'], s2['bonus_minutes']), ('free', 15))

    def test_renew_same_pass_extends(self):
        exp = T0 + dt.timedelta(days=5)
        s = billing.apply_purchase(self.prof(plan='starter', plan_expires_at=billing.iso(exp), minutes_left=7), P['starter'], P, T0)
        self.assertEqual(billing.parse(s['plan_expires_at']), exp + dt.timedelta(days=30)); self.assertEqual(s['minutes_left'], 67)

    def test_switch_pass_keeps_unused_minutes(self):
        exp = billing.iso(T0 + dt.timedelta(days=5))
        s = billing.apply_purchase(self.prof(plan='starter', plan_expires_at=exp, minutes_left=12, bonus_minutes=3), P['creator'], P, T0)
        self.assertEqual((s['plan'], s['minutes_left'], s['bonus_minutes']), ('creator', 600, 15))

    def test_yearly_monthly_refill_and_expiry(self):
        s = billing.apply_purchase(self.prof(), P['pro_yearly'], P, T0)
        self.assertEqual(billing.parse(s['period_resets_at']), T0 + dt.timedelta(days=30))
        s['minutes_left'] = 3
        plan, s2, changed = billing.refresh({'id': 'u1', **s}, P, T0 + dt.timedelta(days=31))
        self.assertTrue(changed); self.assertEqual((plan['id'], s2['minutes_left']), ('pro_yearly', 200))
        self.assertEqual(billing.parse(s2['period_resets_at']), T0 + dt.timedelta(days=60))
        plan, s3, changed = billing.refresh({'id': 'u1', **s}, P, T0 + dt.timedelta(days=366))
        self.assertTrue(changed); self.assertEqual((plan['id'], s3['minutes_left'], s3['plan_expires_at']), ('free', 0, None))

    def test_expired_pass_goes_free_but_keeps_pack_minutes(self):
        p = self.prof(plan='pro', plan_expires_at=billing.iso(T0 - dt.timedelta(seconds=1)), minutes_left=50, bonus_minutes=9)
        plan, s, changed = billing.refresh(p, P, T0)
        self.assertEqual((plan['id'], s['minutes_left'], s['bonus_minutes'], changed), ('free', 0, 9, True))
        plan, s, changed = billing.refresh(self.prof(plan='pro', plan_expires_at=billing.iso(T0 + dt.timedelta(days=1)), minutes_left=50), P, T0)
        self.assertEqual((plan['id'], changed), ('pro', False))

    def test_costs_and_spend(self):
        self.assertEqual(billing.job_cost(P['pro'], 5, 'hi,en'), 10)
        self.assertEqual(billing.job_cost(P['pro'], 5, 'hi'), 5)
        self.assertEqual(billing.job_cost(P['creator'], 5, 'hi,en'), 5)                 # Hindi+English together
        self.assertEqual(billing.split_spend(4, 10, 6), (4, 2))
        self.assertEqual(billing.split_spend(10, 10, 6), (6, 0))
        self.assertIsNone(billing.split_spend(2, 1, 6))

    def test_parse_supabase_timestamps(self):
        self.assertEqual(billing.parse('2026-12-20T12:00:00.123456+00:00'), dt.datetime(2026, 12, 20, 12, 0, 0, 123456, tzinfo=dt.timezone.utc))
        self.assertEqual(billing.parse('2026-12-20T12:00:00.1+00:00').microsecond, 100000)
        self.assertEqual(billing.parse('2026-12-20T12:00:00Z').tzinfo, dt.timezone.utc)

    def test_owner_blank_fields_are_safe(self):
        r = billing.normalize({'id': 'x', 'price_paise': None, 'styles': [], 'resolution': None, 'active': None})
        self.assertEqual((r['price_paise'], r['styles'], r['resolution'], r['active']), (0, None, 720, True))

class Webhook(unittest.TestCase):
    """app.handle_webhook with a fake in-memory database that has commit_activation()'s semantics."""
    def setUp(self):
        import accounts, app
        self.app, self.acc = app, accounts
        billing.WEBHOOK_SECRET = WEBHOOK_SECRET
        self.order = {'id': 'o1', 'user_id': 'u1', 'plan_id': 'pro', 'amount_paise': 49900, 'status': 'created', 'razorpay_order_id': 'order_TEST1'}
        self.profile = {'id': 'u1', 'plan': 'free', 'plan_expires_at': None, 'minutes_left': 0, 'bonus_minutes': 0, 'period_resets_at': None}
        self.calls = 0
        def activate(order, payment_id, via):            # mirrors accounts.activate + SQL commit_activation
            self.calls += 1
            if self.order['status'] == 'paid': return 'already'
            self.profile.update(billing.apply_purchase(self.profile, P[order['plan_id']], P))
            self.order.update(status='paid', razorpay_payment_id=payment_id, paid_via=via); return 'ok'
        self._orig = (accounts.order_by_rzp, accounts.activate, accounts.order_set, accounts.db_ready)
        accounts.db_ready = lambda: True                  # tables exist in this fake database
        accounts.order_by_rzp = lambda rid: dict(self.order) if rid == self.order['razorpay_order_id'] else None
        accounts.activate = activate
        accounts.order_set = lambda oid, **f: self.order.update(f)

    def tearDown(self):
        self.acc.order_by_rzp, self.acc.activate, self.acc.order_set, self.acc.db_ready = self._orig

    def send(self, body, secret=WEBHOOK_SECRET): return self.app.handle_webhook(body, sig(body, secret))

    def test_duplicate_webhooks_activate_once(self):
        b1 = webhook_body('payment.captured'); b2 = webhook_body('order.paid')
        self.assertEqual(self.send(b1), (200, {'ok': True, 'result': 'ok'}))
        self.assertEqual(self.send(b1), (200, {'ok': True, 'result': 'already'}))      # Razorpay retry
        self.assertEqual(self.send(b2), (200, {'ok': True, 'result': 'already'}))      # second event, same order
        self.assertEqual((self.profile['plan'], self.profile['minutes_left']), ('pro', 200))
        self.assertEqual(self.order['paid_via'], 'webhook')

    def test_bad_signature_rejected(self):
        b = webhook_body('payment.captured')
        self.assertEqual(self.send(b, 'attacker')[0], 400); self.assertEqual(self.calls, 0)

    def test_amount_mismatch_ignored(self):
        code, out = self.send(webhook_body('payment.captured', amount=100))
        self.assertEqual((code, out['ignored']), (200, 'amount mismatch')); self.assertEqual(self.profile['plan'], 'free')

    def test_unknown_order_and_other_events(self):
        self.assertEqual(self.send(webhook_body('payment.captured', order_id='order_OTHER'))[1]['ignored'], 'unknown order')
        self.assertEqual(self.send(json.dumps({'event': 'payment.failed', 'payload': {}}).encode())[1]['ignored'], 'payment.failed')

    def test_tables_missing_webhook_retried_later(self):
        self.acc.db_ready = lambda: False                 # Phase 3 SQL not run yet -> 503 so Razorpay retries
        self.assertEqual(self.send(webhook_body('payment.captured'))[0], 503); self.assertEqual(self.calls, 0)

    def test_no_secret_configured(self):
        billing.WEBHOOK_SECRET = ''
        self.assertEqual(self.send(webhook_body('payment.captured'))[0], 503)

if __name__ == '__main__': unittest.main()
