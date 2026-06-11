from __future__ import annotations


def test_parse_any_markdown_checkbox_line_as_task():
    from mastisk.tasks.parser import parse_markdown_tasks

    text = (
        "# Host\n"
        "- [ ] outside any section 📅 2026-06-12 ⏳ 2026-06-11 "
        "🔁 every Friday ⏫ #work [[Mastisk]] 🆔 abc123\n"
        "## Notes\n"
        "  - [x] nested done 🔽 #home [[Home|label]] 🆔 z9\n"
    )

    tasks = parse_markdown_tasks(text, host_path="projects/mastisk.md")

    assert [t["line_number"] for t in tasks] == [2, 4]
    assert tasks[0] == {
        "host_path": "projects/mastisk.md",
        "line_number": 2,
        "checked": False,
        "status": "open",
        "text": "outside any section",
        "due": "2026-06-12",
        "scheduled": "2026-06-11",
        "recurrence": "every Friday",
        "priority": "high",
        "tags": ["work"],
        "links": ["Mastisk"],
        "uid": "abc123",
        "needs_triage": False,
    }
    assert tasks[1]["checked"] is True
    assert tasks[1]["status"] == "done"
    assert tasks[1]["text"] == "nested done"
    assert tasks[1]["priority"] == "low"
    assert tasks[1]["links"] == ["Home"]


def test_parse_rewrite_parse_roundtrip_preserves_task_fields():
    from mastisk.tasks.parser import parse_markdown_tasks, rewrite_task_line

    cases = [
        "- [ ] call Sam 📅 2026-06-12 ⏫ #follow-up [[Sam]] 🆔 aa11",
        "  - [x] weird   spacing 🔁 every other Tuesday 🔼 🆔 bb22",
        "- [ ] unicode café task ⏳ 2026-06-11 🔽 #home/maintenance 🆔 cc33",
    ]

    for line in cases:
        before = parse_markdown_tasks(f"{line}\n")[0]
        rewritten = rewrite_task_line(
            line,
            checked=before["checked"],
            due=before["due"],
            scheduled=before["scheduled"],
            recurrence=before["recurrence"],
            priority=before["priority"],
            uid=before["uid"],
        )
        after = parse_markdown_tasks(f"{rewritten}\n")[0]
        assert after == before


def test_due_time_marker_round_trips_as_datetime():
    from mastisk.tasks.parser import parse_markdown_tasks, rewrite_task_line

    line = "- [ ] call Sam 📅 2026-06-10 ⏰ 14:00 🆔 timed1"

    parsed = parse_markdown_tasks(f"{line}\n")[0]
    assert parsed["due"] == "2026-06-10T14:00:00"
    rewritten = rewrite_task_line(line, due=parsed["due"], uid=parsed["uid"])

    assert rewritten == line


def test_due_time_marker_does_not_become_recurrence_text():
    from mastisk.tasks.parser import parse_markdown_tasks

    parsed = parse_markdown_tasks(
        "- [ ] call Sam 🔁 every monday ⏰ 14:00 🆔 timed1\n"
    )[0]

    assert parsed["recurrence"] == "every monday"


def test_toggle_rewrites_only_the_matching_task_line():
    from mastisk.tasks.parser import rewrite_task_by_uid

    markdown = (
        "intro\n"
        "- [ ] first task 🆔 first1\n"
        "middle\n"
        "- [ ] second task 📅 2026-06-12 🆔 second2\n"
        "tail\n"
    )

    rewritten = rewrite_task_by_uid(markdown, "second2", checked=True)

    before_lines = markdown.splitlines()
    after_lines = rewritten.splitlines()
    assert after_lines[0] == before_lines[0]
    assert after_lines[1] == before_lines[1]
    assert after_lines[2] == before_lines[2]
    assert after_lines[4] == before_lines[4]
    assert after_lines[3] == "- [x] second task 📅 2026-06-12 🆔 second2"


def test_assign_missing_uids_rewrites_only_missing_task_lines():
    from mastisk.tasks.parser import ensure_task_uids

    markdown = (
        "- [ ] needs id\n"
        "not a task\n"
        "- [x] already has one 🆔 old123\n"
    )

    rewritten, assigned = ensure_task_uids(markdown, uid_factory=lambda: "abc789")

    assert assigned == ["abc789"]
    assert rewritten.splitlines() == [
        "- [ ] needs id 🆔 abc789",
        "not a task",
        "- [x] already has one 🆔 old123",
    ]


def test_duplicate_uids_are_reassigned_after_the_first_occurrence():
    from mastisk.tasks.parser import ensure_task_uids

    markdown = (
        "- [ ] original 🆔 dup1\n"
        "- [ ] copied line 🆔 dup1\n"
    )

    rewritten, assigned = ensure_task_uids(markdown, uid_factory=lambda: "new222")

    assert assigned == ["new222"]
    assert rewritten.splitlines() == [
        "- [ ] original 🆔 dup1",
        "- [ ] copied line 🆔 new222",
    ]
