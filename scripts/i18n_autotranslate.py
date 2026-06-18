#!/usr/bin/env python3
"""Авто-перевод пустых msgstr в .po через Anthropic API.

«Всё на коде»: переводы складываются в .po (данные репозитория), рантайм —
чистый gettext, без LLM в проде. Скрипт идемпотентен и возобновляем —
повторный запуск пропускает уже заполненные msgstr.

Запуск:
  python3 scripts/i18n_autotranslate.py [--lang es] [--limit N] [--batch 40]

Фильтр: переводим только msgid, содержащий кириллицу (реальный русский текст);
HTML-/шаблон-артефакты без кириллицы пропускаем. Плейсхолдеры (%(x)s, {x}, %s),
HTML-теги, emoji, ведущие/замыкающие пробелы и пунктуация сохраняются.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import polib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGS = {
    "en": "English",
    "zh_Hans": "Simplified Chinese (简体中文)",
    "es": "Spanish (español)",
    "ar": "Arabic (العربية)",
}
CYR = re.compile(r"[а-яА-ЯёЁ]")


def load_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    envp = os.path.join(BASE, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY не найден (env или .env)")


SYSTEM = """Ты профессиональный переводчик UI B2B-платформы запчастей для спецтехники.
Переводишь короткие интерфейсные строки С РУССКОГО на {lang}.

СТРОГИЕ ПРАВИЛА (нарушение ломает рантайм):
- Сохраняй ДОСЛОВНО плейсхолдеры: %(name)s, %(id)s, %s, %d, {var}, {0}.
- Сохраняй HTML-теги, CSS, URL, emoji, цифры, коды (OEM, ISO) без изменений.
- Сохраняй ведущие/замыкающие пробелы, переводы строк (\\n) и пунктуацию.
- Переводи ТОЛЬКО человекочитаемый текст; технические токены не трогай.
- Тон: деловой, краткий, как в проф. B2B-интерфейсе.
- Для китайского — упрощённый. Для арабского — современный стандартный.
- НЕ добавляй кавычки, пояснения, нумерацию — только перевод.

Вход — JSON-объект {"номер": "строка"}. Выход — JSON-объект с ТЕМИ ЖЕ ключами,
значения — переводы соответствующих строк. Выравнивание строго по ключу.
Только JSON-объект, без markdown-обёртки."""


def translate_batch(client, lang_name, sources, model):
    # Выравнивание по КЛЮЧУ (не по позиции) — защита от сдвига/перестановки.
    items = {str(i): s for i, s in enumerate(sources)}
    payload = json.dumps(items, ensure_ascii=False)
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        output_config={"effort": "low"},
        system=SYSTEM.replace("{lang}", lang_name),
        messages=[{"role": "user", "content": payload}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    out = json.loads(text)
    if not isinstance(out, dict):
        raise ValueError("ответ не JSON-объект")
    # KeyError, если ключ пропущен → батч уходит в retry
    return [out[str(i)] for i in range(len(sources))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=list(LANGS), help="одна локаль (по умолчанию все)")
    ap.add_argument("--limit", type=int, default=0, help="макс строк на локаль (0=все)")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--model", default="claude-opus-4-8")
    ap.add_argument("--retranslate", action="store_true",
                    help="перезалить ВСЕ кириллические msgid (а не только пустые)")
    args = ap.parse_args()

    import anthropic
    client = anthropic.Anthropic(api_key=load_key())

    langs = [args.lang] if args.lang else list(LANGS)
    grand = 0
    for lc in langs:
        po_path = os.path.join(BASE, "locale", lc, "LC_MESSAGES", "django.po")
        if not os.path.exists(po_path):
            print(f"[{lc}] нет .po — пропуск"); continue
        po = polib.pofile(po_path)
        todo = [e for e in po
                if not e.obsolete and not e.msgid_plural
                and (args.retranslate or not e.msgstr) and CYR.search(e.msgid)]
        if args.limit:
            todo = todo[: args.limit]
        print(f"[{lc}] к переводу: {len(todo)}")
        done = 0
        for i in range(0, len(todo), args.batch):
            chunk = todo[i : i + args.batch]
            sources = [e.msgid for e in chunk]
            for attempt in range(3):
                try:
                    out = translate_batch(client, LANGS[lc], sources, args.model)
                    break
                except Exception as ex:
                    if attempt == 2:
                        print(f"[{lc}] батч {i} провал: {ex}"); out = None
                    else:
                        time.sleep(2 * (attempt + 1))
            if not out:
                continue
            for e, tr in zip(chunk, out):
                e.msgstr = tr
                if "fuzzy" in e.flags:
                    e.flags.remove("fuzzy")
            po.save(po_path)  # сохраняем после каждого батча — возобновляемо
            done += len(chunk)
            print(f"[{lc}] {done}/{len(todo)}", flush=True)
        grand += done
        print(f"[{lc}] готово: {done}")
    print(f"ИТОГО переведено: {grand}")


if __name__ == "__main__":
    main()
