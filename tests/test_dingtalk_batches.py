import json
import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from trendradar.notification.senders import send_to_dingtalk


class DingTalkBatchTests(unittest.TestCase):
    def test_escaped_json_is_rebatched_using_the_final_request_size(self):
        rows = [f'第{index}条新闻 "' + '"\\新闻' * 18 + "\n" for index in range(16)]

        def split_by_lines(*args, max_bytes, **kwargs):
            batches = [""]
            for row in rows:
                if len((batches[-1] + row).encode("utf-8")) > max_bytes:
                    batches.append("")
                batches[-1] += row
            return batches

        splitter = Mock(side_effect=split_by_lines)
        reply = Mock(status_code=200)
        reply.json.return_value = {"errcode": 0}
        with patch("trendradar.notification.senders.requests.post", return_value=reply) as post:
            self.assertTrue(send_to_dingtalk(
                "https://example.com/dingtalk", {}, "A股收盘简报",
                batch_size=1200, batch_interval=0, split_content_func=splitter,
            ))
        self.assertGreater(splitter.call_count, 1)
        self.assertGreater(post.call_count, 1)
        bodies = [call.kwargs["data"] for call in post.call_args_list]
        self.assertTrue(all(len(body) <= 1200 for body in bodies))
        messages = "\n".join(json.loads(body)["markdown"]["text"] for body in bodies)
        self.assertTrue(all(row.rstrip() in messages for row in rows))

    def test_unsplittable_payload_does_not_send_partial_batches(self):
        splitter = Mock(return_value=["单条内容" * 4000])
        with patch("trendradar.notification.senders.requests.post") as post:
            self.assertFalse(send_to_dingtalk(
                "https://example.com/dingtalk", {}, "A股收盘简报",
                batch_size=1000, split_content_func=splitter,
            ))
        post.assert_not_called()
