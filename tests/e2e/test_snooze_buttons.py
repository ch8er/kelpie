"""Snoozing from the alert itself (#176): a person presses "Snooze" under an item of rule 13's
digest in Telegram, the real ``kelpie serve`` takes the press through its button poller,
acknowledges it in the chat, and the next digest lists the item in its footer."""

from __future__ import annotations

import re

from tests.e2e.test_incidents import SUGGEST, blocked_by_agent
from tests.sim.channels import CHAT_ID
from tests.sim.farm import Farm
from tests.sim.kelpie import wait_for

ALICE = 4242
_ITEM = re.compile(r"^(\d+)\. .* · (t_\w+)$", re.MULTILINE)


def test_a_press_snoozes_a_digest_item_and_the_next_digest_lists_it_in_the_footer(
    farm: Farm,
) -> None:
    blocked_by_agent(
        farm, "t_r2", "CEO: enable R2 for the backup bucket", kind="needs_input", hours=6
    )
    blocked_by_agent(farm, "t_qa", "CEO: run the manual QA pass", kind="needs_input", hours=5)
    rules = {"needs_you": {**SUGGEST, "default_approver": "alice"}}
    telegram = farm.channels.telegram
    farm.serve(farm.config(rules, extra={"alerts": farm.channels.config(buttons=[ALICE])}))

    farm.wait_message("Rule: needs_you", "t_r2", "t_qa")
    message_id, body = next((m, b) for m, b in telegram.sent() if "t_r2" in b["text"])
    numbers = {task: int(n) for n, task in _ITEM.findall(re.sub(r"<[^>]+>", "", body["text"]))}
    rows = body["reply_markup"]["inline_keyboard"]
    row = next(r for r in rows if r[0]["text"].startswith(f"{numbers['t_r2']}. Snooze"))
    assert [b["text"] for b in row][1:] == ["3d", "7d"]
    assert all("t_r2" not in b["callback_data"] for b in row)

    telegram.press(message_id, body, row[0]["callback_data"], ALICE, CHAT_ID)

    (edit,) = wait_for("the alert's acknowledgement", lambda: telegram.calls("editMessageText"))
    assert f"✅ {numbers['t_r2']}. Snoozed t_r2 until " in edit["text"]
    assert edit["text"].endswith(" by Alice")
    left = [b["text"] for r in edit["reply_markup"]["inline_keyboard"] for b in r]
    assert left == [f"{numbers['t_qa']}. Snooze 1d", "3d", "7d"]
    (toast,) = telegram.calls("answerCallbackQuery")
    assert toast["text"].startswith("Snoozed t_r2 until ")

    footer = farm.wait_message("Rule: needs_you", "Snoozed: Work on t_r2 until ", "by Alice")
    assert "t_qa" in footer and "Waiting for you: 1 task" in footer
    assert farm.kelpie is not None
    status = farm.kelpie.cli("snoozes")
    assert status.code == 0 and "t_r2 until " in status.stdout and "by Alice" in status.stdout
    assert "from chat" in status.stdout
    metrics = farm.kelpie.metrics()
    assert metrics['kelpie_chat_actions_total{channel="telegram",outcome="snoozed"}'] == 1
    assert metrics['kelpie_chat_actions_total{channel="telegram",outcome="rejected"}'] == 0
