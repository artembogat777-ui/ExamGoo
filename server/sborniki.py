"""Сборники прошлых лет: sborniki/<год>.json -> задания приложения.

Один файл = один год, внутри все варианты:
  {"year": 2017, "subject": "rus", "variants": [
      {"variant_number": 1,
       "reading_text": {"title": "К заданиям А28—А30 и В1—В5", "sentences": ["(1) ...", ...]},
       "tasks": [{"number": "А1", "text": "...", "options": ["1) ...", ...] | null, "answer": [2, 4] | "слово" | "А2Б1В3",
                  "topic": "...", "explanation": "...", "doubt": "", "hints": ["необязательная подсказка", ...]}]}]}
Старый формат одного варианта (variant_number и tasks в корне файла) тоже читается: год берётся из имени файла.
Файлы с одним годом можно складывать по частям (2017.json, 2017_v2.json): всё объединяется по номеру варианта."""
import json, re
from pathlib import Path

LETTER = {"А": "A", "A": "A", "В": "B", "B": "B", "Б": "B"}  # в сборнике часть В; латиница, кириллица и «Б» из старых выгрузок
LABEL = {"A": "А", "B": "В"}
MATCH = re.compile(r"(?:[АБВГДабвгд]\d)+")
SUBJECTS = {"rus", "math", "phys", "chem", "bio", "hist"}


def split_lines(x):
    if isinstance(x, str):
        x = x.split("\n")
    return [str(s).strip() for s in x if str(s).strip()][:8] if isinstance(x, list) else []


