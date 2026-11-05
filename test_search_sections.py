import unittest

import local_ai_manager as lam


class ModelSearchTests(unittest.TestCase):
    def test_empty_query_matches_everything(self):
        self.assertTrue(lam.matches_model_query("Qwen2.5-3B-Q4.gguf", ""))
        self.assertTrue(lam.matches_model_query("Qwen2.5-3B-Q4.gguf", "   "))

    def test_substring_ignores_case(self):
        self.assertTrue(lam.matches_model_query("Qwen2.5-3B-Instruct-Q4_K_M.gguf", "qWeN"))
        self.assertTrue(lam.matches_model_query("Qwen2.5-3B-Instruct-Q4_K_M.gguf", "q4_k_m"))

    def test_no_match(self):
        self.assertFalse(lam.matches_model_query("Llama-3-8B.gguf", "qwen"))

    def test_query_inside_folder_name(self):
        self.assertTrue(lam.matches_model_query("Mistral [папка]", "mistral"))


class HfSearchUrlTests(unittest.TestCase):
    def test_url_contains_gguf_filter_and_sort(self):
        url = lam.hf_search_url("", sort="downloads", limit=30)
        self.assertTrue(url.startswith("https://huggingface.co/api/models?"))
        self.assertIn("filter=gguf", url)
        self.assertIn("sort=downloads", url)
        self.assertIn("direction=-1", url)
        self.assertIn("limit=30", url)
        self.assertNotIn("search=", url)

    def test_query_is_encoded(self):
        url = lam.hf_search_url("qwen 2.5", sort="createdAt")
        self.assertIn("search=qwen+2.5", url)
        self.assertIn("sort=createdAt", url)


class HfRepoListTests(unittest.TestCase):
    def test_sorted_by_downloads(self):
        payload = [
            {"id": "a/small", "downloads": 10},
            {"id": "b/big", "downloads": 5000},
            {"id": "c/mid", "downloads": 300},
        ]
        self.assertEqual([r["id"] for r in lam.hf_repo_list(payload)],
                         ["b/big", "c/mid", "a/small"])

    def test_broken_entries_ignored(self):
        payload = [{"downloads": 5}, "string", {"id": "ok/repo"}]
        repos = lam.hf_repo_list(payload)
        self.assertEqual([r["id"] for r in repos], ["ok/repo"])
        self.assertEqual(repos[0]["downloads"], 0)

    def test_empty_payload(self):
        self.assertEqual(lam.hf_repo_list(None), [])

    def test_sections_defined(self):
        self.assertIn("Популярные GGUF", lam.HF_SECTIONS)
        self.assertTrue(lam.HF_SECTIONS["Поиск по названию"]["query"])


if __name__ == "__main__":
    unittest.main()
