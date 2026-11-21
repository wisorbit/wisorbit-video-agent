# WisOrbit AI Video Agent (v0.3)
Topic -> research (Wikipedia) -> script (AI) -> fact-check (every number/date must appear in research) -> Hindi translation -> neural voice per scene -> visuals (real photos first, AI images second) -> scene cut to voice length -> subtitles, brand, progress bar, credits -> final MP4.

## Free providers, automatic fallback
- Brain: Gemini (free key, best) -> Pollinations text (no key)
- Voice: Microsoft Edge neural voices -> Google gTTS
- Visuals: Hugging Face FLUX (free token, no watermark) -> Wikimedia Commons real photos -> plain branded card. Watermarked sources are off unless ALLOW_WATERMARK=1.

## Keys (Space -> Settings -> Variables and secrets)
- GEMINI_API_KEY  (free: aistudio.google.com/apikey)
- HF_TOKEN        (free: huggingface.co/settings/tokens, Read)

## Run
Local: pip install -r requirements.txt && uvicorn app:app --port 7860
Free hosting: create a Docker Space on huggingface.co, upload these files.
Link from wisorbit.com: add a button pointing to the Space URL.

## Limits (honest)
- Free CPU renders roughly 1 min of video in 2-3 min; a 10-min video can take 20-30 min.
- Visuals are photos/AI stills with camera motion, not AI motion video (free AI video quotas are too small for 10-min videos).
- Free AI brains can make mistakes; the agent blocks unverified numbers, but review before publishing.


## Deploy on Render (free)
New > Web Service > this repo; Render reads render.yaml (Docker, free plan). Add GEMINI_API_KEY and HF_TOKEN as environment variables in the Render dashboard. Never commit keys.

