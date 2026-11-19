"""What a user does, in a real browser, against the real server (fake AI only)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import as_client
from tests.e2e.fake_groq import ANSWER, SUMMARY

pytestmark = pytest.mark.e2e

RESULT_TIMEOUT = 30_000  # ms; the UI polls every 2 s


def upload(page: Page, path) -> None:
    page.goto("/")
    page.set_input_files("#audio-input", str(path))
    expect(page.locator("#dropzone-text")).to_contain_text(path.name)
    page.click("#upload-btn")
    expect(page.locator("#result-section")).to_be_visible(timeout=RESULT_TIMEOUT)


def test_upload_shows_the_full_notes(page: Page, make_audio):
    upload(page, make_audio())

    expect(page.locator("#result-title")).to_have_text(SUMMARY["title"])
    expect(page.locator("#result-summary")).to_have_text(SUMMARY["summary"])
    expect(page.locator("#result-decisions li")).to_have_text(SUMMARY["key_decisions"])
    action = page.locator("#result-actions li").first
    expect(action).to_contain_text("Send the budget")
    expect(action).to_contain_text("Priya")
    expect(action).to_contain_text("Friday")
    expect(page.locator("#result-open-questions li")).to_have_text(SUMMARY["open_questions"])
    expect(page.locator("#result-transcript")).to_contain_text("Priya will send the budget by Friday.")
    expect(page.locator("#upload-section")).to_be_hidden()


def test_progress_is_shown_while_processing(page: Page, make_audio):
    page.goto("/")
    page.set_input_files("#audio-input", str(make_audio()))
    page.click("#upload-btn")
    # The status card appears straight away, before the result.
    expect(page.locator("#status-section")).to_be_visible()
    expect(page.locator("#result-section")).to_be_visible(timeout=RESULT_TIMEOUT)


def test_ask_a_question_about_the_meeting(page: Page, make_audio):
    upload(page, make_audio())

    page.fill("#ask-input", "Who sends the budget?")
    page.click("#ask-btn")
    answer = page.locator("#ask-answer")
    expect(answer).to_contain_text(ANSWER)
    expect(answer.locator(".source-chip").first).to_be_visible()
    expect(answer).not_to_contain_text("from cache")

    # The same question again is answered from the cache.
    page.click("#ask-btn")
    expect(answer).to_contain_text("from cache")


def test_ask_across_all_meetings_and_open_a_source(page: Page, make_audio):
    upload(page, make_audio())
    page.click("#new-upload-btn")

    page.fill("#ask-all-input", "Who sends the budget?")
    page.click("#ask-all-btn")
    box = page.locator("#ask-all-answer")
    expect(box).to_contain_text(ANSWER)
    chip = box.locator(".source-chip").first
    expect(chip).to_contain_text(SUMMARY["title"])
    chip.click()
    expect(page.locator("#result-section")).to_be_visible()
    expect(page.locator("#result-title")).to_have_text(SUMMARY["title"])


def test_search_history_highlights_matches(page: Page, make_audio):
    upload(page, make_audio())

    page.click("#history-tab")
    page.fill("#history-search", "budget")
    item = page.locator("#history-list .history-item").first
    expect(item.locator("mark")).to_have_text("budget")

    page.fill("#history-search", "zebra")
    expect(page.locator("#history-list")).to_contain_text("No matches.")


def test_export_downloads_markdown(page: Page, make_audio):
    upload(page, make_audio())

    with page.expect_download() as download:
        page.click("#export-btn")
    assert download.value.suggested_filename == "Launch-planning-sync.md"
    text = download.value.path().read_text(encoding="utf-8")
    assert SUMMARY["title"] in text
    assert "Send the budget" in text


def test_copy_summary_puts_the_notes_on_the_clipboard(page: Page, make_audio):
    upload(page, make_audio())

    page.click("#copy-btn")
    expect(page.locator("#copy-btn")).to_have_text("Copied ✓")
    clip = page.evaluate("navigator.clipboard.readText()")
    assert SUMMARY["summary"] in clip
    assert "Send the budget (Priya, Friday)" in clip


def test_delete_removes_the_meeting(page: Page, make_audio):
    upload(page, make_audio())

    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#delete-btn")
    expect(page.locator("#upload-section")).to_be_visible()

    page.click("#history-tab")
    expect(page.locator("#history-list")).to_contain_text("No meetings yet.")


def test_cancelling_delete_keeps_the_meeting(page: Page, make_audio):
    upload(page, make_audio())

    page.once("dialog", lambda dialog: dialog.dismiss())
    page.click("#delete-btn")
    expect(page.locator("#result-section")).to_be_visible()


def test_meetings_are_private_to_each_browser(page: Page, browser, base_url, make_audio):
    upload(page, make_audio())

    stranger = browser.new_context(base_url=base_url)
    as_client(stranger, base_url, "10.9.9.9")
    other = stranger.new_page()
    other.goto("/")
    other.click("#history-tab")
    expect(other.locator("#history-list")).to_contain_text("No meetings yet.")
    stranger.close()

    # Reloading in the same browser keeps access (the token is in localStorage).
    page.reload()
    page.click("#history-tab")
    expect(page.locator("#history-list .history-item")).to_have_count(1)


def test_rejects_a_file_that_is_not_audio(page: Page, tmp_path):
    fake = tmp_path / "notes.mp3"
    fake.write_text("this is not audio")
    page.goto("/")
    page.set_input_files("#audio-input", str(fake))
    page.click("#upload-btn")
    expect(page.locator("#upload-error")).to_be_visible()
    expect(page.locator("#upload-section")).to_be_visible()


def test_live_recording_with_a_microphone(page: Page):
    page.goto("/")
    page.click("#record-btn")
    expect(page.locator("#record-btn")).to_have_text("■ Stop recording")
    expect(page.locator("#record-timer")).to_be_visible()

    page.wait_for_timeout(3000)  # record a few seconds of the fake microphone
    page.click("#record-btn")
    expect(page.locator("#result-section")).to_be_visible(timeout=RESULT_TIMEOUT)
    expect(page.locator("#result-title")).to_have_text(SUMMARY["title"])
    expect(page.locator("#record-btn")).to_have_text("● Record a meeting")


def test_reopen_a_past_meeting_from_history(page: Page, make_audio):
    upload(page, make_audio())
    page.goto("/")

    page.click("#history-tab")
    page.locator("#history-list .history-item").first.click()
    expect(page.locator("#result-section")).to_be_visible()
    expect(page.locator("#result-title")).to_have_text(SUMMARY["title"])


def test_theme_toggle_remembers_the_choice(page: Page):
    page.goto("/")
    before = page.evaluate("document.documentElement.dataset.theme || ''")
    page.click("#theme-toggle")
    after = page.evaluate("document.documentElement.dataset.theme")
    assert after and after != before
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == after


@pytest.mark.parametrize("width", [375, 768, 1280])
def test_layout_fits_the_screen_without_sideways_scrolling(page: Page, make_audio, width):
    page.set_viewport_size({"width": width, "height": 900})
    upload(page, make_audio())
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"page is {overflow}px wider than a {width}px screen"


def test_no_javascript_errors_during_a_full_session(page: Page, make_audio):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text))

    upload(page, make_audio())
    page.fill("#ask-input", "Who sends the budget?")
    page.click("#ask-btn")
    expect(page.locator("#ask-answer")).to_contain_text(ANSWER)
    page.click("#history-tab")
    page.fill("#history-search", "launch")
    expect(page.locator("#history-list .history-item").first).to_be_visible()

    assert errors == []
