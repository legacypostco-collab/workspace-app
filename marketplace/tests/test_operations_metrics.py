from django.core.cache import cache
from django.test import TestCase, override_settings

from marketplace.services.observability import http_window, record_http_request
from marketplace.tasks import monitoring_heartbeat


class OperationsMetricsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_http_window_uses_aggregated_status_classes(self):
        record_http_request(200, 20)
        record_http_request(404, 20)
        record_http_request(503, 4_000)

        window = http_window(5)

        self.assertEqual(window["total"], 3)
        self.assertEqual(window["status_4xx"], 1)
        self.assertEqual(window["status_5xx"], 1)
        self.assertEqual(window["slow"], 1)

    def test_heartbeat_updates_shared_cache(self):
        timestamp = monitoring_heartbeat()

        self.assertEqual(cache.get("operations:celery_heartbeat"), timestamp)

    @override_settings(HEALTHCHECK_TOKEN="health-secret")
    def test_metrics_endpoint_is_hidden_without_correct_token(self):
        self.assertEqual(self.client.get("/metrics/").status_code, 404)
        self.assertEqual(
            self.client.get(
                "/metrics/", HTTP_X_HEALTHCHECK_TOKEN="wrong-secret"
            ).status_code,
            404,
        )

    @override_settings(HEALTHCHECK_TOKEN="health-secret")
    def test_metrics_endpoint_returns_prometheus_text(self):
        record_http_request(500, 25)

        response = self.client.get(
            "/metrics/", HTTP_X_HEALTHCHECK_TOKEN="health-secret"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("consolidator_up 1", response.content.decode())
        self.assertIn("consolidator_http_responses_total{class=\"5xx\"}", response.content.decode())
        self.assertNotIn("health-secret", response.content.decode())
