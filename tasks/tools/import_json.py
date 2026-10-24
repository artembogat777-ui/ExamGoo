"""Конвертер сборников из JSON (id, part, task, options, answer, common_text) в формат приложения.
Запуск:  python tools/import_json.py ПУТЬ [--subject rus] [--exam ЦТ] [--verified]
ПУТЬ: файл .json или папка с такими файлами. Результат: tasks/<имя файла>.json и отчёт.
Без --verified задания скрыты в приложении: включайте флаг только после ручной проверки ответов."""
import argparse, json, re
from pathlib import Path

PART = {"A": "А", "B": "Б", "В": "Б", "А": "А", "Б": "Б"}
MATCH = re.compile(r"(?:[АБВГДабвгд]\d)+")


def convert(items, name, a):
    out, warn, seen = [], [], set()
    for i, t in enumerate(items, 1):
        tid = f'{name}-{t.get("id", i)}'
        w = lambda m: warn.append(f"{tid}: {m}")
        opts = [re.sub(r"^\s*\d\)\s*", "", o).strip() for o in (t.get("options") or t.get("choices") or [])]
        pick = lambda *k: next((t[x] for x in k if t.get(x) not in (None, "")), None)  # запасные названия полей
        ans = pick("answer", "correct", "correct_answer", "answers")
        text = str(pick("task", "question", "text") or "").strip()
        passage = str(pick("common_text", "passage") or "").strip()
        if tid in seen:
            w("повторяющийся id")
        seen.add(tid)
        if ans in (None, "", []):
            w("нет ответа, задание пропущено")
            continue
        if opts:
            kind = "multi"
            ans = [int(x) for x in (re.findall(r"\d", ans) if isinstance(ans, str) else ans)]
            if not ans or any(x < 1 or x > len(opts) for x in ans):
                w("номер ответа вне вариантов, задание пропущено")
                continue
        else:
            ans = str(ans).strip()
            if re.fullmatch(r"\d(?:[\s,;]*\d)*", ans):
                kind, ans = "nums", [int(x) for x in re.findall(r"\d", ans)]  # номера цифрами, без вариантов
            else:
                kind = "match" if MATCH.fullmatch(ans.replace(" ", "")) else "text"
        if "(?)" in text + passage:
            w("в тексте есть «(?)»: сверьте пропуск со сборником")
        if "выделенн" in text and "**" not in text + passage:
            w("в задании «выделенное слово», но выделение в тексте не отмечено (**слово**)")
        out.append({"id": tid, "exam": a.exam, "subject": a.subject, "topic": "Часть " + PART.get(str(t.get("part", "")).upper(), "?"),
                    "level": 1, "type": kind, "question": text, "options": opts, "answer": ans,
                    "passage": passage, "source": name, "verified": a.verified,
                    "part": PART.get(str(t.get("part", "")).upper(), "?")})
        item, ex = out[-1], t.get("explanation")  # пояснение, тема и сомнение из JSON (подготовлены в AI Studio)
        if isinstance(ex, str):
            ex = [x.strip() for x in ex.split("\n") if x.strip()]
        if isinstance(ex, list) and ex:
            item["explanation"], item["ai"] = [str(x).strip() for x in ex][:8], "draft"
        if str(t.get("topic") or "").strip():
            item["topic"] = str(t["topic"]).strip()
        if str(t.get("doubt") or "").strip():
            item["verified"], item["review_note"] = False, str(t["doubt"]).strip()[:300]
            w("ИИ сомневается в ключе, задание скрыто: " + item["review_note"])
    return out, warn


def flatten(d):
    """Формат {test, variants:[{variant_number, reading_text, tasks:[{number, text, options, answer}]}]} -> плоский список."""
    tr = str.maketrans("АВБ", "ABB")
    items, notes, per = [], [], {}
    for v in d.get("variants", []):
        vn, rt = v.get("variant_number", "?"), v.get("reading_text") or {}
        text = " ".join(rt.get("sentences") or [])
        spans = [(m[0].translate(tr), int(m[1]), int(m[2])) for m in re.findall(r"([АВБAB])(\d+)\s*[—–-]\s*[АВБAB]?(\d+)", rt.get("title", ""))]
        got = per.setdefault(vn, {})
        for t in v.get("tasks", []):
            m = re.fullmatch(r"([АВБAB])(\d+)", str(t.get("number", "")).strip())
            if not m:
                notes.append(f"вариант {vn}: непонятный номер задания {t.get('number')!r}")
                continue
            L, n = m[1].translate(tr), int(m[2])
            got.setdefault(L, set()).add(n)
            ctext = text if any(L == l and lo <= n <= hi for l, lo, hi in spans) else ""
            items.append({"id": f"v{vn}-{L}{n}", "part": L, "task": t.get("text"), "options": t.get("options"),
                          "answer": t.get("answer"), "common_text": ctext,
                          "explanation": t.get("explanation"), "topic": t.get("topic"), "doubt": t.get("doubt")})
    best = {L: max((len(g.get(L, ())) for g in per.values()), default=0) for L in "AB"}
    for vn, g in per.items():
        for L in "AB":
            have = g.get(L, set())
            gaps = sorted(set(range(1, max(have, default=0) + 1)) - have)
            if gaps:
                notes.append(f"вариант {vn}: пропущены номера части {L}: {gaps}")
            if len(have) < best[L]:
                notes.append(f"вариант {vn}: неполный, в части {L} заданий {len(have)} (в другом варианте {best[L]})")
    return items, notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--subject", default="rus")
    p.add_argument("--exam", default="ЦТ")
    p.add_argument("--verified", action="store_true")
    a = p.parse_args()
    src = Path(a.path)
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    root = Path(__file__).resolve().parent.parent / "tasks"
    for f in files:
        raw = f.read_text("utf-8-sig").replace("\r", "").replace("\n", " ")  # переносы строк из выгрузки; \n внутри строк остаются
        items = json.loads(raw)
        notes = []
        if isinstance(items, dict) and "variants" in items:
            items, notes = flatten(items)
        if isinstance(items, dict):  # файл вида {"tasks": [...]}: берём первый список объектов
            lists = [v for v in items.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
            items = lists[0] if lists else []
        bad = [x for x in items if not isinstance(x, dict)]
        if bad:
            print(f"  ! {f.name}: пропущено элементов, не являющихся заданиями: {len(bad)} (первый: {str(bad[0])[:60]!r})")
        items = [x for x in items if isinstance(x, dict)]
        if not items:
            print(f"{f.name}: заданий не найдено. Пришлите первые 20 строк файла, подстрою формат.")
            continue
        out, warn = convert(items, f.stem, a)
        dst = root / f"{f.stem}.json"
        if dst.exists():  # при повторном импорте не теряем пояснения ИИ (tools/enrich.py)
            old = {t["id"]: t for t in json.loads(dst.read_text("utf-8"))}
            for t in out:
                o = old.get(t["id"])
                if o and o.get("ai") and not t.get("explanation"):
                    t.update({k: o[k] for k in ("topic", "explanation", "ai", "review_note", "verified") if k in o})
        dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        parts = ", ".join(f"{k}: {sum(1 for t in out if t['part'] == k)}" for k in "АБ")
        print(f"{f.name}: принято {len(out)} из {len(items)} ({parts})")
        for m in notes + warn:
            print("  !", m)


main()
