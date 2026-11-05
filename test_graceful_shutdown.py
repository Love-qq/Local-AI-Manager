import subprocess
import sys
import time
import unittest

import local_ai_manager as lam


class GracefulShutdownTests(unittest.TestCase):
    def test_stop_without_process(self):
        result = lam.stop_process_gracefully(None)
        self.assertEqual(result, {"graceful": False, "forced": False, "exit_code": None})

    def test_already_finished_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        result = lam.stop_process_gracefully(proc, None, timeout=0.2)
        self.assertFalse(result["graceful"])
        self.assertFalse(result["forced"])
        self.assertEqual(result["exit_code"], proc.returncode)

    def test_send_ctrl_break_without_pid(self):
        self.assertFalse(lam.send_ctrl_break(0))

    @unittest.skipUnless(lam.IS_WINDOWS, "Windows only")
    def test_create_and_close_job(self):
        job = lam.create_process_job()
        self.assertIsNotNone(job)
        lam.close_handle(job)

    @unittest.skipUnless(lam.IS_WINDOWS, "Windows only")
    def test_child_is_stopped_and_job_closed(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        job = lam.create_process_job()
        self.assertIsNotNone(job)
        self.assertTrue(lam.assign_process_to_job(job, proc.pid))
        result = lam.stop_process_gracefully(proc, job, timeout=1.5)
        self.assertIsNotNone(proc.poll(), "процесс должен быть завершён")
        self.assertTrue(result["graceful"] or result["forced"])
        lam.close_handle(job)

    @unittest.skipUnless(lam.IS_WINDOWS, "Windows only")
    def test_detach_job_keeps_process_alive(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            job = lam.create_process_job()
            self.assertIsNotNone(job)
            self.assertTrue(lam.assign_process_to_job(job, proc.pid))
            self.assertTrue(lam.detach_job(job))
            time.sleep(0.3)
            self.assertIsNone(proc.poll(), "после detach процесс должен остаться живым")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
