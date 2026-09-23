# 🧠 Matematika AI Ustozi — Telegram boti

Gemini API asosida ishlaydigan, o'quvchini yo'naltiruvchi (hint tizimi bilan) matematika ustozi.

## Imkoniyatlar

- Sizning **Gem ko'rsatmangiz** (`system_instruction.json`) to'liq qo'llanadi
- Matn va **rasm** (daftardagi masala surati) qabul qiladi
- Har bir foydalanuvchi uchun alohida suhbat xotirasi
- Tugmalar: 💡 Hint · ✅ To'liq yechim · 📝 O'xshash masala · 🔽 Osonroq tushuntir
- `/level` — qiyinlik darajasi (🟢 🔵 🟠 🔴)
- `/reset` — suhbatni tozalash
- Uzun javoblarni bo'lib yuboradi, LaTeX o'rniga oddiy belgilardan foydalanadi
- Spamdan himoya (cooldown) va ixtiyoriy ruxsat ro'yxati

## 1. Kalitlarni olish

1. **Telegram token:** Telegram'da [@BotFather](https://t.me/BotFather) → `/newbot` → nom va username bering → token oling.
2. **Gemini API kalit:** https://aistudio.google.com/apikey → *Create API key*.

## 2. O'rnatish va ishga tushirish

```bash
cd math-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # so'ng .env ichiga token va kalitni yozing
python bot.py
```

Konsolda `Bot ishga tushdi` chiqsa, Telegram'da botingizga `/start` yuboring.

## 3. Doimiy ishlatish (server)

**Docker bilan:**

```bash
docker build -t math-bot .
docker run -d --restart unless-stopped --env-file .env --name math-bot math-bot
```

**Yoki systemd (VPS):** `/etc/systemd/system/math-bot.service`

```ini
[Unit]
Description=Math Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/math-bot
ExecStart=/opt/math-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/opt/math-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now math-bot
```

## Keyingi botlar uchun

Bu kod boshqa botlar uchun ham shablon bo'ladi: yangi botga faqat **yangi token**
va **yangi JSON ko'rsatma** (`SYSTEM_INSTRUCTION_FILE`) kerak.

## Muhim eslatmalar

- `.env` faylini hech kimga bermang va GitHub'ga yuklamang.
- Suhbat tarixi xotirada saqlanadi, bot qayta ishga tushsa tozalanadi. Doimiy saqlash kerak bo'lsa SQLite/Redis qo'shing.
- Gemini API kaliti "location not supported" xatosini bersa, bot ishlayotgan **server** joylashuvi qo'llab-quvvatlanadigan hududda bo'lishi kerak.
- Model nomini `.env` dagi `GEMINI_MODEL` orqali almashtirishingiz mumkin.
