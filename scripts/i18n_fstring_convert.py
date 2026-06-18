#!/usr/bin/env python3
"""Конвертация f-строк в display-контекстах в gettext-форму.

f"Заказ #{order.id} · {q.total:,.2f}"
  → _("Заказ #%(p0)s · %(p1)s") % {"p0": order.id, "p1": f"{q.total:,.2f}"}

Каждое поле f-строки превращается в standalone-f-строку в dict (формат-спеки
сохраняются дословно), статические части — в msgid с %(pN)s; литеральный % → %%.
Оборачиваем ТОЛЬКО display-контексты (значения display-ключей словарей и
display-kwargs), не трогаем логику. Идемпотентно (уже-обёрнутые _() пропускаем).

Запуск: python3 scripts/i18n_fstring_convert.py <file.py> [<file.py> ...]
"""
from __future__ import annotations

import ast
import re
import sys

CYR = re.compile(r'[а-яА-ЯёЁ]')
DISPLAY_KEYS = {"title", "label", "subtitle", "sub", "note", "body", "text",
                "confirm_label", "cancel_label", "placeholder", "hint", "caption",
                "header", "footer", "message", "msg", "tooltip", "description",
                "desc", "summary", "empty", "reason"}
DISPLAY_KWARGS = {"text", "title", "label", "reason", "body", "subtitle", "note",
                  "placeholder", "hint", "summary", "confirm_label", "cancel_label"}


def joinedstr_has_cyr(js):
    return any(isinstance(v, ast.Constant) and isinstance(v.value, str) and CYR.search(v.value)
              for v in js.values)


def convert_js(js, src):
    """JoinedStr -> (code_str). Возвращает None, если нет полей (чистый литерал)."""
    parts = []
    items = []
    pidx = 0
    for v in js.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value).replace('%', '%%'))
        elif isinstance(v, ast.FormattedValue):
            name = f"p{pidx}"; pidx += 1
            # standalone f-строка для этого поля — формат/конверсия сохраняются точно
            val_src = ast.unparse(ast.JoinedStr(values=[v]))
            parts.append(f"%({name})s")
            items.append((name, val_src))
    msgid = ''.join(parts)
    if not items:
        return f"_({msgid!r})"
    dict_src = '{' + ', '.join(f'"{n}": {v}' for n, v in items) + '}'
    return f"_({msgid!r}) % {dict_src}"


def collect_targets(tree):
    targets = []  # JoinedStr nodes in display context
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, val in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value in DISPLAY_KEYS
                        and isinstance(val, ast.JoinedStr) and joinedstr_has_cyr(val)):
                    targets.append(val)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg in DISPLAY_KWARGS and isinstance(kw.value, ast.JoinedStr)
                        and joinedstr_has_cyr(kw.value)):
                    targets.append(kw.value)
    return targets


def main():
    for fn in sys.argv[1:]:
        src = open(fn, encoding='utf-8').read()
        tree = ast.parse(src)
        targets = collect_targets(tree)
        if not targets:
            print(f"{fn}: 0 f-строк в display-контексте"); continue
        # ВАЖНО: AST col_offset — байтовые смещения (UTF-8), работаем в bytes
        data = src.encode('utf-8')
        blines = data.split(b'\n')
        boffs = [0]
        for bl in blines:
            boffs.append(boffs[-1] + len(bl) + 1)
        def bpos(ln, col):
            return boffs[ln - 1] + col
        repls = []
        for js in targets:
            start = bpos(js.lineno, js.col_offset)
            end = bpos(js.end_lineno, js.end_col_offset)
            repls.append((start, end, convert_js(js, src).encode('utf-8')))
        repls.sort(key=lambda r: -r[0])
        seen = set()
        for start, end, code in repls:
            if start in seen:
                continue
            seen.add(start)
            data = data[:start] + code + data[end:]
        new_src = data.decode('utf-8')
        ast.parse(new_src)  # синтаксис-гейт ДО записи
        open(fn, 'w', encoding='utf-8').write(new_src)
        print(f"{fn}: сконвертировано f-строк = {len(targets)}")


if __name__ == "__main__":
    main()
