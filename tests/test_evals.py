"""The harness's grading must be able to fail, or a 30/30 means nothing."""

from evals import run
from evals.scenarios import call


def test_pct_nearest_rank():
    assert run.pct([10, 20, 30, 40], 0.5) == 20
    assert run.pct([10, 20, 30, 40], 0.95) == 40


def test_check_reply_passes_a_correct_reply():
    step = call("create_booking", says=["Booked"], when="Friday at 2pm")
    body = {"results": [{"toolCallId": "t1", "result": "Booked Friday."}]}
    assert run.check_reply(step, "t1", 200, body) == []


def test_check_reply_catches_each_kind_of_wrong_reply():
    step = call("create_booking", says=["Booked"], when="Friday at 2pm")
    assert run.check_reply(step, "t1", 401, None) == ["HTTP 401, expected 200"]
    assert run.check_reply(step, "t1", 200, {"results": []}) == [
        "expected exactly one entry in results"]
    wrong_id = {"results": [{"toolCallId": "t2", "result": "Booked."}]}
    assert run.check_reply(step, "t1", 200, wrong_id) == ["toolCallId doesn't match the request"]
    as_error = {"results": [{"toolCallId": "t1", "error": "Booked."}]}
    assert "expected 'result'" in run.check_reply(step, "t1", 200, as_error)[0]
    two_lines = {"results": [{"toolCallId": "t1", "result": "Booked\nFriday"}]}
    assert run.check_reply(step, "t1", 200, two_lines) == ["reply is not a single line"]
    missing = {"results": [{"toolCallId": "t1", "result": "Taken."}]}
    assert run.check_reply(step, "t1", 200, missing) == ["missing 'Booked'"]
