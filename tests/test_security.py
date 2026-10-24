import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import HTTPException

from server import billing, main, settings


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(main.SCHEMA)
        main.migrate(self.db)
        now = int(time.time())
        self.db.execute("INSERT INTO users(id, name, hearts_ts, created_ts) VALUES(1, 'Tester', ?, ?)", (now, now))
        for task_id, verified in (("visible", True), ("hidden", False)):
            main.put_task(self.db, {
                "id": task_id,
                "subject": "rus",
                "type": "choice",
                "question": "Test question",
                "options": ["correct", "wrong"],
                "answer": 0,
                "explanation": [],
                "verified": verified,
            })
        self.user = self.db.execute("SELECT * FROM users WHERE id=1").fetchone()

    def tearDown(self):
        self.db.close()

    def test_unverified_task_cannot_be_answered_or_hinted(self):
        with self.assertRaises(HTTPException) as answer_error:
            main.answer(main.Ans(task_id="hidden", answer=0), self.user, self.db)
        self.assertEqual(answer_error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as hint_error:
            main.hint("hidden", 1, self.user, self.db)
        self.assertEqual(hint_error.exception.status_code, 404)

    def test_request_models_reject_oversized_user_input(self):
        with self.assertRaises(ValueError):
            main.Ans(task_id="visible", answer="x" * 2001)
        with self.assertRaises(ValueError):
            main.ExamSave(exam_id=1, task_id="visible", answer=list(range(101)))
        with self.assertRaises(ValueError):
            main.Report(task_id="visible", note="x" * 301)

    def test_duplicate_task_report_is_deduplicated_for_one_day(self):
        report = main.Report(task_id="visible", note="Typo")
        first = main.report(report, self.user, self.db)
        second = main.report(report, self.user, self.db)
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports WHERE user_id=1").fetchone()[0], 1)

    def test_report_submission_has_daily_per_user_cap(self):
        now = int(time.time())
        self.db.executemany("INSERT INTO reports(user_id,task_id,note,ts) VALUES(1,'visible','existing',?)",
                            [(now - number,) for number in range(20)])
        with patch.object(main.time, "time", return_value=now), self.assertRaises(HTTPException) as error:
            main.report(main.Report(task_id="hidden", note="another task"), self.user, self.db)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports WHERE user_id=1").fetchone()[0], 20)

    def test_repeated_correct_answer_only_rewards_once_per_day(self):
        first = main.answer(main.Ans(task_id="visible", answer=0), self.user, self.db)
        second = main.answer(main.Ans(task_id="visible", answer=0), self.user, self.db)
        self.assertEqual((first["xp_earned"], second["xp_earned"]), (10, 0))
        self.assertEqual(self.db.execute("SELECT xp FROM users WHERE id=1").fetchone()[0], 10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts WHERE user_id=1").fetchone()[0], 2)

    def test_revealed_answer_is_server_tracked_not_client_controlled(self):
        self.db.execute("UPDATE tasks SET explanation=? WHERE id='visible'", (json.dumps(["Answer explanation"]),))
        original_cfg = main.cfg

        def test_cfg(key):
            if key == "reveal_counts_as_miss":
                return True
            if key == "hearts_enabled":
                return False
            return original_cfg(key)

        access = {"daily_limit": None, "features": {"hints": True, "practice": True}}
        with patch.object(main, "cfg", side_effect=test_cfg), patch.object(main.billing, "access", return_value=access):
            main.hint("visible", 2, self.user, self.db)
            result = main.answer(main.Ans(task_id="visible", answer=0, revealed=False), self.user, self.db)

        self.assertTrue(result["helped"])
        self.assertEqual(result["xp_earned"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM revealed_answers").fetchone()[0], 0)

    def test_revealed_answer_marker_survives_wrong_attempt_until_correct_attempt(self):
        self.db.execute("UPDATE tasks SET explanation=? WHERE id='visible'", (json.dumps(["Answer explanation"]),))
        original_cfg = main.cfg

        def test_cfg(key):
            if key == "reveal_counts_as_miss":
                return True
            if key == "hearts_enabled":
                return False
            return original_cfg(key)

        access = {"daily_limit": None, "features": {"hints": True, "practice": True}}
        with patch.object(main, "cfg", side_effect=test_cfg), patch.object(main.billing, "access", return_value=access):
            main.hint("visible", 2, self.user, self.db)
            wrong = main.answer(main.Ans(task_id="visible", answer=1, revealed=False), self.user, self.db)
            correct = main.answer(main.Ans(task_id="visible", answer=0, revealed=False), self.user, self.db)

        self.assertFalse(wrong["correct"])
        self.assertTrue(correct["helped"])
        self.assertEqual(correct["xp_earned"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM revealed_answers").fetchone()[0], 0)

    def test_perfect_session_bonus_is_idempotent_for_the_day(self):
        self.db.executemany("INSERT INTO attempts(user_id, task_id, correct, ts) VALUES(1, ?, 1, ?)",
                            [(f"perfect-{i}", int(time.time()) + i) for i in range(5)])
        first = main.finish(main.Fin(total=5), self.user, self.db)
        second = main.finish(main.Fin(total=5), self.user, self.db)
        self.assertGreater(first["bonus"], 0)
        self.assertEqual(second["bonus"], 0)

    def test_repeating_one_task_does_not_count_as_perfect_session(self):
        now = int(time.time())
        self.db.executemany("INSERT INTO attempts(user_id, task_id, correct, ts) VALUES(1, 'visible', 1, ?)",
                            [(now + i,) for i in range(5)])
        result = main.finish(main.Fin(total=5), self.user, self.db)
        self.assertEqual(result["bonus"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM perfect_claims WHERE user_id=1").fetchone()[0], 0)

    def test_mastered_error_returns_for_delayed_reinforcement(self):
        task_id = "visible"
        now = 1_800_000_000
        self.db.execute("INSERT INTO errors(user_id,task_id,mistakes,stage,next_review,status) VALUES(1,?,1,4,?,'mastered')",
                        (task_id, now - 1))
        with patch.object(main.time, "time", return_value=now), \
             patch.object(main, "cfg", side_effect=lambda key: [30, 60, 90] if key == "error_reinforcement_days" else [1, 3, 7, 14] if key == "error_steps_days" else main.cfg(key)):
            main.review_error(self.db, 1, task_id, True)
        row = self.db.execute("SELECT stage,next_review,status FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "mastered")
        self.assertEqual(row["next_review"], now + 60 * main.DAY)
        self.assertEqual(row["stage"], 5)

    def test_full_error_cycle_reinforces_after_mastering_and_resets_on_mistake(self):
        task_id = "visible"
        now = 1_810_000_000
        with patch.object(main, "cfg", side_effect=lambda key: [1, 3, 7, 14] if key == "error_steps_days" else [30, 60, 90] if key == "error_reinforcement_days" else None), \
             patch.object(main.time, "time", return_value=now):
            main.review_error(self.db, 1, task_id, False)
            expected_delays = [1, 3, 7]
            for stage, delay in enumerate(expected_delays, start=1):
                next_review = self.db.execute("SELECT next_review FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()[0]
                with patch.object(main.time, "time", return_value=next_review):
                    main.review_error(self.db, 1, task_id, True)
                row = self.db.execute("SELECT stage,next_review,status FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()
                self.assertEqual(row["stage"], stage)
                self.assertEqual(row["status"], "reviewing")
                self.assertEqual(row["next_review"], next_review + [3, 7, 14][stage - 1] * main.DAY)

            next_review = self.db.execute("SELECT next_review FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()[0]
            with patch.object(main.time, "time", return_value=next_review):
                main.review_error(self.db, 1, task_id, True)
            row = self.db.execute("SELECT stage,next_review,status FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()
            self.assertEqual((row["stage"], row["status"]), (4, "mastered"))
            self.assertEqual(row["next_review"], next_review + 30 * main.DAY)

            next_review = self.db.execute("SELECT next_review FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()[0]
            with patch.object(main.time, "time", return_value=next_review):
                main.review_error(self.db, 1, task_id, True)
            row = self.db.execute("SELECT stage,next_review,status FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()
            self.assertEqual((row["stage"], row["status"]), (5, "mastered"))
            self.assertEqual(row["next_review"], next_review + 60 * main.DAY)

            with patch.object(main.time, "time", return_value=next_review + 1):
                main.review_error(self.db, 1, task_id, False)
            row = self.db.execute("SELECT stage,status,mistakes FROM errors WHERE user_id=1 AND task_id=?", (task_id,)).fetchone()
            self.assertEqual((row["stage"], row["status"], row["mistakes"]), (0, "failed", 2))

    def test_error_reinforcement_batch_is_capped_and_prioritizes_due(self):
        now = int(time.time())
        for number in range(12):
            task_id = f"remember-{number}"
            main.put_task(self.db, {
                "id": task_id, "subject": "rus", "type": "text", "question": "Recall", "answer": "ok",
                "verified": True, "topic": "Recall", "year": 2020, "variant": 1, "num": f"A{number+1}", "part": "A",
            })
            status = "mastered" if number < 3 else "failed"
            due = now - 10 if number in (0, 1, 3) else now + 86400 + number
            self.db.execute("INSERT INTO errors(user_id,task_id,mistakes,stage,next_review,status) VALUES(1,?,1,4,?,?)",
                            (task_id, due, status))
        features = {feature: True for feature in billing.FEATURES}
        with patch.object(main.billing, "access", return_value={"features": features, "daily_limit": None}), \
             patch.object(main, "cfg", side_effect=lambda key: True if key == "hearts_enabled" else None), \
             patch.object(main.time, "time", return_value=now):
            result = main.session(mode="errors_all", n=50, u=self.user, c=self.db)
            summary = main.errors_list(self.user, self.db)
        returned_ids = [task["id"] for task in result["tasks"]]
        self.assertEqual(len(returned_ids), 10)
        self.assertEqual(returned_ids[:3], ["remember-0", "remember-1", "remember-3"])
        self.assertEqual(summary["open"], 9)
        self.assertEqual(summary["reinforcement_due"], 2)
        self.assertEqual(summary["due"], 1)

    def test_general_diag_session_samples_topics_across_subjects(self):
        for number in range(12):
            subject = "rus" if number % 2 == 0 else "math"
            topic = f"T{number % 5}"
            task_id = f"diag-{number}"
            main.put_task(self.db, {
                "id": task_id,
                "subject": subject,
                "topic": topic,
                "type": "choice",
                "question": f"Diag {number}",
                "options": ["correct", "wrong"],
                "answer": 0,
                "explanation": [],
                "verified": True,
            })
        result = main.session(mode="diag", subject="", n=10, u=self.user, c=self.db)
        self.assertEqual(len(result["tasks"]), 10)
        self.assertEqual({task["subject"] for task in result["tasks"]}, {"rus", "math"})
        self.assertEqual(len({(task["subject"], task["topic"]) for task in result["tasks"]}), 10)

    def test_wrong_answer_from_diagnostic_enters_error_review_cycle(self):
        now = int(time.time())
        features = {feature: True for feature in billing.FEATURES}
        with patch.object(main.billing, "access", return_value={"features": features, "daily_limit": None}), \
             patch.object(main, "cfg", side_effect=lambda key: False if key == "hearts_enabled" else settings.cfg(key)), \
             patch.object(main.time, "time", return_value=now):
            session = main.session(mode="diag", u=self.user, c=self.db)
            self.assertEqual([task["id"] for task in session["tasks"]], ["visible"])
            main.answer(main.Ans(task_id="visible", answer=1), self.user, self.db)

        error = self.db.execute("SELECT status,next_review FROM errors WHERE user_id=1 AND task_id='visible'").fetchone()
        self.assertEqual(error["status"], "failed")
        self.assertGreater(error["next_review"], now)

    def test_subject_diagnostic_scores_coverage_and_persists_result(self):
        for number in range(12):
            main.put_task(self.db, {
                "id": f"subject-diag-{number}", "subject": "rus", "topic": f"Topic {number}",
                "type": "choice", "question": "Question", "options": ["yes", "no"],
                "answer": 0, "explanation": [], "verified": True,
            })
        features = {feature: True for feature in billing.FEATURES}
        with patch.object(main.billing, "access", return_value={"features": features, "daily_limit": None}), \
             patch.object(main, "cfg", side_effect=lambda key: False if key == "hearts_enabled" else settings.cfg(key)):
            started = main.diagnostic_start(main.DiagStart(subject="rus"), self.user, self.db)

        self.assertEqual(len(started["tasks"]), 10)
        self.assertEqual(len({task["topic"] for task in started["tasks"]}), 10)
        task_ids = [task["id"] for task in started["tasks"]]
        self.db.executemany("INSERT INTO attempts(user_id,task_id,correct,ts) VALUES(1,?,?,?)",
                            [(task_id, int(index < 7), int(time.time())) for index, task_id in enumerate(task_ids)])
        result = main.diagnostic_finish(main.DiagFinish(diagnostic_id=started["diagnostic_id"]), self.user, self.db)

        self.assertEqual((result["correct"], result["answered"], result["total"]), (7, 10, 10))
        self.assertEqual(result["score"], 62)
        self.assertEqual(self.db.execute("SELECT score FROM subject_scores WHERE user_id=1 AND subject='rus'").fetchone()[0], 62)

    def test_subject_score_moves_gradually_after_practice_answer(self):
        self.db.execute("INSERT INTO subject_scores(user_id,subject,score,attempts,correct,updated_ts) VALUES(1,'rus',50,10,5,?)",
                        (int(time.time()),))
        main.answer(main.Ans(task_id="visible", answer=0), self.user, self.db)
        after_correct = self.db.execute("SELECT score FROM subject_scores WHERE user_id=1 AND subject='rus'").fetchone()[0]
        main.answer(main.Ans(task_id="visible", answer=1), self.user, self.db)
        after_wrong = self.db.execute("SELECT score FROM subject_scores WHERE user_id=1 AND subject='rus'").fetchone()[0]

        self.assertEqual(after_correct, 51)
        self.assertEqual(after_wrong, 50)

    def test_exam_cannot_save_task_from_another_variant(self):
        now = int(time.time())
        self.db.execute("INSERT INTO exams(user_id, subject, year, variant, started, deadline) VALUES(1, 'rus', 2020, 1, ?, ?)",
                         (now, now + 300))
        with self.assertRaises(HTTPException) as error:
            main.exam_save(main.ExamSave(exam_id=1, task_id="visible", answer=0), self.user, self.db)
        self.assertEqual(error.exception.status_code, 404)

    def test_exam_rejects_saves_after_deadline(self):
        now = int(time.time())
        main.put_task(self.db, {
            "id": "exam-task",
            "subject": "rus",
            "type": "choice",
            "question": "Exam question",
            "options": ["correct", "wrong"],
            "answer": 0,
            "explanation": [],
            "verified": True,
            "year": 2020,
            "variant": 1,
            "num": "A1",
            "part": "A",
        })
        self.db.execute("INSERT INTO exams(user_id, subject, year, variant, started, deadline) VALUES(1, 'rus', 2020, 1, ?, ?)",
                        (now - 120, now - 60))
        with self.assertRaises(HTTPException) as error:
            main.exam_save(main.ExamSave(exam_id=1, task_id="exam-task", answer=0), self.user, self.db)
        self.assertEqual(error.exception.status_code, 400)

    def test_exam_finish_is_atomic_and_idempotent(self):
        now = int(time.time())
        task_ids = []
        for number in range(1, 6):
            task_id = f"finish-{number}"
            task_ids.append(task_id)
            main.put_task(self.db, {
                "id": task_id,
                "subject": "rus",
                "type": "choice",
                "question": "Exam question",
                "options": ["correct", "wrong"],
                "answer": 0,
                "explanation": [],
                "verified": True,
                "year": 2020,
                "variant": 1,
                "num": f"A{number}",
                "part": "A",
            })
        self.db.execute("INSERT INTO exams(user_id, subject, year, variant, started) VALUES(1, 'rus', 2020, 1, ?)", (now,))
        self.db.executemany("INSERT INTO exam_answers(exam_id, task_id, answer) VALUES(1, ?, '0')", [(task_id,) for task_id in task_ids])
        first = main.exam_finish(main.ExamFin(exam_id=1), self.user, self.db)
        second = main.exam_finish(main.ExamFin(exam_id=1), self.user, self.db)
        self.assertEqual(first["correct"], 5)
        self.assertEqual(second["correct"], 5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts WHERE user_id=1").fetchone()[0], 5)
        self.assertEqual(self.db.execute("SELECT xp FROM users WHERE id=1").fetchone()[0], 25)

    def test_repeating_exam_does_not_reward_same_tasks_twice_per_day(self):
        now = int(time.time())
        task_ids = []
        for number in range(1, 6):
            task_id = f"exam-{number}"
            task_ids.append(task_id)
            main.put_task(self.db, {
                "id": task_id,
                "subject": "rus",
                "type": "choice",
                "question": "Exam question",
                "options": ["correct", "wrong"],
                "answer": 0,
                "explanation": [],
                "verified": True,
                "year": 2020,
                "variant": 1,
                "num": f"A{number}",
                "part": "A",
            })
        for exam_id in (1, 2):
            self.db.execute("INSERT INTO exams(id, user_id, subject, year, variant, started) VALUES(?, 1, 'rus', 2020, 1, ?)",
                             (exam_id, now + exam_id))
            self.db.executemany("INSERT INTO exam_answers(exam_id, task_id, answer) VALUES(?,?,?)",
                                [(exam_id, task_id, json.dumps(0)) for task_id in task_ids])
            exam = self.db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
            main.grade_exam(self.db, exam)
        self.assertEqual(self.db.execute("SELECT xp FROM users WHERE id=1").fetchone()[0], 25)

    def test_telegram_init_data_rejects_future_timestamp_and_duplicate_fields(self):
        def signed_init(auth_date):
            data = {"auth_date": str(auth_date), "user": json.dumps({"id": 123, "first_name": "Tester"})}
            check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
            secret = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
            data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            return urlencode(data)

        with patch.object(main, "BOT_TOKEN", "test-token"):
            self.assertEqual(main.verify(signed_init(int(time.time())))["id"], 123)
            with self.assertRaises(HTTPException):
                main.verify(signed_init(int(time.time()) + 3600))
            with self.assertRaises(HTTPException):
                main.verify(signed_init(int(time.time())) + "&user=duplicate")

    def test_admin_panel_requires_configured_secret(self):
        from starlette.requests import Request
        request = Request({"type": "http", "scheme": "http", "server": ("localhost", 8000), "path": "/", "root_path": "",
                   "query_string": b"", "http_version": "1.1", "method": "GET",
                           "client": ("127.0.0.1", 12345), "headers": [(b"host", b"localhost:8000")]})
        with patch.object(main, "ADMIN_PANEL_TOKEN", "admin-secret"):
            with self.assertRaises(HTTPException) as error:
                main.require_admin(request, "wrong-secret")
            self.assertEqual(error.exception.status_code, 404)
            self.assertIsNone(main.require_admin(request, "admin-secret"))

    def test_admin_panel_rejects_non_local_host_even_with_secret(self):
        from starlette.requests import Request
        request = Request({"type": "http", "scheme": "https", "server": ("example.com", 443), "path": "/", "root_path": "",
                   "query_string": b"", "http_version": "1.1", "method": "GET",
                           "client": ("127.0.0.1", 12345), "headers": [(b"host", b"example.com")]})
        with patch.object(main, "ADMIN_PANEL_TOKEN", "admin-secret"):
            with self.assertRaises(HTTPException) as error:
                main.require_admin(request, "admin-secret")
            self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as page_error:
            main.admin_page(request)
        self.assertEqual(page_error.exception.status_code, 404)
        import asyncio
        static_request = Request({**request.scope, "path": "/admin.html"})
        with patch.object(main, "is_local_admin_host", return_value=False):
            response = asyncio.run(main.prevent_admin_caching(static_request, lambda _: None))
        self.assertEqual(response.status_code, 404)

    def test_admin_panel_rejects_missing_secret_even_on_local_host(self):
        from starlette.requests import Request
        request = Request({"type": "http", "scheme": "http", "server": ("localhost", 8000), "path": "/", "root_path": "",
                   "query_string": b"", "http_version": "1.1", "method": "GET",
                           "client": ("127.0.0.1", 12345), "headers": [(b"host", b"localhost:8000")]})
        with patch.object(main, "ADMIN_PANEL_TOKEN", ""):
            with self.assertRaises(HTTPException) as error:
                main.require_admin(request, "")
            self.assertEqual(error.exception.status_code, 404)

    def test_admin_overview_aggregates_real_platform_metrics(self):
        now = int(time.time())
        self.db.execute("INSERT INTO attempts(user_id,task_id,correct,ts) VALUES(1,'visible',1,?)", (now,))
        overview = main.admin_overview(from_date="", to_date="", c=self.db)
        self.assertEqual(overview["users"], 1)
        self.assertEqual(overview["active_today"], 1)
        self.assertEqual(overview["attempts_today"], 1)
        self.assertEqual(overview["accuracy_period"], 100)

    def test_admin_users_search_returns_access_and_activity(self):
        now = int(time.time())
        self.db.execute("UPDATE users SET trial_until=? WHERE id=1", (now + 86400,))
        self.db.execute("INSERT INTO attempts(user_id,task_id,correct,ts) VALUES(1,'visible',1,?)", (now,))
        result = main.admin_users(q="Tester", page=1, from_date="", to_date="", c=self.db)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["users"][0]["name"], "Tester")
        self.assertEqual(result["users"][0]["attempts"], 1)
        self.assertEqual(result["users"][0]["access"], "trial")

    def test_admin_registers_return_filtered_rows_from_database(self):
        now = int(time.time())
        self.db.execute("INSERT INTO attempts(user_id,task_id,correct,ts) VALUES(1,'visible',1,?)", (now,))
        tasks = main.admin_tasks(q="Test question", subject="rus", verified="yes", task_type="", year=0, page=1, c=self.db)
        exams = main.admin_exams(q="", subject="", status="all", page=1, from_date="", to_date="", c=self.db)
        payments = main.admin_payments(q="", status="all", page=1, from_date="", to_date="", c=self.db)
        reports = main.admin_reports(q="", subject="", page=1, from_date="", to_date="", c=self.db)
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(tasks["tasks"][0]["id"], "visible")
        self.assertEqual((exams["total"], payments["total"], reports["total"]), (0, 0, 0))

    def test_admin_sort_fields_reject_sql_fragments(self):
        with self.assertRaises(HTTPException) as error:
            main.admin_users(q="", page=1, sort="id; DROP TABLE users", from_date="", to_date="", c=self.db)
        self.assertEqual(error.exception.status_code, 400)

    def test_session_question_count_is_bounded_in_api_schema(self):
        params = main.app.openapi()["paths"]["/api/session"]["get"]["parameters"]
        count = next(param["schema"] for param in params if param["name"] == "n")
        self.assertEqual((count["minimum"], count["maximum"]), (1, 20))
    def test_new_user_report_is_sent_once_on_first_authenticated_login(self):
        telegram_user = {"id": 123, "first_name": "New Student", "username": "new_student"}
        with patch.object(main, "DEV", False), patch.object(main, "BOT_TOKEN", "bot-token"), \
             patch.object(main, "ADMIN_ID", 456), patch.object(main, "verify", return_value=telegram_user), \
             patch.object(main, "refill"), patch.object(main.billing, "notify_new_user") as report:
            main.auth("signed-init-data", self.db)
            main.auth("signed-init-data", self.db)

        report.assert_called_once_with("bot-token", 456, 123, "New Student", "new_student")

    def test_new_user_report_contains_username_and_id(self):
        with patch.object(billing, "notify") as send:
            billing.notify_new_user("bot-token", 456, 123, "  New  Student ", "new_student")
        send.assert_called_once_with("bot-token", 456,
                                     "Новый пользователь Mini App\nИмя: New Student\nНик: @new_student\nTelegram ID: 123")

    def test_invoice_payload_is_bound_to_user_price_and_term(self):
        payload = billing.invoice_payload(123, 50, 45)
        self.assertEqual(billing.parse_invoice_payload(payload), (123, 50, 45))
        self.assertIsNone(billing.parse_invoice_payload("sub:123"))
        self.assertIsNone(billing.parse_invoice_payload("sub:0:50:30"))

    def test_empty_collection_file_is_reported_as_not_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2017.json"
            path.write_text("", encoding="utf-8")
            tasks, notes = main.sborniki.load_file(path)
        self.assertEqual(tasks, [])
        self.assertTrue(any("пустой JSON-файл" in note for note in notes))

    def test_nested_subject_collection_is_loaded_with_subject_from_folder(self):
        source = {"variant_number": 1, "tasks": [{"number": "А1", "text": "Question",
                  "options": ["wrong", "right"], "answer": [2]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks" / "text" / "hist" / "2017.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            rows, notes = main.sborniki.load_all(path.parents[3])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "hist")
        self.assertEqual(rows[0]["year"], 2017)
        self.assertEqual(notes, [])

    def test_startup_sync_does_not_delete_collection_tasks_when_source_invalid(self):
        main.put_task(self.db, {
            "id": "2017-v1-A1", "subject": "rus", "type": "text",
            "question": "Task from collection", "answer": "answer", "year": 2017,
            "variant": 1, "num": "A1", "part": "A", "verified": True,
        })
        main.prune_missing_tasks(self.db, set(), sources_valid=False)
        self.assertIsNotNone(self.db.execute("SELECT id FROM tasks WHERE id='2017-v1-A1'").fetchone())

    def test_trial_period_is_anchored_and_does_not_change_with_settings(self):
        now = int(time.time())
        base_config = {"price_stars": 50, "trial_days": 7, "paid_days": 45, "paywall_enabled": True,
                       "free_plan": {"daily_tasks": 10, "sborniki": False, "exam": False, "errors": False,
                                     "hints": False, "score": False}}
        with patch.object(billing.settings, "get", return_value=base_config):
            first_access = billing.access(self.db, 1, now=now)
        trial_until = self.db.execute("SELECT trial_until FROM users WHERE id=1").fetchone()[0]
        self.assertEqual(first_access["state"], "trial")
        self.assertEqual(first_access["days_left"], 7)
        longer_config = {**base_config, "trial_days": 30}
        with patch.object(billing.settings, "get", return_value=longer_config):
            next_access = billing.access(self.db, 1, now=now)
        self.assertEqual(next_access["state"], "trial")
        self.assertEqual(next_access["days_left"], 7)
        self.assertEqual(self.db.execute("SELECT trial_until FROM users WHERE id=1").fetchone()[0], trial_until)

    def test_payment_grant_rolls_back_if_access_update_fails(self):
        self.db.execute("""
            CREATE TRIGGER fail_paid_until BEFORE UPDATE OF paid_until ON users
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
        with patch.object(billing.settings, "cfg", return_value=30):
            with self.assertRaises(sqlite3.IntegrityError):
                billing.grant(self.db, 77, 50, "charge-retry", days=30)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payments WHERE charge_id='charge-retry'").fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT id FROM users WHERE id=77").fetchone())
        self.db.execute("DROP TRIGGER fail_paid_until")
        with patch.object(billing.settings, "cfg", return_value=30):
            granted_until = billing.grant(self.db, 77, 50, "charge-retry", days=30)
        self.assertGreater(granted_until, int(time.time()))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payments WHERE charge_id='charge-retry'").fetchone()[0], 1)

    def test_referral_rolls_back_both_bonuses_and_claim(self):
        now = int(time.time())
        self.db.execute("INSERT INTO users(id, name, hearts_ts, created_ts) VALUES(2, 'Friend', ?, ?)", (now, now))
        self.db.execute("CREATE TABLE referrals(user_id INTEGER PRIMARY KEY, referrer INT, ts INT)")
        self.db.execute("""
            CREATE TRIGGER fail_referral_bonus BEFORE UPDATE OF bonus_until ON users
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
        with patch.object(billing.settings, "cfg", return_value=3):
            with self.assertRaises(sqlite3.IntegrityError):
                billing.referral(self.db, 1, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM referrals").fetchone()[0], 0)
        bonuses = [tuple(row) for row in self.db.execute("SELECT bonus_until FROM users WHERE id IN (1, 2)").fetchall()]
        self.assertEqual(bonuses, [(0,), (0,)])
        self.db.execute("DROP TRIGGER fail_referral_bonus")
        with billing.transaction(self.db):
            self.assertTrue(billing.referral(self.db, 1, 2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM referrals WHERE user_id=1").fetchone()[0], 1)

    def test_refund_rolls_back_flag_if_access_revoke_fails(self):
        with patch.object(billing.settings, "cfg", return_value=5):
            billing.grant(self.db, 1, 50, "refundable", days=5)
        self.db.execute("""
            CREATE TRIGGER fail_revoke BEFORE UPDATE OF paid_until ON users
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            billing.revoke(self.db, "refundable")
        self.assertEqual(self.db.execute("SELECT refunded FROM payments WHERE charge_id='refundable'").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER fail_revoke")
        self.assertEqual(billing.revoke(self.db, "refundable"), 1)
        self.assertEqual(self.db.execute("SELECT refunded FROM payments WHERE charge_id='refundable'").fetchone()[0], 1)

    def test_exam_feature_gate_rejects_expired_access(self):
        with patch.object(main.billing, "access", return_value={"features": {"exam": False}}):
            with self.assertRaises(HTTPException) as error:
                main.need("exam")(self.user, self.db)
        self.assertEqual(error.exception.status_code, 402)

    def test_invalid_settings_root_retains_last_good_values(self):
        good_config = {**settings.DEFAULTS, "paywall_enabled": False, "free_plan": {**settings.DEFAULTS["free_plan"], "daily_tasks": 7}}
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "settings.json"
            config_path.write_text(json.dumps({"paywall_enabled": False, "free_plan": {"daily_tasks": 7}}), encoding="utf-8")
            with patch.object(settings, "PATH", config_path), \
                  patch.object(settings, "_cache", {"mtime": None, "invalid_mtime": None, "data": settings.DEFAULTS.copy()}):
                loaded = settings.get()
                self.assertFalse(loaded["paywall_enabled"])
                self.assertEqual(loaded["free_plan"]["daily_tasks"], 7)
                last_good_mtime = settings._cache["mtime"]

                for invalid in ([], None):
                    last_good_mtime += 2
                    config_path.write_text(json.dumps(invalid), encoding="utf-8")
                    os.utime(config_path, (last_good_mtime, last_good_mtime))
                    self.assertEqual(settings.get(), good_config)

                config_path.write_text(json.dumps({
                    "price_stars": "free",
                    "free_plan": {"daily_tasks": "unlimited", "exam": "yes"},
                }), encoding="utf-8")
                last_good_mtime += 2
                os.utime(config_path, (last_good_mtime, last_good_mtime))
                sanitized = settings.get()
                self.assertEqual(sanitized["price_stars"], settings.DEFAULTS["price_stars"])
                self.assertEqual(sanitized["free_plan"]["daily_tasks"], settings.DEFAULTS["free_plan"]["daily_tasks"])
                self.assertIs(sanitized["free_plan"]["exam"], settings.DEFAULTS["free_plan"]["exam"])

    def test_disabled_paywall_does_not_create_a_bill(self):
        with patch.object(main, "cfg", side_effect=lambda key: False if key == "paywall_enabled" else None):
            with self.assertRaises(HTTPException) as error:
                main.pay(self.user, self.db)
        self.assertEqual(error.exception.status_code, 403)

    def test_foundation_content_has_unique_ids_and_valid_answer_keys(self):
        path = Path(__file__).resolve().parents[1] / "tasks" / "fundamentals.json"
        tasks = json.loads(path.read_text(encoding="utf-8"))
        identifiers = [task["id"] for task in tasks]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(all(identifier.isascii() for identifier in identifiers))
        self.assertTrue(all(main.is_right(task["type"], task["answer"], task["answer"]) for task in tasks))
        self.assertEqual({task["subject"] for task in tasks}, {"math", "phys", "chem", "bio", "hist"})

    def test_legacy_schema_migrates_before_creating_indexes(self):
        legacy = sqlite3.connect(":memory:")
        legacy.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE tasks(id TEXT PRIMARY KEY, exam TEXT, subject TEXT, topic TEXT, level INT, type TEXT,
                question TEXT, options TEXT, answer TEXT, explanation TEXT, source TEXT, verified INT DEFAULT 0,
                distractors TEXT DEFAULT '{}', passage TEXT DEFAULT '');
            CREATE TABLE exams(id INTEGER PRIMARY KEY, user_id INT, year INT, variant INT, started INT,
                deadline INT DEFAULT 0, finished INT DEFAULT 0, correct INT DEFAULT 0, total INT DEFAULT 0,
                points REAL DEFAULT 0, max_points REAL DEFAULT 0, score INT DEFAULT 0);
        """)
        legacy.executescript(main.SCHEMA)
        main.migrate(legacy)
        task_columns = {row[1] for row in legacy.execute("PRAGMA table_info(tasks)")}
        exam_columns = {row[1] for row in legacy.execute("PRAGMA table_info(exams)")}
        indexes = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("year", task_columns)
        self.assertIn("subject", exam_columns)
        self.assertIn("idx_tasks_subject_variant", indexes)
        legacy.close()


if __name__ == "__main__":
    unittest.main()