"""Детерминированный перевод названий запчастей EN/DE → RU.

НАДЁЖНОСТЬ: в рантайме — только код (словарь-лукап по словам), без AI.
Один и тот же вход всегда даёт один и тот же выход. AI используется лишь
ОФЛАЙН, чтобы наполнить словарь, — а в продакшене работает этот код.

Каталог почти весь латиницей (Komatsu/Epiroc — английский, Liebherr — немецкий).
Разных слов всего ~15К, топ-2000 покрывают 96% вхождений, поэтому переводим
ПО СЛОВАМ, а не по целым названиям.

Пайплайн translate_title():
  1. нормализация: верхний регистр, схлопывание пробелов;
  2. фразовые оверрайды (WIRING HARNESS → жгут проводов) по границам слов;
  3. срез бренд-мусора (KOMATSU/PART/…) — лезет в начало синтетических имён;
  4. раскрытие «в сборе» (ASS'Y / KPL / COMPLETE → одно «в сборе»);
  5. перевод каждого слова по GLOSSARY; неизвестные слова и модель-коды
     (PC220, 6D102) остаются как есть;
  6. если ничего не перевелось (нет кириллицы) → '' (русское имя отсутствует,
     фронт покажет оригинал).

Словарь — обычный dict в коде (как customs_data). Расширяется отдельно, с
вычиткой терминологии. Незнакомое слово НИКОГДА не теряется — проходит как есть.
"""
from __future__ import annotations

import re

# Бренд-слова и служебный мусор, который попадает в title и не несёт смысла
# («Komatsu Pc220rock Bucket1» → выкидываем KOMATSU, оставляем модель+тип).
NOISE = {
    "KOMATSU", "LIEBHERR", "EPIROC", "CATERPILLAR", "CAT", "HITACHI",
    "SANDVIK", "ATLAS", "COPCO", "BOMAG", "DANA", "BERCO", "KRONVERK",
    "GENERIC", "PART", "PARTS",
}

# Все варианты «в сборе» → один токен (порядок и дубли схлопываем).
ASSEMBLY_WORDS = {
    "ASS'Y", "ASSY", "ASS'", "ASS", "ASSEMBLY", "ASSEMBLED",
    "COMPLETE", "CPL", "KPL", "KPL.", "KOMPL", "KOMPL.", "KOMPLETT",
}
ASSEMBLY_RU = "в сборе"

# Частотные двусловные сочетания, которые по словам собираются криво.
PHRASES = {
    "WIRING HARNESS": "жгут проводов",
    "CONTROL VALVE": "клапан управления",
    "RELIEF VALVE": "предохранительный клапан",
    "CHECK VALVE": "обратный клапан",
    "SOLENOID VALVE": "электромагнитный клапан",
    "OIL SEAL": "сальник",
    "O RING": "уплотнительное кольцо",
    "TRACK SHOE": "башмак гусеницы",
    "TRACK ROLLER": "опорный каток",
    "CARRIER ROLLER": "поддерживающий каток",
    "DRIVE SHAFT": "приводной вал",
}

