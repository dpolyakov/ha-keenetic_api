"""SMS handling tests without a Home Assistant installation."""

import asyncio
import unittest

from test_modem import keenetic


def message(parts=None, total=None, text="Привет", timestamp="Sat Aug 10 21:32:02 2024", read=False):
    data = {"from": "+79990000000", "timestamp": timestamp, "read": read, "text": text}
    if parts is not None:
        data["parts"] = parts
    if total is not None:
        data["total-parts"] = total
    return data


def inbox(messages):
    return keenetic.parse_sms_list({"messages-count": len(messages), "messages": messages})


class SmsParsingTest(unittest.TestCase):
    def test_empty_and_single_message(self):
        self.assertEqual(inbox({}).messages, [])
        result = inbox({"sim-1": message()})
        self.assertEqual(result.unread_count, 1)
        self.assertEqual(result.latest_complete["text"], "Привет")
        self.assertTrue(result.messages[0]["complete"])

    def test_incomplete_parts_hide_text_and_latest_uses_complete_message(self):
        older = message(1, 1, "Готово", "Sat Aug 10 21:30:02 2024", True)
        result = inbox({"sim-1": older, "sim-2": message(3, 4, "Обрывок")})
        self.assertEqual(result.unread_count, 1)
        self.assertIsNone(result.messages[1]["text"])
        self.assertEqual(result.messages[1]["parts"], 3)
        self.assertEqual(result.messages[1]["total_parts"], 4)
        self.assertEqual(result.latest_complete["text"], "Готово")

    def test_missing_or_invalid_part_count_stays_incomplete(self):
        for data in (
            message(1, None), message(None, 2), message("bad", 2),
            message("1", 1), message(True, 1), message(1.5, 1), message(2, 3),
        ):
            with self.subTest(data=data):
                parsed = inbox({"sim-1": data}).messages[0]
                self.assertFalse(parsed["complete"])
                self.assertIsNone(parsed["text"])

    def test_rejects_invalid_inbox(self):
        with self.assertRaises(keenetic.KeeneticSmsError):
            keenetic.parse_sms_list({"messages": []})


class SmsEventTest(unittest.TestCase):
    def setUp(self):
        self.modem = keenetic.KeeneticModemData("UsbQmi0", "modem-1", {})
        self.tracker = keenetic.SmsEventTracker()

    def test_emits_only_after_all_parts_arrive(self):
        self.assertEqual(self.tracker.new_messages(self.modem, inbox({"sim-1": message(1, 3)})), [])
        self.assertEqual(self.tracker.new_messages(self.modem, inbox({"sim-1": message(2, 3)})), [])
        complete = inbox({"sim-1": message(3, 3, "Полный текст")})
        self.assertEqual(self.tracker.new_messages(self.modem, complete)[0]["text"], "Полный текст")
        self.assertEqual(self.tracker.new_messages(self.modem, complete), [])

    def test_initial_messages_are_baselined_and_reused_id_is_new(self):
        first = inbox({"sim-1": message()})
        self.assertEqual(self.tracker.new_messages(self.modem, first), [])
        self.assertEqual(self.tracker.new_messages(self.modem, first), [])
        reused = inbox({"sim-1": message(text="Другое", timestamp="Sun Aug 11 21:32:02 2024")})
        self.assertEqual(len(self.tracker.new_messages(self.modem, reused)), 1)
        self.assertEqual(self.tracker.new_messages(self.modem, reused), [])


class SmsRouterTest(unittest.TestCase):
    def setUp(self):
        self.router = keenetic.Router(None)
        self.calls = []

        async def api(method, endpoint, payload):
            self.calls.append((method, endpoint, payload))
            command = next(iter(payload["sms"]))
            if command == "list":
                return {"sms": {"list": {"messages": {"sim-1": message()}}}}
            return {"sms": {command: {"status": [{"status": "message"}]}}}

        self.router.api = api

    def test_list_mark_and_send_use_rci(self):
        result = asyncio.run(self.router.list_sms("UsbQmi0"))
        self.assertEqual(result.messages[0]["id"], "sim-1")
        asyncio.run(self.router.mark_sms_read("UsbQmi0", "sim-1"))
        asyncio.run(self.router.send_sms("UsbQmi0", "+79990000000", "Привет"))
        self.assertEqual(self.calls[0], ("post", "/rci/", {"sms": {"list": {"interface": "UsbQmi0"}}}))
        self.assertEqual(self.calls[1][2], {"sms": {"read": {"interface": "UsbQmi0", "id": "sim-1"}}})
        self.assertEqual(self.calls[2][2], {"sms": {"send": {"interface": "UsbQmi0", "to": "+79990000000", "message": "Привет"}}})
        self.assertTrue(keenetic.is_sms_request("/rci/", self.calls[2][2]))

    def test_send_error_is_not_retried(self):
        async def failing_api(method, endpoint, payload):
            self.calls.append((method, endpoint, payload))
            return {"sms": {"send": {"status": [{"status": "error"}]}}}

        self.router.api = failing_api
        with self.assertRaises(keenetic.KeeneticSmsError):
            asyncio.run(self.router.send_sms("UsbQmi0", "+79990000000", "Привет"))
        self.assertEqual(len(self.calls), 1)

    def test_command_accepts_top_level_rci_status_and_reports_error(self):
        async def status_api(method, endpoint, payload):
            self.calls.append((method, endpoint, payload))
            return {"status": [{"status": "message", "message": "accepted"}]}

        self.router.api = status_api
        asyncio.run(self.router.send_sms("UsbQmi0", "+79990000000", "Привет"))
        self.assertEqual(len(self.calls), 1)

        async def error_api(method, endpoint, payload):
            return {"status": "error"}

        self.router.api = error_api
        with self.assertRaises(keenetic.KeeneticSmsError):
            asyncio.run(self.router.mark_sms_read("UsbQmi0", "sim-1"))


if __name__ == "__main__":
    unittest.main()
