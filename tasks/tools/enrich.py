"""Добавляет в задания тему и пояснения через Gemini API (Google AI Studio); результат пишется прямо в tasks/*.json.
Ключ: aistudio.google.com -> Get API key.  PowerShell:  $env:GEMINI_API_KEY="ключ"
Запуск:  python tools/enrich.py [--file tasks/2017.json] [--limit 8] [--batch 4] [--model gemini-flash-latest]
Можно прерывать и запускать снова: обработанные задания пропускаются. Пояснения помечаются "ai": "draft".
Если ИИ сомневается в ключе ответа, задание скрывается (verified=false) и получает review_note."""
import argparse, json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

TOPICS = ["Орфография: корни", "Орфография: приставки", "Орфография: суффиксы и окончания", "Н и НН", "НЕ и НИ",
          "Слитно, раздельно, через дефис", "Пунктуация: простое предложение", "Пунктуация: сложное предложение",
          "Пунктуация: вводные слова и чужая речь", "Лексика и фразеология", "Морфология", "Словообразование",
          "Синтаксис", "Анализ текста", "Нормы речи"]
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
PROMPT = ("Ты опытный учитель русского языка в Беларуси, готовишь школьников к ЦТ. Ниже задания из сборника. "
          "Верный ответ из сборника правильный, НЕ меняй его: твоя задача объяснить.\n"
          "Для каждого задания верни объект JSON с полями:\n"
          '- "id": как в задании;\n- "topic": одна тема из списка: ' + "; ".join(TOPICS) + ";\n"
          '- "key_ok": true, если ответ из сборника верен по правилам современного русского языка, иначе false;\n'
          '- "note": если key_ok равно false, кратко почему, иначе пустая строка;\n'
          '- "explanation": массив коротких строк на русском. Для заданий с вариантами по одной строке на каждый вариант: '
          '«1) верно: ...» или «1) неверно: ...» с названием правила. Для остальных 2-4 строки: правило и как оно применяется.\n'
          "Если в задании упомянуто выделенное слово, которого нет в тексте, не угадывай его: key_ok оставь true, "
          "а в explanation напиши, что нужна сверка с книгой.\nОтвет: только JSON-массив.\n\n")


def ask(model, key, prompt):
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    for wait in (5, 20, 60, 120):
        req = urllib.request.Request(URL.format(model), body, {"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 429 and "PerDay" in text:
                sys.exit("Дневной лимит исчерпан. Запустите скрипт завтра: он продолжит с того же места.")
            if e.code in (429, 500, 503):
                print(f"  сервис занят ({e.code}), жду {wait} с")
                time.sleep(wait)
                continue
            sys.exit(f"Ошибка API {e.code}: {text}")
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, IndexError) as e:
            print("  сбой, повторяю:", str(e)[:80])
            time.sleep(wait)
    return None


def render(t):
    ans = ", ".join(map(str, t["answer"])) if isinstance(t["answer"], list) else t["answer"]
    parts = [f'### id: {t["id"]}', f'Тип: {t["type"]}']
    if t.get("passage"):
        parts.append("Текст к заданию: " + t["passage"][:1800])
    parts.append("Задание: " + t["question"])
    if t.get("options"):
        parts.append("Варианты:\n" + "\n".join(f"{i}) {o}" for i, o in enumerate(t["options"], 1)))
    parts.append(f"Верный ответ по сборнику: {ans}")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--pause", type=float, default=4)
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
    a = ap.parse_args()
    key = os.environ.get("GEMINI_API_KEY") or sys.exit('Задайте ключ: $env:GEMINI_API_KEY="..."')
    root = Path(__file__).resolve().parent.parent / "tasks"
    files = [Path(a.file)] if a.file else sorted(root.glob("*.json"))
    done = 0
    for f in files:
        tasks = json.loads(f.read_text("utf-8"))
        todo = [t for t in tasks if "ai" not in t]
        if a.limit:
            todo = todo[: max(0, a.limit - done)]
        for i in range(0, len(todo), a.batch):
            chunk = todo[i:i + a.batch]
            res = ask(a.model, key, PROMPT + "\n\n".join(render(t) for t in chunk))
            if isinstance(res, dict):
                res = next((v for v in res.values() if isinstance(v, list)), [])
            res = [r for r in (res or []) if isinstance(r, dict)]
            by = {str(r.get("id")): r for r in res}
            if len(res) == len(chunk) and not all(t["id"] in by for t in chunk):
                by = {t["id"]: r for t, r in zip(chunk, res)}  # модель исказила id: сопоставляем по порядку
            for t in chunk:
                r, ex = by.get(t["id"]), (by.get(t["id"]) or {}).get("explanation")
                if not (isinstance(ex, list) and ex and all(isinstance(x, str) for x in ex)):
                    print("  пропуск", t["id"])
                    continue
                t["explanation"] = [x.strip() for x in ex][:8]
                if r.get("topic") in TOPICS:
                    t["topic"] = r["topic"]
                t["ai"] = "draft"
                if r.get("key_ok") is False:
                    t["verified"], t["review_note"] = False, str(r.get("note", ""))[:300]
                    print("  сомнение в ключе:", t["id"], t["review_note"])
            f.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), "utf-8")
            done += len(chunk)
            print(f"{f.name}: обработано {done}")
            time.sleep(a.pause)


main()