def load_file(path):
    """-> (список заданий в формате таблицы tasks, список замечаний)."""
    path = Path(path)
    source = path.read_text("utf-8-sig")
    if not source.strip():
        return [], [f"{path.name}: пустой JSON-файл; удаление заданий из базы пропущено"]
    if source.lstrip().startswith("["):
        return [], []  # плоский список: его читает init_db
    text = source.replace("\r", "").replace("\n", " ")  # переносы из выгрузки; \n внутри строк остаются
    docs, pos, dec = [], 0, json.JSONDecoder()
    while True:  # в файле может лежать несколько объектов подряд: новый вариант можно просто дописать ниже предыдущего
        while pos < len(text) and text[pos] in " \t,\ufeff":
            pos += 1
        if pos >= len(text):
            break
        try:
            d, pos = dec.raw_decode(text, pos)
        except ValueError as e:
            return [], [f"{path.name}: ошибка JSON возле символа {pos + getattr(e, 'pos', 0)}: {text[max(0, pos + getattr(e, 'pos', 0) - 40):pos + getattr(e, 'pos', 0) + 40]!r}"]
        docs += d if isinstance(d, list) else [d]
    notes, variants, year, subject, default_ok = [], [], 0, next(
        (part for part in reversed(path.parent.parts) if part in SUBJECTS), "rus"), True
    for d in docs:
        if not isinstance(d, dict):
            continue
        year = int(d.get("year") or year or 0)
        subject, default_ok = d.get("subject", subject), d.get("verified", default_ok)
        variants += d["variants"] if "variants" in d else [d]
    year = year or int((re.search(r"\d{4}", path.stem) or ["0"])[0])
    if not year:
        return [], [f"{path.name}: не удалось определить год (поле year или 4 цифры в имени файла)"]
    out, seen = [], set()
    for v in variants:
        vn = v.get("variant_number", v.get("variant", 1))
        rt = v.get("reading_text") or {}
        text = " ".join(rt.get("sentences") or []) if isinstance(rt.get("sentences"), list) else str(rt.get("sentences") or "")
        spans = [(LETTER[m[0]], int(m[1]), int(m[2])) for m in re.findall(r"([АВБAB])(\d+)\s*[—–-]\s*[АВБAB]?(\d+)", rt.get("title", ""))]
        for t in v.get("tasks", []):
            m = re.fullmatch(r"\s*([АВБAB])\s*(\d+)\s*", str(t.get("number", "")))
            if not m:
                notes.append(f"{year}/вариант {vn}: непонятный номер задания {t.get('number')!r}")
                continue
            L, n = LETTER[m[1]], int(m[2])
            vs = v.get("subject", subject)  # предмет: поле "subject" в файле или в варианте (rus, math, phys, bio, chem, hist)
            tid = f"{year}-v{vn}-{L}{n}" if vs == "rus" else f"{vs}-{year}-v{vn}-{L}{n}"
            w = lambda msg: notes.append(f"{tid}: {msg}")
            if tid in seen:
                w("повторяющийся номер, второе задание пропущено")
                continue
            seen.add(tid)
            ans = t.get("answer")
            if ans in (None, "", []):
                w("нет ответа, задание пропущено")
                continue
            opts = [re.sub(r"^\s*\d\)\s*", "", str(o)).strip() for o in (t.get("options") or [])]
            if opts:
                kind = "multi"
                ans = [int(x) for x in (re.findall(r"\d", ans) if isinstance(ans, str) else ans)]
                if not ans or any(x < 1 or x > len(opts) for x in ans):
                    w("номер ответа вне вариантов, задание пропущено")
                    continue
            else:
                ans = str(ans).strip()
                if re.fullmatch(r"\d(?:[\s,;]*\d)*", ans):
                    kind, ans = "nums", [int(x) for x in re.findall(r"\d", ans)]
                else:
                    kind = "match" if MATCH.fullmatch(ans.replace(" ", "")) else "text"
            q = str(t.get("text") or t.get("task") or t.get("question") or "").strip()
            passage = text if any(L == l and lo <= n <= hi for l, lo, hi in spans) else ""
            if "(?)" in q + passage:
                w("в тексте есть «(?)»: сверьте пропуск со сборником")
            if "выделенн" in q and "**" not in q + passage:
                w("в задании «выделенное слово», но выделение в тексте не отмечено (**слово**)")
            doubt = str(t.get("doubt") or "").strip()
            ok = bool(t.get("verified", default_ok)) and not doubt
            if doubt:
                w("ИИ сомневается в ключе, задание скрыто: " + doubt)
            out.append({"id": tid, "exam": "ЦТ", "subject": vs, "topic": str(t.get("topic") or "").strip() or "Часть " + LABEL[L],
                        "level": 1, "type": kind, "question": q, "options": opts, "answer": ans,
                        "explanation": split_lines(t.get("explanation")), "source": f"ЦТ {year}, вариант {vn}", "verified": ok,
                        "distractors": {}, "passage": passage, "year": year, "variant": int(vn), "num": f"{LABEL[L]}{n}",
                        "part": L, "hints": split_lines(t.get("hints")), "note": doubt[:300]})
    # полнота вариантов: пропуски в нумерации почти всегда значат потерянное при выгрузке задание
    by = {}
    for t in out:
        by.setdefault((t["subject"], t["variant"]), {}).setdefault(t["part"], set()).add(int(t["num"][1:]))
    best = {L: max((len(g.get(L, ())) for g in by.values()), default=0) for L in "AB"}
    for variant_key, g in sorted(by.items()):
        vn = variant_key[1]
        for L in "AB":
            have = g.get(L, set())
            gaps = sorted(set(range(1, max(have, default=0) + 1)) - have)
            if gaps:
                notes.append(f"{year}/вариант {vn}: пропущены номера части {LABEL[L]}: {gaps}")
            if len(have) < best[L]:
                notes.append(f"{year}/вариант {vn}: неполный, в части {LABEL[L]} заданий {len(have)} (в другом варианте {best[L]})")
    return out, notes


def load_all(folder):
    tasks, notes = [], []
    for f in sorted(Path(folder).rglob("*.json")):
        try:
            t, n = load_file(f)
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as e:
            tasks_n = [f"{f.name}: файл не прочитан ({type(e).__name__}: {e})"]
            t, n = [], tasks_n
        tasks += t
        notes += n
    return tasks, notes


if __name__ == "__main__":  # проверка:  python -m server.sborniki   (печатает, что принято и что не так)
    import sys
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "tasks"
    for f in sorted(folder.glob("*.json")):
        t, n = load_file(f)
        vs = sorted({(x["year"], x["variant"]) for x in t})
        print(f"{f.name}: заданий {len(t)}, варианты: {[v for _, v in vs]}")
        for m in n:
            print("  !", m)
