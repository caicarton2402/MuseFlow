import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

TEMP = tempfile.TemporaryDirectory()
os.environ["BEAUTY_FEED_DATA"] = TEMP.name
app = importlib.import_module("app")
discover = importlib.import_module("discover")


class FeatureTests(unittest.TestCase):
    def test_authorized_source_identifiers(self):
        self.assertEqual(
            discover.AUTHORIZED_SOURCES["会画卧蚕吗"],
            "MzYyMTg3MjgwMg==",
        )

    def test_text_features_are_normalized_and_deterministic(self):
        first = app.hashed_text_features("夏日 人像 摄影")
        second = app.hashed_text_features("夏日 人像 摄影")
        self.assertEqual(first, second)
        self.assertEqual(len(first), app.FEATURE_DIM)
        self.assertAlmostEqual(sum(x * x for x in first), 1.0, places=6)

    def test_event_weights(self):
        self.assertGreater(app.event_weight("like", 0), 0)
        self.assertLess(app.event_weight("dislike", 0), 0)
        self.assertGreater(app.event_weight("view", 12), app.event_weight("view", 1))
        with self.assertRaises(ValueError):
            app.event_weight("unknown", 0)

    def test_article_url_policy(self):
        original = app.is_public_host
        app.is_public_host = lambda host: True
        try:
            self.assertEqual(
                app.validate_article_url("https://mp.weixin.qq.com/s/abc"),
                "https://mp.weixin.qq.com/s/abc",
            )
            for bad in [
                "http://mp.weixin.qq.com/s/abc",
                "https://example.com/s/abc",
                "file:///etc/passwd",
            ]:
                with self.assertRaises(ValueError):
                    app.validate_article_url(bad)
        finally:
            app.is_public_host = original

    def test_extract_account_biz(self):
        html = b'<script>var biz = "MzExample123==";</script>'
        self.assertEqual(app.extract_account_biz(html), "MzExample123==")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        with app.db() as conn:
            conn.execute("DELETE FROM interactions")
            conn.execute("DELETE FROM user_profiles")
            conn.execute("DELETE FROM images")

    def insert_image(self, image_id=1):
        vector = [0.0] * app.FEATURE_DIM
        vector[0] = 1.0
        with app.db() as conn:
            conn.execute(
                """INSERT INTO images(id, sha256, local_path, source_url, article_url,
                article_title, account_name, alt_text, width, height, mime_type,
                visual_json, text_json, status, created_at)
                VALUES (?, ?, 'x.webp', 'https://mmbiz.qpic.cn/x',
                'https://mp.weixin.qq.com/s/a', '标题', '来源', '', 720, 1280,
                'image/webp', ?, ?, 'approved', 9999999999)""",
                (image_id, str(image_id) * 64, json.dumps(vector), json.dumps(vector)),
            )

    def test_profile_updates_and_feed_returns_item(self):
        self.insert_image()
        before_visual, _, _ = app.profile_for("tester")
        self.assertEqual(sum(before_visual), 0)
        app.update_profile("tester", [1.0] + [0.0] * (app.FEATURE_DIM - 1), [1.0] + [0.0] * (app.FEATURE_DIM - 1), 1.8)
        after_visual, _, _ = app.profile_for("tester")
        self.assertGreater(after_visual[0], 0)
        items = app.ranked_feed("tester", 5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 1)

    def test_blocked_and_pending_images_are_not_recommended(self):
        self.insert_image()
        with app.db() as conn:
            conn.execute("UPDATE images SET status='blocked' WHERE id=1")
        self.assertEqual(app.ranked_feed("tester", 5), [])
        with app.db() as conn:
            conn.execute("UPDATE images SET status='pending' WHERE id=1")
        self.assertEqual(app.ranked_feed("tester", 5), [])

    def test_pending_endpoint_lists_review_items(self):
        self.insert_image()
        with app.db() as conn:
            conn.execute("UPDATE images SET status='pending' WHERE id=1")
        response = app.pending_images(10)
        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(response["items"][0]["preview_url"], "/api/moderation/media/1")


if __name__ == "__main__":
    unittest.main()
