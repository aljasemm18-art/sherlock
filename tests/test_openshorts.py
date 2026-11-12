from unittest import mock

import pytest

from sherlock_project import openshorts
from sherlock_project.openshorts import OpenShortsClient, OpenShortsError


def _resp(payload, ok=True, status=200):
    r = mock.Mock()
    r.ok = ok
    r.status_code = status
    r.content = b"x"
    r.text = str(payload)
    r.json.return_value = payload
    return r


def test_process_url_sends_form_and_api_key_header():
    client = OpenShortsClient(base_url="http://x", api_key="osk_test")
    with mock.patch("requests.request", return_value=_resp({"job_id": "j1"})) as req:
        assert client.process_url("https://y/v", target_clips=3) == "j1"
    args, kwargs = req.call_args
    assert args == ("POST", "http://x/api/process")
    assert kwargs["headers"] == {"X-API-Key": "osk_test"}
    assert kwargs["data"] == {"url": "https://y/v", "acknowledged": "true", "target_clips": "3"}


def test_process_file_uploads_then_processes(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"data")
    client = OpenShortsClient(base_url="http://x")
    responses = [_resp({"upload_id": "u1"}), _resp({}), _resp({"job_id": "j2"})]
    with mock.patch("requests.request", side_effect=responses) as req:
        assert client.process_file(str(f)) == "j2"
    assert req.call_args_list[1].args == ("PUT", "http://x/api/uploads/u1")
    assert req.call_args_list[2].kwargs["data"]["upload_id"] == "u1"


def test_wait_returns_on_completion_and_raises_on_failure():
    client = OpenShortsClient(base_url="http://x")
    with mock.patch.object(client, "status", side_effect=[{"status": "processing"}, {"status": "completed", "clips": []}]):
        assert client.wait("j", poll=0)["status"] == "completed"
    with mock.patch.object(client, "status", return_value={"status": "failed"}):
        with pytest.raises(OpenShortsError):
            client.wait("j", poll=0)


def test_http_error_raises():
    client = OpenShortsClient(base_url="http://x")
    with mock.patch("requests.request", return_value=_resp({}, ok=False, status=401)):
        with pytest.raises(OpenShortsError):
            client.status("j")


def test_record_stream_requires_ffmpeg():
    with mock.patch.object(openshorts.shutil, "which", return_value=None):
        with pytest.raises(OpenShortsError):
            openshorts.record_stream("https://y", 10, "/tmp/none.mp4")
