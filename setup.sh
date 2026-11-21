#!/bin/bash
# One-time setup for the auto-trading bot on the server.
# Run inside the bot folder:   bash setup.sh
cd "$(dirname "$0")" || exit 1
echo ""
echo "=========== Crypto bot setup ==========="
echo "Type each answer and press Enter."
echo ""
read -r -p "1) Mode - type demo or live [demo]: " MODE
MODE=${MODE:-demo}
if [ "$MODE" != "live" ]; then MODE=demo; fi
read -r -p "2) Binance API Key: " API_KEY
read -r -p "3) Binance Secret Key: " API_SECRET
read -r -p "4) Telegram bot token: " TG
echo ""
echo "5) Now open Telegram, send the word  hi  to your bot, then come back and press Enter."
read -r _
CHAT=$(python3 - "$TG" <<'PY'
import sys, json, urllib.request
try:
    d = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{sys.argv[1]}/getUpdates", timeout=20))
    ids = [u.get("message", {}).get("chat", {}).get("id") for u in d.get("result", [])]
    ids = [i for i in ids if i]
    print(ids[-1] if ids else "")
except Exception:
    print("")
PY
)
if [ -z "$CHAT" ]; then
  read -r -p "   Could not find your chat id. Type it here: " CHAT
else
  echo "   Telegram chat id found: $CHAT"
fi
umask 077
cat > secrets.txt <<EOT
MODE=$MODE
API_KEY=$API_KEY
API_SECRET=$API_SECRET
TELEGRAM_BOT_TOKEN=$TG
TELEGRAM_CHAT_ID=$CHAT
EOT
chmod 600 secrets.txt
echo ""
echo "Testing the connection..."
python3 trader.py check || { echo "Test FAILED - see the message above."; exit 1; }
DIR=$(pwd)
( crontab -l 2>/dev/null | grep -v "trader.py" | grep -v "crypto-bot-pull" ;
  echo "3 * * * * cd $DIR && flock -n /tmp/cryptobot.lock python3 trader.py scan >> bot.log 2>&1" ;
  echo "*/5 * * * * cd $DIR && flock -n /tmp/cryptobot.lock python3 trader.py monitor >> bot.log 2>&1" ;
  echo "58 * * * * cd $DIR && git pull -q >> bot.log 2>&1  # crypto-bot-pull" ) | crontab -
echo ""
echo "DONE. Mode: $MODE"
echo "The bot now runs every hour by itself. Results come to Telegram."
echo "Pause new trades:  touch $DIR/PAUSE      Resume:  rm $DIR/PAUSE"
