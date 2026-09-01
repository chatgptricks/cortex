from app.slack_alerts import build_queue_assignment_message, queue_notification_slack_user_id


def test_trainee_notifications_temporarily_route_to_esteban():
    assert queue_notification_slack_user_id("trainee@sentientagency.io") == "U08UYJMPJ76"


def test_assignment_message_is_compact_and_keeps_the_queue_link():
    message = build_queue_assignment_message(
        task_id=99,
        assignee_email="trainee@sentientagency.io",
        assigned_by_email="ivan@sentientagency.io",
        account="chatgptricks",
        post_id=None,
        production_points=3,
        minutes_per_pp=16,
        note="A long brief that belongs in Queue, not Slack.",
        priority="urgent",
        scheduled_date="2026-09-01",
        scheduled_start_minutes=600,
    )
    assert message["text"] == "<@U0516SU09J9> assigned you this post for @chatgptricks."
    assert all("fields" not in block for block in message["blocks"])
    assert all("Brief" not in str(block) for block in message["blocks"])
    assert message["blocks"][-1]["type"] == "actions"
    assert message["blocks"][-1]["elements"][0]["url"].endswith("queue.html?r=eyJ0YXNrIjoiOTkifQ")


def test_assignment_thumbnail_uses_public_api_url_for_stored_paths():
    message = build_queue_assignment_message(
        task_id=100,
        assignee_email="ivan@sentientagency.io",
        assigned_by_email="esteban@sentientagency.io",
        account="chatgptricks",
        post_id=42,
        recommended_accounts=["chatgptricks"],
        cover_url="/api/dashboard/covers/chatgptricks/42",
    )
    image = next(block for block in message["blocks"] if block["type"] == "image")
    assert image["image_url"] == "https://cortex-api-db2e.onrender.com/api/dashboard/covers/chatgptricks/42"


def test_schedule_update_is_text_only():
    message = build_queue_assignment_message(
        task_id=101,
        assignee_email="ivan@sentientagency.io",
        assigned_by_email="esteban@sentientagency.io",
        account="chatgptricks",
        post_id=42,
        cover_url="/api/dashboard/covers/chatgptricks/42",
        recommended_accounts=["chatgptricks"],
        update=True,
    )
    assert message["text"] == "<@U08UYJMPJ76> updated this post for @chatgptricks."
    assert all(block["type"] != "image" for block in message["blocks"])
