# HANDOFF — WisOrbit AI Video Agent (हिंदी)

यह फ़ाइल किसी भी डेवलपर या कोडिंग एजेंट (जैसे Gemini CLI) के लिए है जो इस काम को आगे बढ़ाएगा।
अंतिम अपडेट: 20 नवंबर 2026.

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

## फ़ाइलें
- `app.py` — FastAPI वेब ऐप और API
- `accounts.py` — Supabase लॉगिन, कोटा, वीडियो हिस्ट्री
- `agent.py` — रिसर्च + स्क्रिप्ट (free AI, fallback)
- `engine.py` — आवाज़, दृश्य, रेंडर (ffmpeg); `tts_edge.py` — आवाज़
- `worker.py` — अलग render worker (`python worker.py` या Docker में `WORKER_ONLY=1`)
- `static/index.html` — UI
- `supabase/schema.sql` — टेबल, RLS, grants, queue functions (नए प्रोजेक्ट पर पूरा चलाएँ)
- `deploy/oracle/setup.sh` — Oracle VM पर worker चलाने की स्क्रिप्ट (README में हिंदी स्टेप्स)

## अब तक क्या हो चुका है
- ईमेल लिंक से लॉगिन, "मेरी वीडियो" (7 दिन बाद डिलीट)
- Free कोटा: रोज़ 2 वीडियो, 1 मिनट तक, रात 12 बजे (IST) रीसेट; एक समय में एक job
- Job queue: लाइन में नंबर + अनुमानित समय, cancel, worker मरे तो job वापस लाइन में
- वीडियो की लंबाई ±10% के अंदर; हिंदी अनुवाद की जाँच (back-translation, Google fallback)
- Render free पर ~18 मिनट में 30 सेकंड का वीडियो (धीमा)

## अगले कदम (क्रम से)
1. Oracle Always Free VM पर worker चलाना (`deploy/oracle/setup.sh`), फिर Render पर `WORKER_IN_WEB=0`
2. Supabase free में एक फ़ाइल की सीमा 50 MB — 10 मिनट वीडियो के लिए bitrate कम करें या दूसरा storage
3. लॉगिन ईमेल के लिए free SMTP (Brevo/Resend) + हिंदी ईमेल टेम्पलेट
4. Google लॉगिन (Google Cloud OAuth client → Supabase provider → `GOOGLE_AUTH_ENABLED=1`)
5. Payments: Razorpay UPI (प्लान अभी मालिक ने तय नहीं किए; प्रस्ताव: Free, ₹199, ₹499, ₹1,299, ₹49 पैक)
6. असली फ़ोन ब्राउज़र में लॉगिन/UI जाँच; free script में तथ्य-गलतियाँ कम करना

## काम करने का तरीका
- हर बदलाव लोकल टेस्ट करके `main` पर push करें; Render अपने-आप deploy करता है।
- `/api/health` से workers और स्थिति देखें।
- कोई secret लॉग/प्रिंट/commit न करें।
