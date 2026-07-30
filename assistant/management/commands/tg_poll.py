"""Long-polling режим Telegram-бота (для dev без публичного URL).

Запуск:
    TELEGRAM_BOT_TOKEN=... python manage.py tg_poll
    Ctrl+C — graceful stop.

В проде использовать webhook (см. assistant/tg_views.py) — он эффективнее
и масштабируется без вечно живого процесса.

Что делает:
  • Каждую 1 сек дёргает getUpdates с offset=last_update_id+1
  • Каждый update диспатчит в tg_bot.handle_update
  • При сетевой ошибке — sleep 5 сек и retry
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Telegram bot long-polling for development."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=1.0,
                            help="Seconds between polls (default: 1.0)")

    def handle(self, *args, **opts):
        token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            self.stdout.write(self.style.ERROR(
                "❌ TELEGRAM_BOT_TOKEN не задан в env. Полинг невозможен."
            ))
            return

        try:
            import requests
        except ImportError:
            self.stdout.write(self.style.ERROR(
                "❌ `pip install requests` нужен для tg_poll."
            ))
            return

        from assistant.tg_bot import handle_update

        last_id = 0
        api = f"https://api.telegram.org/bot{token}"
        self.stdout.write(self.style.SUCCESS(
            f"✓ Polling started. Token …{token[-6:]}. "
            f"Interval {opts['interval']}s. Ctrl+C to stop."
        ))

        while True:
            try:
                r = requests.get(
                    f"{api}/getUpdates",
                    params={"offset": last_id + 1, "timeout": 25},
                    timeout=30,
                    allow_redirects=False,
                )
                if r.status_code != 200:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠ HTTP {r.status_code}: {r.text[:200]}"
                    ))
                    time.sleep(5)
                    continue
                data = r.json()
                for u in data.get("result", []):
                    last_id = max(last_id, u.get("update_id", 0))
                    self.stdout.write(f"  → update #{u.get('update_id')}")
                    try:
                        handle_update(u)
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(
                            f"    ✗ handler failed: {e}"
                        ))
                time.sleep(opts["interval"])
            except KeyboardInterrupt:
                self.stdout.write(self.style.SUCCESS("\n✓ Stopped."))
                break
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ network error: {e}; retry in 5s"
                ))
                time.sleep(5)