# Словарь EN + DE → RU по словам. Покрывает частотное ядро каталога.
# Незнакомые слова проходят как есть — ничего не теряется.
GLOSSARY = {
    # ── базовые типы деталей (EN) ──
    "HOSE": "шланг", "BRACKET": "кронштейн", "PLATE": "пластина",
    "COVER": "крышка", "TUBE": "трубка", "SHEET": "лист", "VALVE": "клапан",
    "SEAL": "уплотнение", "RING": "кольцо", "FRAME": "рама",
    "HARNESS": "жгут проводов", "CABLE": "кабель", "WIRING": "проводка",
    "KIT": "комплект", "SET": "комплект", "PACKING": "уплотнение",
    "CYLINDER": "цилиндр", "PIN": "палец", "PIPE": "труба", "BOLT": "болт",
    "BEARING": "подшипник", "SPRING": "пружина", "GEAR": "шестерня",
    "SCREW": "винт", "BLOCK": "блок", "ROD": "шток", "SHAFT": "вал",
    "ARM": "рычаг", "WASHER": "шайба", "GUARD": "защита", "BUSHING": "втулка",
    "BUSH": "втулка", "ENGINE": "двигатель", "SPACER": "проставка",
    "CLAMP": "хомут", "PUMP": "насос", "CAB": "кабина", "LEVER": "рычаг",
    "SHIM": "регулировочная шайба", "PLUG": "пробка", "OIL": "масляный",
    "ADAPTER": "переходник", "GASKET": "прокладка", "ELBOW": "отвод",
    "NUT": "гайка", "HEAD": "головка", "NIPPLE": "ниппель",
    "PISTON": "поршень", "CAP": "колпак", "SUPPORT": "опора",
    "SWITCH": "выключатель", "MOTOR": "мотор", "TRACK": "гусеница",
    "TANK": "бак", "CONTROL": "управление", "HOLDER": "держатель",
    "MAST": "мачта", "BOOM": "стрела", "LOCK": "фиксатор",
    "FILTER": "фильтр", "JOINT": "соединение", "BOX": "коробка",
    "RUBBER": "резина", "SEAT": "сиденье", "BAR": "штанга",
    "CASE": "корпус", "CONNECTOR": "разъём", "ATTACHMENT": "навеска",
    "HOUSING": "корпус", "LINK": "звено", "CUSHION": "подушка",
    "BUCKET": "ковш", "ROLLER": "каток", "SENSOR": "датчик",
    "SHOE": "башмак", "FORK": "вилы", "WIRE": "провод", "AIR": "воздушный",
    "COOLER": "охладитель", "DOOR": "дверь", "FLANGE": "фланец",
    "GUIDE": "направляющая", "DRIVE": "привод", "COUPLING": "муфта",
    "HOOD": "капот", "STUD": "шпилька", "WEIGHT": "противовес",
    "HYDRAULIC": "гидравлический", "HYD": "гидравлический",
    "FITTING": "фитинг", "BLADE": "отвал", "CLIP": "скоба",
    "DUCTING": "воздуховод", "DUCT": "воздуховод", "PANEL": "панель",
    "FENDER": "крыло", "STAY": "стойка", "CHAIN": "цепь",
    "RADIATOR": "радиатор", "COLLAR": "втулка", "FUEL": "топливный",
    "PRESSURE": "давление", "LIFT": "подъём", "CONTROLLER": "контроллер",
    "BOARD": "плата", "TEE": "тройник", "SLEEVE": "втулка",
    "SIGNAL": "сигнал", "GAUGE": "указатель", "BODY": "корпус",
    "GLASS": "стекло", "RETAINER": "фиксатор", "STEP": "ступень",
    "MOUNT": "опора", "MOUNTING": "крепление", "PROTECTOR": "защита",
    "TERMINAL": "клемма", "RELAY": "реле", "LAMP": "фонарь",
    "LIGHT": "фонарь", "GLOW": "накаливания", "BATTERY": "аккумулятор",
    "FAN": "вентилятор", "BELT": "ремень", "PULLEY": "шкив",
    "NOZZLE": "форсунка", "INJECTOR": "форсунка", "MUFFLER": "глушитель",
    "EXHAUST": "выхлоп", "MANIFOLD": "коллектор", "TURBOCHARGER": "турбокомпрессор",
    "AXLE": "ось", "WHEEL": "колесо", "SPROCKET": "звёздочка",
    "IDLER": "направляющее колесо", "TENSIONER": "натяжитель",
    "DAMPER": "демпфер", "ACTUATOR": "привод", "SOLENOID": "электромагнит",
    "RELIEF": "предохранительный", "CHECK": "обратный",
    "SUCTION": "всасывающий", "RETURN": "сливной", "DELIVERY": "напорный",
    "ACCUMULATOR": "гидроаккумулятор", "STRAINER": "сетчатый фильтр",
    "BREATHER": "сапун", "DIPSTICK": "щуп", "THERMOSTAT": "термостат",
    "STARTER": "стартер", "ALTERNATOR": "генератор", "GENERATOR": "генератор",
    "STEERING": "рулевой", "BRAKE": "тормоз", "CLUTCH": "сцепление",
    "PEDAL": "педаль", "MIRROR": "зеркало", "HANDLE": "ручка",
    "HINGE": "петля", "LATCH": "защёлка", "GRILLE": "решётка",
    "WIPER": "стеклоочиститель", "HEATER": "отопитель", "BLOWER": "вентилятор",
    "COMPRESSOR": "компрессор", "CONDENSER": "конденсатор",
    "EVAPORATOR": "испаритель", "HARDWARE": "крепёж", "BUMPER": "бампер",
    "ROPS": "защитная дуга", "STRIP": "планка", "MESH": "сетка",
    "CORE": "сердечник", "NEEDLE": "игла", "BALL": "шарик",

    # ── немецкий (Liebherr) ──
    "SCHLAUCH": "шланг", "DRUCKLEITUNG": "напорный трубопровод",
    "BOLZEN": "палец", "KABELSATZ": "жгут проводов", "KONSOLE": "кронштейн",
    "SECHSKANTSCHRAUBE": "болт с шестигранной головкой", "HALTER": "держатель",
    "DECKEL": "крышка", "SCHEIBE": "шайба", "DICHTUNG": "уплотнение",
    "BLECH": "лист", "PAKET": "комплект", "BUCHSE": "втулка",
    "ZYLINDERSCHRAUBE": "винт с цилиндрической головкой", "SCHILD": "табличка",
    "VERSCHRAUBUNG": "резьбовое соединение", "KOLBEN": "поршень",
    "HYDRAULIKZYLINDER": "гидроцилиндр", "SCHRAUBE": "винт",
    "MUTTER": "гайка", "FEDER": "пружина", "LAGER": "подшипник",
    "DICHTRING": "уплотнительное кольцо", "ROHR": "труба", "PLATTE": "пластина",
    "WELLE": "вал", "GEHAEUSE": "корпус", "VENTIL": "клапан",
    "ZAHNRAD": "шестерня", "KOLBENSTANGE": "шток", "STUTZEN": "патрубок",
    "STECKER": "разъём", "KABEL": "кабель", "LEITUNG": "трубопровод",
    "FILTEREINSATZ": "фильтрующий элемент", "ABDECKUNG": "крышка",
    "WINKEL": "уголок", "PUMPE": "насос", "ZYLINDER": "цилиндр",
    "MONTAGEHUELSE": "монтажная втулка", "DAEMMMATTE": "шумоизоляционный мат",
    "LINKS": "левый", "RECHTS": "правый",
    "FAHRANTRIEB": "ходовой привод", "FAHRWERK": "ходовая часть",
    "ANTRIEB": "привод", "GETRIEBE": "редуктор", "DREHANTRIEB": "поворотный привод",
    "HYDRAULIKPUMPE": "гидронасос", "HYDRAULIKMOTOR": "гидромотор",
    "STEUERBLOCK": "распределитель", "KUEHLER": "радиатор",
    "SCHELLE": "хомут", "STIFT": "штифт", "FILTER'": "фильтр",
    "DRUCKBEGRENZUNGSVENTIL": "предохранительный клапан",
    "RUECKSCHLAGVENTIL": "обратный клапан", "MAGNETVENTIL": "электромагнитный клапан",

    # ── направление / признак ──
    "LEFT": "левый", "RIGHT": "правый", "FRONT": "передний", "REAR": "задний",
    "UPPER": "верхний", "LOWER": "нижний", "INNER": "внутренний",
    "OUTER": "наружный", "MAIN": "главный",
}


