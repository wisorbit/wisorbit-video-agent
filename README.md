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
