"""Integration test against a LOCAL Postgres + PostgREST loaded with supabase/schema.sql (never the live DB).
Skipped unless LOCAL_REST (PostgREST url) and LOCAL_PSQL (psql command) are set, e.g.
  NO_PROXY=127.0.0.1 LOCAL_REST=http://127.0.0.1:3555 LOCAL_PSQL="psql -h /tmp -p 5544 -U postgres" python -m unittest tests.test_local_db -v
Fake Razorpay keys only; billing.create_order is replaced, so nothing reaches Razorpay."""
import os, sys, json, hmac, hashlib, uuid, shlex, subprocess, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REST, PSQL = os.environ.get('LOCAL_REST'), os.environ.get('LOCAL_PSQL')
os.environ.setdefault('SUPABASE_URL', 'http://fake.local'); os.environ.setdefault('SUPABASE_SECRET_KEY', 'fake')
os.environ.setdefault('SUPABASE_PUBLISHABLE_KEY', 'fake')

def psql(sql):
    return subprocess.run(shlex.split(PSQL) + ['-qAtc', sql], capture_output=True, text=True, check=True).stdout.strip()

@unittest.skipUnless(REST and PSQL, 'local PostgREST not configured')
class LocalDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import accounts, billing, app
        from fastapi.testclient import TestClient
        accounts.REST = REST
        accounts.plans(force=True)                        # drop any plans cache from other test modules
        billing.KEY_ID, billing.KEY_SECRET, billing.WEBHOOK_SECRET = 'rzp_test_FAKE', 'fake_key_secret', 'fake_webhook_secret'
        cls.acc, cls.bill, cls.app = accounts, billing, app
        cls.client = TestClient(app.app)
        cls.uid = str(uuid.uuid4())
        psql(f"insert into auth.users(id,email) values ('{cls.uid}','t@example.com')")
        accounts.get_user = lambda tok: {'id': cls.uid, 'email': 't@example.com'} if tok == 'tok' else None
        cls.n = 0
        def fake_order(amount, receipt, notes):
            cls.n += 1; return {'id': f'order_FAKE{cls.n}_{cls.uid[:6]}', 'amount': amount}
        billing.create_order = fake_order
        cls.H = {'Authorization': 'Bearer tok'}

    def me(self): return self.client.get('/api/me', headers=self.H).json()
    def buy(self, plan):
        r = self.client.post('/api/pay/order', json={'plan': plan}, headers=self.H); self.assertEqual(r.status_code, 200, r.text)
        return r.json()
    def sig(self, msg, secret): return hmac.new(secret.encode(), msg if isinstance(msg, bytes) else msg.encode(), hashlib.sha256).hexdigest()

    def test_1_plans_endpoint_from_db_and_price_edit(self):
        d = self.client.get('/api/plans').json()
        self.assertTrue(d['payments_enabled']); self.assertEqual(d['key_id'], 'rzp_test_FAKE')
        self.assertEqual([p['id'] for p in d['plans']], ['free', 'starter', 'pro', 'creator', 'pack', 'pro_yearly'])
        psql("update plans set price_paise=29900 where id='starter'")       # owner edits a price
        self.acc.plans(force=True)
        self.assertEqual(self.acc.plans()['starter']['price_paise'], 29900)
        psql("update plans set price_paise=19900 where id='starter'"); self.acc.plans(force=True)

    def test_2_free_user_limits(self):
        m = self.me(); self.assertEqual((m['plan'], m['max_minutes'], m['resolution']), ('free', 1, 720))
        ok, msg, p, ctx = self.acc.check_quota({'id': self.uid}, 3, 'educational', 'hi')
        self.assertFalse(ok); self.assertIn('/plans', msg)
        ok, msg, p, ctx = self.acc.check_quota({'id': self.uid}, 1, 'cinematic', 'hi')
        self.assertFalse(ok)                                                  # style not in free plan

    def test_3_verify_flow_and_duplicates(self):
        o = self.buy('pro')
        self.assertEqual(o['amount'], 49900)                                  # amount from DB
        pay = 'pay_FAKE_A'
        good = self.sig(f"{o['order_id']}|{pay}", 'fake_key_secret')
        bad = self.client.post('/api/pay/verify', headers=self.H, json={'razorpay_order_id': o['order_id'], 'razorpay_payment_id': pay, 'razorpay_signature': 'x' * 64})
        self.assertEqual(bad.status_code, 400)
        r = self.client.post('/api/pay/verify', headers=self.H, json={'razorpay_order_id': o['order_id'], 'razorpay_payment_id': pay, 'razorpay_signature': good})
        self.assertEqual(r.status_code, 200, r.text); d = r.json()
        self.assertEqual((d['result'], d['plan'], d['minutes_left'], d['resolution'], d['max_minutes']), ('ok', 'pro', 200, 1080, 10))
        # webhook for the same payment arrives afterwards, twice -> no second activation
        body = json.dumps({'event': 'payment.captured', 'payload': {'payment': {'entity': {'id': pay, 'order_id': o['order_id'], 'amount': 49900, 'currency': 'INR'}}}}).encode()
        for _ in range(2):
            w = self.client.post('/api/razorpay/webhook', content=body, headers={'X-Razorpay-Signature': self.sig(body, 'fake_webhook_secret')})
            self.assertEqual((w.status_code, w.json()['result']), (200, 'already'))
        self.assertEqual(self.me()['minutes_left'], 200)
        self.assertEqual(psql(f"select status||' '||paid_via||' '||razorpay_payment_id from orders where razorpay_order_id='{o['order_id']}'"), f'paid verify {pay}')
        bw = self.client.post('/api/razorpay/webhook', content=body, headers={'X-Razorpay-Signature': 'bad'})
        self.assertEqual(bw.status_code, 400)

    def test_4_webhook_only_pack_and_parallel_duplicates(self):
        o = self.buy('pack')
        body = json.dumps({'event': 'order.paid', 'payload': {
            'order': {'entity': {'id': o['order_id'], 'amount_paid': 4900, 'currency': 'INR'}},
            'payment': {'entity': {'id': 'pay_FAKE_B', 'order_id': o['order_id'], 'amount': 4900, 'currency': 'INR'}}}}).encode()
        h = {'X-Razorpay-Signature': self.sig(body, 'fake_webhook_secret')}
        res = []
        ths = [threading.Thread(target=lambda: res.append(self.client.post('/api/razorpay/webhook', content=body, headers=h).json()['result'])) for _ in range(4)]
        [t.start() for t in ths]; [t.join() for t in ths]
        self.assertEqual(sorted(res), ['already', 'already', 'already', 'ok'])
        m = self.me(); self.assertEqual((m['plan'], m['minutes_left'], m['bonus_minutes']), ('pro', 200, 15))

    def test_5_spend_cancel_refund(self):
        r = self.client.post('/api/generate', headers=self.H, json={'topic': 'Test', 'minutes': 5, 'style': 'cinematic', 'langs': 'hi,en'})
        self.assertEqual(r.status_code, 200, r.text); self.assertEqual(r.json()['cost_minutes'], 10)
        jid = r.json()['id']
        self.assertEqual(psql(f"select priority||' '||resolution||' '||cost_minutes from jobs where id='{jid}'"), '10 1080 10.0')
        self.assertEqual(self.me()['minutes_left'], 190)
        c = self.client.post(f'/api/job/{jid}/cancel', headers=self.H); self.assertEqual(c.status_code, 200)
        self.assertEqual(self.me()['minutes_left'], 200)
        self.assertFalse(self.acc.refund_job(self.acc.get_job(jid)))          # second refund refused
        self.assertEqual(self.me()['minutes_left'], 200)

    def test_6_failed_job_refund_safety_net(self):
        r = self.client.post('/api/generate', headers=self.H, json={'topic': 'Test2', 'minutes': 2, 'style': 'educational', 'langs': 'en'})
        jid = r.json()['id']; self.assertEqual(self.me()['minutes_left'], 198)
        psql(f"update jobs set status='error' where id='{jid}'")               # e.g. reaper gave up
        psql(f"update videos set status='error' where id=(select video_id from jobs where id='{jid}')")
        self.assertEqual(self.acc.refund_failed(), 1); self.assertEqual(self.acc.refund_failed(), 0)
        self.assertEqual(self.me()['minutes_left'], 200)

    def test_7_expiry_back_to_free_keeps_pack(self):
        psql(f"update profiles set plan_expires_at=now()-interval '1 minute' where id='{self.uid}'")
        m = self.me(); self.assertEqual((m['plan'], m['minutes_left'], m['bonus_minutes']), ('free', 0, 15))
        self.assertEqual(m['max_minutes'], 5)                                   # pack limits while pack minutes last
        ok, msg, p, ctx = self.acc.check_quota({'id': self.uid}, 3, 'educational', 'hi')
        self.assertTrue(ok, msg); self.assertEqual((ctx['mode'], ctx['plan'], ctx['cost']), ('minutes', 'pack', 3))

if __name__ == '__main__': unittest.main()
