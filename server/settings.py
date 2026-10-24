"""Настройки из settings.json. Файл перечитывается при каждом изменении, перезапуск сервера не нужен.
Если в файле опечатка (битый JSON), остаются прежние значения. Ключи, начинающиеся с «_», игнорируются (для заметок)."""
import copy, json, os
from pathlib import Path

PATH = Path(os.getenv("SETTINGS_PATH", Path(__file__).resolve().parent.parent / "settings.json"))

DEFAULTS = {
    "paywall_enabled": True,     # False: доступ бесплатный, оплата не требуется
    "trial_days": 7,             # бесплатный период с первого входа
    "price_stars": 50,           # цена доступа в Telegram Stars
    "paid_days": 45,             # на сколько дней даёт доступ одна оплата (0 = навсегда)
    "hearts_enabled": False,     # True: ошибка отнимает сердце, без сердец задания не выдаются
    "max_hearts": 5,
    "heart_minutes": 30,         # за сколько минут возвращается одно сердце
    "hints_enabled": True,       # подсказки в тренировке
    "hint_cost_coins": 0,        # цена одной подсказки в монетах (0 = бесплатно)
    "hints_in_exam": False,      # подсказки во время экзамена (после экзамена разбор виден всегда)
    "reveal_counts_as_miss": True,  # если до ответа открыт разбор, задание не засчитывается и уходит в архив ошибок
    "xp_per_correct": 10,        # XP за верный ответ в тренировке
    "xp_perfect_bonus": 20,      # бонус за сессию без ошибок
    "exam_minutes": 120,         # время на экзамен по варианту (0 = без лимита)
    "exam_xp_per_correct": 5,    # XP за каждый верный ответ на экзамене
    "free_plan": {               # что доступно БЕЗ оплаты после пробного срока (true = открыто)
        "daily_tasks": 10,       #   заданий в день (0 = без лимита)
        "sborniki": False,       #   варианты прошлых лет и тренировка по ним
        "exam": False,           #   экзамены по вариантам
        "errors": False,         #   работа над ошибками (ошибки при этом записываются, после оплаты всё на месте)
        "hints": False,          #   подсказки и разбор по ходу задания
        "score": False,          #   примерный балл и уровень
    },
    "error_steps_days": [1, 3, 7, 14],  # через сколько дней повторять ошибку; после последнего шага она считается проработанной
    "error_reinforcement_days": [30, 60, 90],  # повторное закрепление после закрытия первичного цикла
    "referral_bonus_days": 3,    # дней доступа обоим за приглашённого друга (0 = выключено)
    "reminder_enabled": True,    # напоминание ботом о непроработанных ошибках
    "reminder_hour": 18,         # час по Минску
    "share_text": "Я набрал ~{score} баллов на варианте {year}-{variant} в тренажёре ЦТ. Попробуй и ты:",
    "exam_points": {"A": 1, "B": 1},  # баллов за верное задание части А и части В
    "exam_scale": [],            # таблица пересчёта [[первичных_баллов, тестовый_балл], ...]; пусто = линейно до 100
}

_cache = {"mtime": None, "invalid_mtime": None, "data": copy.deepcopy(DEFAULTS)}


def _valid_value(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return type(value) is int
    if isinstance(default, float):
        return type(value) in (int, float)
    return isinstance(value, type(default))


def _merge(raw):
    data = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key.startswith("_") or key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                continue
            nested = dict(default)
            for nested_key, nested_value in value.items():
                if nested_key in default and _valid_value(default[nested_key], nested_value):
                    nested[nested_key] = nested_value
            data[key] = nested
        elif isinstance(default, list):
            if not isinstance(value, list):
                continue
            if key in ("error_steps_days", "error_reinforcement_days") and all(type(item) is int and item > 0 for item in value):
                data[key] = value
            elif key == "exam_scale" and all(
                    isinstance(row, list) and len(row) == 2
                    and all(type(item) in (int, float) for item in row) for row in value):
                data[key] = value
        elif _valid_value(default, value):
            data[key] = value
    for key in ("trial_days", "paid_days", "hint_cost_coins", "xp_per_correct", "xp_perfect_bonus",
                "exam_minutes", "exam_xp_per_correct", "referral_bonus_days"):
        data[key] = max(0, data[key])
    data["price_stars"] = max(1, data["price_stars"])
    data["max_hearts"] = max(1, data["max_hearts"])
    data["heart_minutes"] = max(1, data["heart_minutes"])
    data["reminder_hour"] = min(23, max(0, data["reminder_hour"]))
    data["free_plan"]["daily_tasks"] = max(0, data["free_plan"]["daily_tasks"])
    data["exam_points"] = {key: max(0, value) for key, value in data["exam_points"].items() if key in ("A", "B")}
    scale = sorted((max(0, row[0]), min(100, max(0, row[1]))) for row in data["exam_scale"])
    data["exam_scale"] = []
    for primary, score in scale:
        if not data["exam_scale"] or primary > data["exam_scale"][-1][0]:
            data["exam_scale"].append([primary, score])
        else:
            data["exam_scale"][-1][1] = score
    return data


def get():
    try:
        m = PATH.stat().st_mtime
    except OSError:
        return _cache["data"]
    if m != _cache["mtime"] and m != _cache["invalid_mtime"]:
        try:
            raw = json.loads(PATH.read_text("utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("корневое значение должно быть JSON-объектом")
            _cache["data"] = _merge(raw)
            _cache["mtime"] = m
            _cache["invalid_mtime"] = None
        except (OSError, ValueError) as e:
            _cache["invalid_mtime"] = m
            print("settings.json не прочитан, остаются прежние значения:", e)
    return _cache["data"]


def cfg(key):
    return get()[key]
