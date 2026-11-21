# HANDOFF — WisOrbit AI Video Agent (हिंदी)

यह फ़ाइल किसी भी डेवलपर या कोडिंग एजेंट (जैसे Gemini CLI) के लिए है जो इस काम को आगे बढ़ाएगा।
अंतिम अपडेट: 22 नवंबर 2026.

## लक्ष्य
विषय (topic) → रिसर्च → स्क्रिप्ट → 10 मिनट तक का वीडियो: सिंक आवाज़, दृश्य, विज़ुअल।
स्टाइल: educational / cinematic / realistic / YouTube। भाषा: हिंदी और English।
नियम: पहले free providers, फेल होने पर अपने-आप दूसरा provider। कोई watermark नहीं। मोबाइल-फ्रेंडली UI।
कोटा से बचने के लिए कई अकाउंट न बनाएँ। Google Flow को automate न करें।

## कहाँ क्या है
- कोड: https://github.com/wisorbit/wisorbit-video-agent (branch `main`)
- लाइव साइट: https://video.wisorbit.com (Render, Docker, Free, Oregon; `main` पर push से auto-deploy)
- होमपेज: wisorbit.com (Cloudflare Pages, direct upload, Git से जुड़ा नहीं; बदलाव = पूरी साइट का zip अपलोड)
- डेटाबेस/लॉगिन/स्टोरेज: Supabase प्रोजेक्ट `crmaheqpldxxehbilwyg` (Mumbai)
- Render, Cloudflare, Supabase में लॉगिन मालिक के GitHub/Google अकाउंट से होता है।

## चाबियाँ (कभी भी repo में commit न करें)
Render → Environment में सेट हैं: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`।
अभी बाकी: `GEMINI_API_KEY` (aistudio.google.com/apikey), `HF_TOKEN`।
ऐच्छिक: `GOOGLE_AUTH_ENABLED=1` (Google लॉगिन), `WORKER_IN_WEB` (डिफ़ॉल्ट 1)।
पेमेंट (Phase 3, अभी सेट नहीं): `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` — तीनों न हों तो साइट "जल्द आ रहा है / Coming soon" दिखाती है।

## फ़ाइलें
- `app.py` — FastAPI वेब ऐप और API
- `accounts.py` — Supabase लॉगिन, प्लान/मिनट (entitlement), कोटा, वीडियो हिस्ट्री, orders
- `billing.py` — प्लान का हिसाब (मिनट, expiry, pack), Razorpay order/signature/webhook (शुद्ध logic, टेस्टेड)
- `agent.py` — रिसर्च + स्क्रिप्ट (free AI, fallback)
- `engine.py` — आवाज़, दृश्य, रेंडर (ffmpeg); `tts_edge.py` — आवाज़
- `worker.py` — अलग render worker (`python worker.py` या Docker में `WORKER_ONLY=1`)
- `static/index.html` — UI (अकाउंट में प्लान, बचे मिनट, expiry; प्लान में न होने वाले स्टाइल पर 🔒)
- `static/plans.html` — प्लान पेज (`/plans`), कीमतें `GET /api/plans` से, Razorpay Checkout बटन
- `static/terms.html`, `privacy.html`, `refund.html`, `shipping.html`, `contact.html` — नीतियाँ (Razorpay वेबसाइट रिव्यू के लिए; `/terms` आदि पर खुलती हैं)। संपर्क सिर्फ़ ईमेल pkdnalanda@gmail.com — **फ़ोन नंबर या पूरा पता कहीं न छापें** (मालिक का निर्णय)। पेज `static/pages.css` + `static/lang.js` (हिंदी/English बटन) शेयर करते हैं।
- `supabase/migrations/phase3_plans_payments.sql` — Phase 3 टेबल (plans, orders, profile के नए कॉलम, `commit_activation()`); यही हिस्सा `schema.sql` में भी है
- `tests/` — `test_billing.py` (unit) और `test_local_db.py` (लोकल Postgres+PostgREST पर पूरा पेमेंट फ़्लो, नकली keys)
- `supabase/schema.sql` — टेबल, RLS, grants, queue functions (नए प्रोजेक्ट पर पूरा चलाएँ)
- `deploy/oracle/setup.sh` — Oracle VM पर worker चलाने की स्क्रिप्ट (README में हिंदी स्टेप्स)

## अब तक क्या हो चुका है
- ईमेल लिंक से लॉगिन, "मेरी वीडियो" (7 दिन बाद डिलीट)
- Free कोटा: रोज़ 2 वीडियो, 1 मिनट तक, रात 12 बजे (IST) रीसेट; एक समय में एक job
- Job queue: लाइन में नंबर + अनुमानित समय, cancel, worker मरे तो job वापस लाइन में
- वीडियो की लंबाई ±10% के अंदर; हिंदी अनुवाद की जाँच (back-translation, Google fallback)
- Render free पर ~18 मिनट में 30 सेकंड का वीडियो (धीमा)
- Phase 3 (कोड तैयार, पेमेंट अभी बंद): प्रीपेड पास (कोई auto-renewal नहीं) — Free, Starter ₹199/30 दिन, Pro ₹499, Creator ₹1,299, 15 मिनट पैक ₹49, Pro सालाना ₹4,990। मिनट = लंबाई × भाषाएँ (Creator में हिंदी+English एक बार), फेल/रद्द वीडियो के मिनट अपने-आप वापस, पेड प्लान 1080p + जल्दी नंबर। पेमेंट दो रास्तों से पक्का होता है (checkout verify + webhook), दोनों बार भी आए तो प्लान एक ही बार जुड़ता है।
- Phase 3 SQL लाइव DB पर **अभी नहीं चला** — तब तक कोड अपने-आप डिफ़ॉल्ट प्लान दिखाता है, `payments_enabled:false`, और job में नए कॉलम नहीं लिखता (सुरक्षित)।

## अगले कदम (क्रम से)
1. Oracle Always Free VM पर worker चलाना (`deploy/oracle/setup.sh`), फिर Render पर `WORKER_IN_WEB=0`
2. Supabase free में एक फ़ाइल की सीमा 50 MB — 10 मिनट वीडियो के लिए bitrate कम करें या दूसरा storage
3. लॉगिन ईमेल के लिए free SMTP (Brevo/Resend) + हिंदी ईमेल टेम्पलेट
4. Google लॉगिन (Google Cloud OAuth client → Supabase provider → `GOOGLE_AUTH_ENABLED=1`)
5. Payments चालू करना — नीचे "मालिक के लिए: पेमेंट चालू करने के स्टेप" देखें
6. असली फ़ोन ब्राउज़र में लॉगिन/UI जाँच; free script में तथ्य-गलतियाँ कम करना

## काम करने का तरीका
- हर बदलाव लोकल टेस्ट करके `main` पर push करें; Render अपने-आप deploy करता है।
- `/api/health` से workers और स्थिति देखें।
- कोई secret लॉग/प्रिंट/commit न करें।

## मालिक के लिए: पेमेंट चालू करने के स्टेप (क्रम से)
1. **SQL चलाएँ (एक बार):** Supabase → प्रोजेक्ट `crmaheqpldxxehbilwyg` → SQL Editor → New query →
   GitHub से `supabase/migrations/phase3_plans_payments.sql` की पूरी सामग्री पेस्ट करें → **Run**।
   दोबारा चलाना सुरक्षित है (आपकी बदली कीमतें नहीं बदलतीं)। जाँच: Table Editor में `plans` टेबल में 6 लाइनें दिखें।
2. **Razorpay Test keys:** dashboard.razorpay.com → ऊपर **Test Mode** → Account & Settings → API Keys → Generate Test Key।
3. **Render में डालें:** Render → wisorbit-video-agent → Environment → जोड़ें:
   `RAZORPAY_KEY_ID` (rzp_test_…), `RAZORPAY_KEY_SECRET`, और `RAZORPAY_WEBHOOK_SECRET` (अपनी बनाई कोई लंबी रैंडम लाइन) → Save (साइट अपने-आप restart होगी)।
4. **Webhook:** Razorpay (Test Mode) → Account & Settings → Webhooks → Add New Webhook →
   URL `https://video.wisorbit.com/api/razorpay/webhook`, Secret = वही `RAZORPAY_WEBHOOK_SECRET`,
   Events: **payment.captured** और **order.paid** → Create।