def _strip_alpha(token: str) -> str:
    """Буквенное ядро токена в верхнем регистре (для лукапа), вкл. ' и умляуты."""
    return re.sub(r"[^A-ZÄÖÜß']", "", token.upper())


def translate_title(title: str) -> str:
    """EN/DE-название → русское. '' если перевести нечего (нет кириллицы)."""
    if not title:
        return ""
    # Скобки/слэши разъединяют склеенные слова: «Roller(hitachizx330-3)» →
    # «Roller hitachizx330-3», иначе ROLLER не переведётся.
    upper = re.sub(r"[()\[\]{}/]", " ", title.upper())
    upper = re.sub(r"\s+", " ", upper).strip()
    if not upper:
        return ""

    # Фразовые оверрайды по границам слов (padding пробелами, чтобы «O RING»
    # не сработал внутри «MICRO RING»). Готовый перевод метим \x01, пробелы — \x02.
    padded = " " + upper + " "
    for ph, ru in PHRASES.items():
        padded = padded.replace(" " + ph + " ", " \x01" + ru.replace(" ", "\x02") + " ")
    upper = padded.strip()

    out: list[str] = []
    seen_assembly = False
    for tok in upper.split(" "):
        if not tok:
            continue
        if tok.startswith("\x01"):           # готовый перевод из фразы
            out.append(tok[1:].replace("\x02", " "))
            continue
        core = _strip_alpha(tok)
        if not core:
            if re.search(r"\d", tok):        # код без букв (модель/артикул)
                out.append(tok)
            continue
        if core in NOISE:
            continue
        if core in ASSEMBLY_WORDS:
            if not seen_assembly:
                out.append(ASSEMBLY_RU)
                seen_assembly = True
            continue
        ru = GLOSSARY.get(core)
        if ru:
            out.append(ru)
        elif re.search(r"\d", tok):           # модель-код (PC220, 6D102) — как есть
            out.append(tok)
        else:                                  # незнакомое слово — НЕ теряем
            out.append(tok)

    res = re.sub(r"\s+", " ", " ".join(out)).strip()
    if not re.search(r"[а-яА-Я]", res):       # перевод не состоялся → русского нет
        return ""
    return res[0].upper() + res[1:]