## Accounts (Phase 1, Supabase)
- Login: Supabase Auth email magic link / OTP (supabase-js in `static/index.html`). "Continue with Google" appears only when env `GOOGLE_AUTH_ENABLED=1` (enable the Google provider in Supabase first).
- Env vars: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY` (server only, never commit), optional `GOOGLE_AUTH_ENABLED`.
- Schema: `supabase/schema.sql` (profiles, videos with RLS, private `videos` bucket).
- Limits: Supabase table `plans` (Phase 3, below); built-in fallback `billing.DEFAULT_PLANS` (free = 2 videos/day, 1 min max). Stored videos are deleted after 7 days.
- API: `GET /api/config`, `GET /api/me`, `GET /api/videos`, `POST /api/generate` (Bearer token required), `GET /api/status/{id}`, `GET /api/file/{id}/{name}`.

## Job queue + workers (Phase 2)
- Every request becomes a row in Supabase table `jobs` (`supabase/schema.sql`). The website only accepts jobs; **workers** render them.
- A worker atomically takes the next job with `claim_job()` (`FOR UPDATE SKIP LOCKED`, highest `priority` then oldest), sends a heartbeat every 30 s, and uploads the MP4s to Storage. If a worker dies, `reap_stale_jobs()` puts its job back in line (max 2 tries).
- Users see "लाइन में आपका नंबर: N / Your place in line" and an estimated wait (from the median render speed of recent jobs and the number of live workers). Queued jobs can be cancelled.
- Limits unchanged: free = 2 videos/day, 1 min, one active job per user (also enforced by a unique index). `jobs.plan` / `jobs.priority` come from the user's plan (table `plans`).
- Run a worker on its own: `python worker.py`, or Docker with `-e WORKER_ONLY=1`. One worker renders one video at a time; run more containers for more parallel videos.
- Render web service: `WORKER_IN_WEB=1` (default) keeps one in-process worker. Set `WORKER_IN_WEB=0` once the Oracle workers are running.
- Status: `GET /api/health` → queued / running / live workers. API: `POST /api/generate`, `GET /api/job/{id}`, `POST /api/job/{id}/cancel`, `GET /api/me` (includes `active_job`).

## Oracle Cloud Always Free worker — आसान स्टेप्स (Hindi)
1. **VM बनाइए:** cloud.oracle.com → Compute → Instances → *Create instance*.
   - Image: **Ubuntu 22.04 या 24.04** · Shape: **Ampere (VM.Standard.A1.Flex)**, 4 OCPU + 24 GB RAM (Always Free सीमा के अंदर)। Ampere न मिले ("out of capacity") तो 2 OCPU/12 GB आज़माइए या कुछ घंटे बाद फिर।
   - *Add SSH keys* → "Generate a key pair" → **private key डाउनलोड** करके संभाल लीजिए।
   - Create दबाइए, 2-3 मिनट में **Public IP** दिखेगा। (कोई port खोलने की ज़रूरत नहीं — worker सिर्फ़ बाहर Supabase से बात करता है।)
2. **VM में जाइए:** Oracle Console में instance खोलकर **Cloud Shell** या अपने कंप्यूटर से:
   `ssh -i <private-key-file> ubuntu@<Public-IP>`
3. **एक कमांड चलाइए:**
   ```
   curl -fsSL https://raw.githubusercontent.com/wisorbit/wisorbit-video-agent/main/deploy/oracle/setup.sh -o setup.sh && sudo bash setup.sh
   ```
   यह पूछेगा: Supabase URL (Enter दबाइए), **Supabase secret key** (Supabase → Project Settings → API Keys → *secret* key; टाइप/पेस्ट करते समय दिखेगी नहीं), Gemini key और HF token (हों तो, वरना Enter), और workers की संख्या (4 OCPU पर 2)।
4. **जाँचिए:** https://video.wisorbit.com/api/health खोलें — `"workers"` बढ़ जाने चाहिए।
5. **Render पर:** Render dashboard → wisorbit-video-agent → Environment → `WORKER_IN_WEB` = `0` → Save (वेबसाइट अब सिर्फ़ jobs लेगी, वीडियो Oracle बनाएगा)।
6. **अपडेट:** नया कोड आने पर VM में फिर `sudo bash setup.sh` चलाइए। Logs: `sudo docker logs -f wisorbit-worker-1`।

## प्लान और पेमेंट (Phase 3) — हिंदी
सभी पेड प्लान **प्रीपेड पास** हैं (एक बार भुगतान, कोई auto-renewal नहीं)। कीमतें कोड में नहीं, Supabase टेबल `plans` में हैं।
पेज: `/plans` (प्लान + Razorpay बटन), `/terms`, `/privacy`, `/refund`, `/shipping`, `/contact` (संपर्क: सिर्फ़ ईमेल pkdnalanda@gmail.com)।
API: `GET /api/plans`, `POST /api/pay/order`, `POST /api/pay/verify`, `POST /api/razorpay/webhook`।

### 1. SQL चलाएँ (एक बार)
Supabase → SQL Editor → `supabase/migrations/phase3_plans_payments.sql` पूरी पेस्ट करें → Run। दोबारा चलाना सुरक्षित है।
जब तक यह नहीं चलता: साइट डिफ़ॉल्ट प्लान दिखाती है, पेमेंट बटन पर "जल्द आ रहा है / Coming soon", और वीडियो बनना पहले जैसा चलता है।

### 2. कीमत बदलना
Supabase → Table Editor → `plans` → लाइन बदलें → Save। `price_paise` पैसे में है: **₹199 = 19900**, ₹499 = 49900, ₹1,299 = 129900, ₹49 = 4900, ₹4,990 = 499000।
बदलाव **60 सेकंड के अंदर** साइट पर दिखता है। `id` कभी न बदलें। `active = false` से प्लान छुप जाता है।

### 3. पेमेंट चालू करना (मालिक)
1. Razorpay → **Test Mode** → API Keys → Test key बनाएँ।
2. Render → Environment: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (अपनी रैंडम लाइन) → Save।
3. Razorpay → Webhooks → URL `https://video.wisorbit.com/api/razorpay/webhook`, वही secret, events **payment.captured** + **order.paid**।
4. `/plans` पर टेस्ट खरीद: UPI `success@razorpay` (फेल टेस्ट: `failure@razorpay`)। प्लान, मिनट, expiry होमपेज पर दिखें।
5. KYC/वेबसाइट रिव्यू के बाद **Live keys** से Render की तीनों वैल्यू बदलें और Live Mode में webhook दोबारा बनाएँ।

कोई key कभी commit/print न करें। टेस्ट सिर्फ़ नकली keys से (`tests/`)।
