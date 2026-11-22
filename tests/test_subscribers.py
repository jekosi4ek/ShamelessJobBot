"""Тести модуля subscribers на SQLite в пам'яті, без мережі.
Запуск: python -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import datetime, date

import pytz

os.environ["ADMIN_CHAT_ID"] = "1"
os.environ.pop("SUBSCRIBER_EXCLUDED_COMPANIES", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlalchemy  # noqa: E402
import subscribers as s  # noqa: E402

PRAGUE = pytz.timezone("Europe/Prague")
ADMIN = 1


def prague(y, mo, d, h, mi):
    return PRAGUE.localize(datetime(y, mo, d, h, mi))


# 04.12.2026 — п'ятниця
FRI_NIGHT = (prague(2026, 12, 4, 1, 45), prague(2026, 12, 4, 6, 45))


class ParsingTests(unittest.TestCase):
    def test_days(self):
        self.assertEqual(s.parse_days("пн-пт"), [1, 2, 3, 4, 5])
        self.assertEqual(s.parse_days("сб,нд"), [6, 7])
        self.assertEqual(s.parse_days("пт-пн"), [1, 5, 6, 7])
        self.assertEqual(s.parse_days("будні"), [1, 2, 3, 4, 5])
        self.assertEqual(s.parse_days("pá"), [5])
        self.assertEqual(s.parse_days("п’ятниця"), [5])
        with self.assertRaises(ValueError):
            s.parse_days("xx")

    def test_time_range(self):
        self.assertEqual(s.parse_time_range("08:00-18:00"), (480, 1080))
        self.assertEqual(s.parse_time_range("8-18"), (480, 1080))
        self.assertEqual(s.parse_time_range("22:00-07:00"), (1320, 420))
        self.assertEqual(s.parse_time_range("00:00-24:00"), (0, 1440))
        for bad in ("8:00", "25-26", "10-10", "8:61-9"):
            with self.assertRaises(ValueError):
                s.parse_time_range(bad)

    def test_schedule_args(self):
        self.assertEqual(s.parse_schedule_args(["сб,", "нд", "10-14"]), ([6, 7], 600, 840))


class MatchingTests(unittest.TestCase):
    def test_no_windows_matches_everything(self):
        self.assertTrue(s.shift_matches(*FRI_NIGHT, []))

    def test_same_day_window(self):
        self.assertTrue(s.shift_matches(*FRI_NIGHT, [(5, 0, 480)]))
        self.assertFalse(s.shift_matches(*FRI_NIGHT, [(5, 120, 480)]))  # починається раніше вікна
        self.assertFalse(s.shift_matches(*FRI_NIGHT, [(5, 0, 360)]))    # закінчується пізніше
        self.assertFalse(s.shift_matches(*FRI_NIGHT, [(4, 0, 480)]))    # інший день

    def test_overnight_window_covers_next_morning(self):
        self.assertTrue(s.shift_matches(*FRI_NIGHT, [(4, 1320, 420)]))   # чт 22:00-07:00
        self.assertFalse(s.shift_matches(*FRI_NIGHT, [(4, 1320, 360)]))  # чт 22:00-06:00

    def test_shift_crossing_midnight(self):
        shift = (prague(2026, 12, 4, 22, 0), prague(2026, 12, 5, 6, 0))  # пт 22:00 - сб 06:00
        self.assertTrue(s.shift_matches(*shift, [(5, 1260, 420)]))  # пт 21:00-07:00
        self.assertFalse(s.shift_matches(*shift, [(5, 1260, 1440)]))  # пт 21:00-24:00

    def test_missing_time(self):
        self.assertTrue(s.shift_matches(None, None, []))
        self.assertFalse(s.shift_matches(None, None, [(5, 0, 1440)]))

    def test_company(self):
        self.assertTrue(s.company_matches(10, None))
        self.assertTrue(s.company_matches(10, {10, 20}))
        self.assertFalse(s.company_matches(30, {10, 20}))
        self.assertFalse(s.company_matches(555, None))      # виключена, як і в каналі
        self.assertFalse(s.company_matches(555, {555}))


class BotFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlalchemy.create_engine("sqlite://")
        with self.engine.begin() as c:
            c.execute(sqlalchemy.text(
                "CREATE TABLE positions_v4 (id INTEGER PRIMARY KEY, date DATE, position_id INTEGER, "
                "company VARCHAR(255), company_id INTEGER)"))
            today = date.today().isoformat()
            for i, (cid, name) in enumerate([(10, "Alza"), (20, "Albert Logistika"), (555, "Party"), (10, "Alza")]):
                c.execute(sqlalchemy.text("INSERT INTO positions_v4 VALUES (:i, :d, :i, :n, :c)"),
                          {"i": i, "d": today, "n": name, "c": cid})
        s.init(self.engine, "TEST")
        self.sent = []
        s._api = self._fake_api

    channel_admins = {}  # user_id -> status у адмін-каналі

    def tearDown(self):
        s.ADMIN_CHANNEL_ID = ""
        s._admin_cache.clear()

    def _fake_api(self, method, **params):
        self.sent.append((method, params))
        if method == "getChatMember":
            status = self.channel_admins.get(params["user_id"])
            if status is None:
                return {"ok": False, "error_code": 400, "description": "Bad Request: user not found"}
            return {"ok": True, "result": {"status": status}}
        return {"ok": True, "result": {}}

    def msg(self, chat_id, text, first_name="Petr", username="petr"):
        s.handle_update({"update_id": 1, "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": first_name, "username": username},
            "text": text}})

    def last_text(self, chat_id):
        for method, p in reversed(self.sent):
            if method == "sendMessage" and str(p["chat_id"]) == str(chat_id):
                return p["text"]
        return None

    def recipients(self, shift=FRI_NIGHT, company=10):
        with s._Session() as db:
            return s.recipients_for(db, *shift, company)

    def test_new_user_alerts_admin_once(self):
        self.msg(100, "/start")
        self.msg(100, "/start")
        alerts = [p for m, p in self.sent if m == "sendMessage" and p["chat_id"] == "1"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Новий підписник", alerts[0]["text"])
        self.assertIn("blk:100", str(alerts[0]["reply_markup"]))

    def test_windows_and_company_filters(self):
        self.msg(100, "/start")
        self.msg(200, "/start")
        self.assertEqual(sorted(self.recipients()), [100, 200])

        self.msg(100, "/set чт 22:00-07:00")
        self.msg(200, "/set пт 08:00-16:00")
        self.assertEqual(self.recipients(), [100])

        self.msg(100, "/company add 20")
        self.assertEqual(self.recipients(company=10), [])
        self.assertEqual(self.recipients(company=20), [100])

        self.msg(100, "/company add alza")
        self.assertIn("Alza", self.last_text(100))
        self.assertEqual(self.recipients(company=10), [100])

        self.msg(100, "/company clear")
        self.msg(200, "/clear")
        self.assertEqual(sorted(self.recipients(company=30)), [100, 200])
        self.assertEqual(self.recipients(company=555), [])

    def test_company_add_unknown_and_excluded(self):
        self.msg(100, "/start")
        self.msg(100, "/company add 999")
        self.assertIn("не знайдено", self.last_text(100))
        self.msg(100, "/companies")
        self.assertNotIn("Party", self.last_text(100))
        self.assertIn("Alza", self.last_text(100))

    def test_pause_resume(self):
        self.msg(100, "/start")
        self.msg(100, "/pause")
        self.assertEqual(self.recipients(), [])
        self.msg(100, "/resume")
        self.assertEqual(self.recipients(), [100])

    def test_admin_block_by_command_and_button(self):
        self.msg(100, "/start")
        self.msg(ADMIN, "/block 100")  # адмін, що написав боту, теж стає підписником
        self.assertIn("Заблоковано", self.last_text(ADMIN))
        self.assertEqual(self.recipients(), [ADMIN])

        self.msg(100, "/resume")  # користувач не може зняти блокування сам
        self.assertEqual(self.last_text(100), "Доступ закрито.")
        self.assertEqual(self.recipients(), [ADMIN])

        s.handle_update({"update_id": 2, "callback_query": {
            "id": "cb", "from": {"id": ADMIN}, "data": "unb:100",
            "message": {"chat": {"id": ADMIN}, "message_id": 5}}})
        self.assertEqual(sorted(self.recipients()), [ADMIN, 100])

        self.msg(ADMIN, f"/block {ADMIN}")
        self.assertEqual(self.last_text(ADMIN), "Адміна заблокувати не можна.")

    def test_admin_channel(self):
        s.ADMIN_CHANNEL_ID = "-1001234"
        self.channel_admins = {42: "administrator", 43: "member"}

        self.msg(100, "/start")
        alerts = [p for m, p in self.sent if m == "sendMessage" and p["chat_id"] == "-1001234"]
        self.assertEqual(len(alerts), 1)  # сповіщення йде в канал, не в особистий чат
        self.assertFalse([p for m, p in self.sent if m == "sendMessage" and p["chat_id"] == "1"])

        # звичайний учасник каналу не може блокувати
        s.handle_update({"update_id": 5, "callback_query": {
            "id": "cb", "from": {"id": 43}, "data": "blk:100", "message": {}}})
        self.assertEqual(self.recipients(), [100])

        # адмін каналу натискає кнопку під сповіщенням у каналі
        s.handle_update({"update_id": 6, "callback_query": {
            "id": "cb", "from": {"id": 42}, "data": "blk:100",
            "message": {"chat": {"id": -1001234}, "message_id": 9}}})
        self.assertEqual(self.recipients(), [])

        # адмін каналу може виконувати /users в особистому чаті; адміна каналу заблокувати не можна
        self.msg(42, "/users")
        self.assertIn("Підписники:", self.last_text(42))
        self.msg(ADMIN, "/block 42")
        self.assertEqual(self.last_text(ADMIN), "Адміна заблокувати не можна.")
        # новий адмін каналу не породжує сповіщення про «нового підписника»
        self.assertEqual(len([p for m, p in self.sent if m == "sendMessage" and p["chat_id"] == "-1001234"]), 1)

    def test_non_admin_cannot_block(self):
        self.msg(100, "/start")
        self.msg(200, "/start")
        self.msg(200, "/block 100")
        self.assertEqual(sorted(self.recipients()), [100, 200])
        s.handle_update({"update_id": 3, "callback_query": {
            "id": "cb", "from": {"id": 200}, "data": "blk:100", "message": {}}})
        self.assertEqual(sorted(self.recipients()), [100, 200])

    def test_users_list(self):
        self.msg(100, "/start", first_name="<b>x</b>")
        self.msg(ADMIN, "/users")
        text = self.last_text(ADMIN)
        self.assertIn("Підписники: 2", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    def test_group_messages_ignored(self):
        s.handle_update({"update_id": 4, "message": {
            "chat": {"id": -5, "type": "supergroup"}, "from": {"id": 7}, "text": "/start"}})
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
