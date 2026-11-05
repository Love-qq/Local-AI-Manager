import unittest

import local_ai_manager as lam


def plan(**state):
    base = {
        "has_model": True,
        "is_gguf": True,
        "server_ready": True,
        "server_running": False,
        "memory_issue": False,
        "export_state": "exported",
    }
    base.update(state)
    return lam.choose_next_actions(base)


class NextActionTests(unittest.TestCase):
    def test_no_model(self):
        result = plan(has_model=False, is_gguf=False, export_state="none")
        self.assertEqual(result["primary"]["id"], "add_model")
        self.assertEqual([a["id"] for a in result["secondary"]], ["hf_dialog"])

    def test_no_add_folder_action_anywhere(self):
        states = [
            {"has_model": False},
            {"is_gguf": False},
            {"server_ready": False},
            {"memory_issue": True},
            {"export_state": "none"},
        ]
        for state in states:
            result = plan(**state)
            ids = [result["primary"]["id"]] + [a["id"] for a in result["secondary"]]
            self.assertNotIn("add_folder", ids)

    def test_non_gguf_asks_for_gguf(self):
        result = plan(is_gguf=False)
        self.assertEqual(result["primary"]["id"], "add_model")
        self.assertIn("GGUF", result["primary"]["label"])

    def test_server_running(self):
        result = plan(server_running=True)
        self.assertEqual(result["primary"]["id"], "stop")
        self.assertEqual([a["id"] for a in result["secondary"]], ["chat", "api_check"])

    def test_missing_server(self):
        result = plan(server_ready=False)
        self.assertEqual(result["primary"]["id"], "choose_server")
        self.assertEqual([a["id"] for a in result["secondary"]], ["download_llama"])

    def test_memory_issue(self):
        result = plan(memory_issue=True)
        self.assertEqual(result["primary"]["id"], "recommendations")
        self.assertEqual([a["id"] for a in result["secondary"]],
                         ["smaller_ctx", "gpu_setup"])

    def test_stale_export(self):
        result = plan(export_state="stale")
        self.assertEqual(result["primary"]["id"], "re_export")
        self.assertEqual([a["id"] for a in result["secondary"]], ["unexport"])

    def test_not_exported(self):
        result = plan(export_state="none")
        self.assertEqual(result["primary"]["id"], "export")
        self.assertEqual([a["id"] for a in result["secondary"]], ["start"])

    def test_ready_to_start(self):
        result = plan()
        self.assertEqual(result["primary"]["id"], "start")
        self.assertEqual([a["id"] for a in result["secondary"]], ["gpu_check", "export"])

    def test_running_wins_over_memory_and_export(self):
        result = plan(server_running=True, memory_issue=True, export_state="stale")
        self.assertEqual(result["primary"]["id"], "stop")

    def test_missing_server_wins_over_memory(self):
        result = plan(server_ready=False, memory_issue=True)
        self.assertEqual(result["primary"]["id"], "choose_server")

    def test_never_more_than_two_secondary(self):
        for state in ({}, {"server_running": True}, {"memory_issue": True},
                      {"export_state": "none"}, {"has_model": False}):
            self.assertLessEqual(len(plan(**state)["secondary"]), 2)

    def test_every_action_has_label(self):
        for state in ({}, {"server_running": True}, {"memory_issue": True},
                      {"export_state": "stale"}, {"export_state": "none"},
                      {"has_model": False}, {"is_gguf": False}, {"server_ready": False}):
            result = plan(**state)
            self.assertTrue(result["primary"]["label"])
            self.assertTrue(result["reason"])
            for item in result["secondary"]:
                self.assertTrue(item["label"])


if __name__ == "__main__":
    unittest.main()
