from unittest.mock import MagicMock

from app.copy_intel.scheduler import COPY_POLL_REGISTERED_KEY, schedule_copy_intel_jobs
from tests.conftest import make_test_settings


class TestCopyScheduler:
    def test_job_registered_once(self):
        settings = make_test_settings(copy_intel_enabled=True)
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_copy_intel_jobs(app, settings)
        assert app.bot_data[COPY_POLL_REGISTERED_KEY] is True
        app.job_queue.run_repeating.assert_called_once()
        schedule_copy_intel_jobs(app, settings)
        app.job_queue.run_repeating.assert_called_once()

    def test_disabled_skips_registration(self):
        settings = make_test_settings(copy_intel_enabled=False)
        app = MagicMock()
        app.bot_data = {}
        app.job_queue = MagicMock()
        schedule_copy_intel_jobs(app, settings)
        app.job_queue.run_repeating.assert_not_called()