5. **टेस्ट पेमेंट:** https://video.wisorbit.com/plans → लॉगिन → कोई प्लान → Buy → UPI में `success@razorpay` डालें
   (असफल टेस्ट के लिए `failure@razorpay`)। सफल होने पर पेज और होमपेज पर प्लान, बचे मिनट और expiry दिखनी चाहिए।
   Supabase → `orders` टेबल में status `paid` दिखेगा।
6. **Live:** Razorpay KYC और वेबसाइट रिव्यू पास होने के बाद Live Mode में नई keys बनाएँ, Render में तीनों वैल्यू बदलें,
   और Live Mode में भी वही webhook (नए secret के साथ) बनाएँ।

## कीमत या लिमिट कैसे बदलें (बिना कोड)
Supabase → Table Editor → `plans` टेबल → लाइन पर डबल-क्लिक करके बदलें → Save।
- `price_paise` = पैसे में कीमत: ₹199 = `19900`, ₹499 = `49900`, ₹4,990 = `499000`।
- `minutes_quota` (मिनट), `max_video_minutes`, `period_days` (30/365), `daily_videos` (Free), `resolution` (720/1080),
  `styles` (खाली = सभी स्टाइल), `active` (false = प्लान छुप जाएगा), `sort` (क्रम), `name_hi/en`, `desc_hi/en`।
- बदलाव वेबसाइट पर **60 सेकंड के अंदर** दिखता है। पहले से खरीदे पास पर असर नहीं पड़ता।
- `id` न बदलें (खरीदे गए प्लान इसी से जुड़े हैं)।

## टेस्ट चलाना
- Unit: `python -m unittest tests.test_billing -v`
- पूरा फ़्लो (लोकल DB, कभी लाइव DB नहीं): Postgres (port 5544, socket /tmp) में Supabase के छोटे stubs (auth.users, storage, roles) + `supabase/schema.sql` लोड करें,
  PostgREST को `db-anon-role=service_role` के साथ 127.0.0.1:3555 पर चलाएँ, फिर
  `NO_PROXY=127.0.0.1 LOCAL_REST=http://127.0.0.1:3555 LOCAL_PSQL="psql -h /tmp -p 5544 -U postgres" python -m unittest discover -s tests -v`
