# ChessWager – Going Live Guide

## Step 1: Get a Server (VPS)

The cheapest reliable options ranked by cost:

| Provider | Plan | Monthly Cost | Best for |
|---|---|---|---|
| **Hetzner** (recommended) | CX22 (2 vCPU, 4 GB RAM) | ~₹700/month | Best price-performance |
| **DigitalOcean** | Basic Droplet (1 GB) | ~₹600/month | Easy setup |
| **Render.com** | Starter | Free → ₹600/month | Easiest (no server management) |
| **Railway.app** | Hobby | ~₹400/month | Very easy, good for starting |

**Easiest for a beginner → Render.com or Railway.app** (no Linux knowledge needed).

---

## Step 2: Get a Domain

- **Namecheap.com** – `.in` domain costs ~₹700/year, `.com` ~₹900/year
- **GoDaddy.com** – similar prices, sometimes has offers
- Example domain: `chesswager.in` or `chesswager.in`

After buying, point your domain's DNS to your server IP.

---

## Step 3: Deploy on Render.com (Easiest Method)

### 3a. Push code to GitHub
```bash
cd D:\chess-api
git init
git add .
git commit -m "Initial ChessWager deployment"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/chess-api.git
git push -u origin main
```

### 3b. Create a Render Web Service
1. Go to [render.com](https://render.com) → New → Web Service
2. Connect your GitHub repo
3. Settings:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add Environment Variables (from `.env.example`):
   - `SECRET_KEY` → generate with: `python -c "import secrets; print(secrets.token_hex(32))"`
   - `ADMIN_SECRET` → choose a strong password
   - `UPI_ID` → your UPI ID (e.g. `yourname@okaxis`)
   - `UPI_NAME` → `ChessWager`
   - `PLATFORM_FEE_PERCENT` → `10`
   - `PAYOUT_HOLD_HOURS` → `24`
5. Click **Deploy** → Render gives you a free `.onrender.com` URL
6. Connect your custom domain in Render dashboard → SSL is automatic (free)

---

## Step 4: Deploy on a VPS (Hetzner/DigitalOcean)

```bash
# 1. SSH into your server
ssh root@YOUR_SERVER_IP

# 2. Install Python and Nginx
apt update && apt install python3 python3-pip nginx certbot python3-certbot-nginx -y

# 3. Copy your files
scp -r D:\chess-api root@YOUR_SERVER_IP:/opt/chesswager

# 4. Install dependencies
cd /opt/chesswager
pip3 install -r requirements.txt

# 5. Set environment variables
cp .env.example .env
nano .env   # fill in your values

# 6. Create a systemd service (runs on startup)
cat > /etc/systemd/system/chesswager.service << EOF
[Unit]
Description=ChessWager API
After=network.target

[Service]
WorkingDirectory=/opt/chesswager
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
User=www-data
EnvironmentFile=/opt/chesswager/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl enable chesswager
systemctl start chesswager

# 7. Configure Nginx reverse proxy
cat > /etc/nginx/sites-available/chesswager << EOF
server {
    listen 80;
    server_name chesswager.in www.chesswager.in;

    client_max_body_size 55M;   # allow video chunk uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";   # WebSocket support
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

ln -s /etc/nginx/sites-available/chesswager /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 8. Free SSL certificate (HTTPS)
certbot --nginx -d chesswager.in -d www.chesswager.in
```

---

## Step 5: Set Up Your UPI ID

1. Open your **Google Pay** or **PhonePe** or **Paytm** app
2. Go to **Profile → UPI ID** → copy your UPI ID (e.g. `yourname@okaxis`)
3. Set it in your `.env` file:
   ```
   UPI_ID=yourname@okaxis
   UPI_NAME=ChessWager
   ```
4. All player deposits will show a QR code that sends money **directly to your UPI ID**
5. You verify each UTR number in the admin dashboard → approve → wallet credited

> ⚠️ **Important**: Keep your phone with you to verify payments. UTR numbers are unique and cannot be reused.

---

## Step 6: Important Production Settings

Edit `.env` on your server:

```env
# MUST change these:
SECRET_KEY=<run: python3 -c "import secrets; print(secrets.token_hex(32))">
ADMIN_SECRET=<choose a strong password, e.g. Chess@Admin2026!>

# Your UPI ID:
UPI_ID=yourname@okaxis
UPI_NAME=ChessWager

# Business settings:
PLATFORM_FEE_PERCENT=10    # you keep 10% of every prize pool
PAYOUT_HOLD_HOURS=24       # 24 hours to review video before paying
MIN_BET=10                 # minimum ₹10
MAX_BET=100                # maximum ₹100

# Use PostgreSQL in production (more reliable than SQLite):
DATABASE_URL=postgresql+asyncpg://user:password@localhost/chesswager
```

---

## Business Model (How You Always Profit)

```
Two players bet ₹50 each
────────────────────────────────
Total prize pool:        ₹100
Platform fee (10%):      ₹10   ← YOU KEEP THIS NO MATTER WHAT
Winner receives:         ₹90

If cheating is found:
  Reject payout →        ₹90 also goes to platform
  Penalty charged →      extra deduction from wallet

Result: You earn ₹10–₹100 per game, zero risk.
```

### Revenue per day (estimate)
| Games/day | Avg bet | Platform 10% | Your daily earnings |
|---|---|---|---|
| 10 games | ₹30 | ₹6/game | ₹60/day |
| 50 games | ₹30 | ₹6/game | ₹300/day |
| 200 games | ₹50 | ₹10/game | ₹2,000/day |

---

## Legal Notes (India)

- Skill-based gaming (chess) is **generally legal** in most Indian states
- States where online gaming with stakes may be restricted: Andhra Pradesh, Telangana, Karnataka (check current laws)
- Consult a lawyer before launch
- Register as a business and maintain proper GST records
- Keep UPI transaction records for tax purposes

---

## Admin Dashboard

After going live, access your admin panel at:
```
https://chesswager.in/admin
```
Password: whatever you set as `ADMIN_SECRET`

Daily routine:
1. **Morning**: Check "UPI Deposits" → verify UTR numbers → approve/reject
2. **Morning**: Check "Payouts" → review video evidence → release or reject
3. **Evening**: Check "Revenue" → see today's earnings
4. **As needed**: Check "Anti-Cheat Flags" → ban cheaters
