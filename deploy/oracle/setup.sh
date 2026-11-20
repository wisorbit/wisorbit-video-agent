#!/usr/bin/env bash
# WisOrbit AI Video Agent - render workers on an Oracle Cloud "Always Free" Ubuntu VM (ARM / Ampere A1 or x86).
#
# Run on the VM (one command):
#   curl -fsSL https://raw.githubusercontent.com/wisorbit/wisorbit-video-agent/main/deploy/oracle/setup.sh -o setup.sh && sudo bash setup.sh
#
# What it does: installs Docker, clones the repo, asks for the keys (hidden typing), builds the image and
# starts N render workers that restart by themselves (docker --restart=always, also after a VM reboot).
# Run it again any time to update to the latest code or change the number of workers.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/wisorbit/wisorbit-video-agent.git}"
APP_DIR="${APP_DIR:-/opt/wisorbit-video-agent}"
ENV_FILE="/etc/wisorbit-worker.env"
IMAGE="wisorbit-video-agent:latest"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
if [ "$(id -u)" -ne 0 ]; then echo "कृपया sudo के साथ चलाइए / Please run with sudo:  sudo bash setup.sh"; exit 1; fi

say "1/6 Docker इंस्टॉल / Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
fi
apt-get install -y git >/dev/null 2>&1 || true
systemctl enable --now docker
REAL_USER="${SUDO_USER:-ubuntu}"
usermod -aG docker "$REAL_USER" 2>/dev/null || true

say "2/6 Swap (बड़ी वीडियो के लिए) / Swap file for long videos"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "3/6 कोड डाउनलोड / Getting the code"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO_URL" "$APP_DIR"; fi

say "4/6 Keys (.env)"
ask() {  # ask VAR "question" hidden(0/1) default
  local var="$1" q="$2" hide="$3" def="${4:-}" cur val
  cur="$(grep -E "^$var=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  [ -n "$cur" ] && q="$q [Enter = पुरानी रखें / keep current]"
  [ -z "$cur" ] && [ -n "$def" ] && q="$q [Enter = $def]"
  if [ "$hide" = 1 ]; then read -r -s -p "$q: " val; echo; else read -r -p "$q: " val; fi
  [ -z "$val" ] && val="${cur:-$def}"
  printf -v "$var" '%s' "$val"
}
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
ask SUPABASE_URL "Supabase URL" 0 "https://crmaheqpldxxehbilwyg.supabase.co"
ask SUPABASE_SECRET_KEY "Supabase secret key (sb_secret_..., टाइप करते समय दिखेगा नहीं / hidden)" 1
ask GEMINI_API_KEY "Gemini API key (optional, Enter = skip)" 1
ask HF_TOKEN "Hugging Face token (optional, Enter = skip)" 1
CPUS="$(nproc)"; DEF_N=$(( CPUS >= 4 ? 2 : 1 ))
ask WORKERS "कितने workers? / How many workers (1 per 2 CPU cores)" 0 "$DEF_N"
if [ -z "$SUPABASE_SECRET_KEY" ]; then echo "Supabase secret key ज़रूरी है / is required"; exit 1; fi
cat > "$ENV_FILE" <<EOF
SUPABASE_URL=$SUPABASE_URL
SUPABASE_SECRET_KEY=$SUPABASE_SECRET_KEY
GEMINI_API_KEY=$GEMINI_API_KEY
HF_TOKEN=$HF_TOKEN
WORKERS=$WORKERS
WORKER_ONLY=1
WORKER_NAME=oracle-$(hostname)
EOF
chmod 600 "$ENV_FILE"

say "5/6 Docker image बन रही है (5-10 मिनट) / Building the image (5-10 min)"
docker build -t "$IMAGE" "$APP_DIR"

say "6/6 Workers शुरू / Starting $WORKERS worker(s)"
for c in $(docker ps -a --format '{{.Names}}' | grep '^wisorbit-worker-' || true); do docker rm -f "$c" >/dev/null; done
for i in $(seq 1 "$WORKERS"); do
  docker run -d --name "wisorbit-worker-$i" --restart=always --env-file "$ENV_FILE" \
    -e WORKER_NAME="oracle-$(hostname)-$i" --memory=5g --log-opt max-size=10m --log-opt max-file=3 "$IMAGE" >/dev/null
done
sleep 8
docker ps --filter name=wisorbit-worker- --format 'table {{.Names}}\t{{.Status}}'
echo
docker logs --tail 5 wisorbit-worker-1 || true
cat <<'EOF'

✅ हो गया / Done.
- जाँचें / Check:   https://video.wisorbit.com/api/health   ("workers" में ये workers गिने जाएँगे / should count these workers)
- Logs:            sudo docker logs -f wisorbit-worker-1
- Update code:     sudo bash setup.sh   (फिर से चलाइए / run again)
- Stop all:        sudo docker rm -f $(sudo docker ps -aq --filter name=wisorbit-worker-)
Render पर अब WORKER_IN_WEB=0 रख सकते हैं ताकि वेबसाइट सिर्फ़ jobs ले और Oracle वीडियो बनाए।
You can now set WORKER_IN_WEB=0 on Render so the website only takes jobs and Oracle renders them.
EOF
