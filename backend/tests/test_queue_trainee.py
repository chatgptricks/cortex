from app.slack_alerts import build_queue_assignment_message, queue_notification_slack_user_id


def test_trainee_notifications_temporarily_route_to_esteban():
    assert queue_notification_slack_user_id("user12@example.com") == "U0000000012"


def test_trainee_assignment_message_uses_sixteen_minute_pp():
    message = build_queue_assignment_message(
        task_id=99,
        assignee_email="user12@example.com",
        assigned_by_email="user05@example.com",
        account="chatgptricks",
        post_id=None,
        production_points=3,
        minutes_per_pp=16,
    )
    fields = [
        field["text"]
        for block in message["blocks"]
        for field in block.get("fields", [])
    ]
    assert "*Production*\n3 PP · 48 min" in fields
