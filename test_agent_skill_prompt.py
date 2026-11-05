import unittest
from local_ai_manager import LocalAIManager, hidden_process_kwargs


class AgentSkillPromptTests(unittest.TestCase):
    def setUp(self):
        self.app = LocalAIManager.__new__(LocalAIManager)
        self.app.config_data = {
            "agents": [{
                "id": "agent-1",
                "name": "Coder",
                "system_prompt": "Ты пишешь аккуратный Python-код.",
                "skill_ids": ["skill-on", "skill-off"],
            }],
            "skills": [
                {"id": "skill-on", "name": "Tests", "enabled": True,
                 "content": "Добавляй тесты к исправлениям."},
                {"id": "skill-off", "name": "Disabled", "enabled": False,
                 "content": "Эта инструкция не должна попасть в prompt."},
                {"id": "unused", "name": "Unused", "enabled": True,
                 "content": "Не подключённый навык."},
            ],
        }

    def test_prompt_includes_agent_and_only_selected_enabled_skills(self):
        prompt = self.app.build_agent_system_prompt("agent-1")
        self.assertIn("Ты пишешь аккуратный Python-код.", prompt)
        self.assertIn("Добавляй тесты к исправлениям.", prompt)
        self.assertNotIn("Эта инструкция не должна попасть", prompt)
        self.assertNotIn("Не подключённый навык", prompt)

    def test_unknown_agent_uses_safe_default(self):
        prompt = self.app.build_agent_system_prompt("missing")
        self.assertIn("Ты полезный ассистент.", prompt)
        self.assertIn("Не утверждай, что вызываешь инструменты", prompt)

    def test_hidden_process_kwargs_are_defined(self):
        kwargs = hidden_process_kwargs()
        self.assertIn("creationflags", kwargs)


if __name__ == "__main__":
    unittest.main()
