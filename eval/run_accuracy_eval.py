"""Accuracy/quality eval for transcription + summarization.

Not part of the shipped app — a standalone dev tool that runs the real
pipeline (real Groq calls, real audio) against a small hand-written golden
set where the "correct" answer is known in advance:

  1. Synthesize each script to speech (Windows TTS via PowerShell).
  2. Transcribe it with the real ASR service and compute Word Error Rate
     against the original script.
  3. Summarize the transcript with the real summarization service and
     score the result against the known decisions, action items, and
     open questions — including whether it correctly avoided treating
     something merely *discussed* as a *decision*.

Run from the project root:

    python -m eval.run_accuracy_eval

Requires GROQ_API_KEY in .env, same as the app itself, and Windows (the
TTS step uses System.Speech via PowerShell — this project has only ever
targeted Windows dev environments).
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core import config  # noqa: F401  (loads .env into os.environ)
from backend.app.services.summarization_service import summarize
from backend.app.services.transcription_service import transcribe
from eval.cases import CASES
from eval.wer import word_error_rate

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def synthesize_speech(text: str, out_wav: Path) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    script_txt = out_wav.with_suffix(".script.txt")
    script_txt.write_text(text, encoding="utf-8")

    ps_command = (
        "Add-Type -AssemblyName System.Speech; "
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f'$synth.SetOutputToWaveFile("{out_wav}"); '
        f'$text = Get-Content -Raw -Encoding UTF8 "{script_txt}"; '
        "$synth.Speak($text); "
        "$synth.Dispose()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_command],
        check=True,
        capture_output=True,
    )


def _norm(text: str) -> str:
    # Strips all punctuation/whitespace, not just lowercasing — the model
    # is free to write "stand-up", "stand up", or "stand‑up" and all
    # of those are the same word for matching purposes.
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _matches(text: str, keyword_groups: list[list[str]]) -> bool:
    """keyword_groups is an OR of ANDs: [["budget"], ["contractor"]] means
    "matches if the text contains 'budget', OR contains 'contractor'" —
    lets a case accept more than one valid real-world phrasing of the same
    underlying item without weakening what counts as a match."""
    normalized = _norm(text)
    return any(all(_norm(kw) in normalized for kw in group) for group in keyword_groups)


def score_decisions(expected: list[dict], must_not_decide: list[dict], actual: list[str]) -> dict:
    matched = sum(1 for exp in expected if any(_matches(d, exp["keywords"]) for d in actual))
    false_positives = sum(1 for bad in must_not_decide if any(_matches(d, bad["keywords"]) for d in actual))
    total = len(expected)
    return {
        "matched": matched,
        "total_expected": total,
        "recall": matched / total if total else 1.0,
        "false_positives": false_positives,
    }


def score_action_items(expected: list[dict], actual: list[dict]) -> dict:
    matched = 0
    for exp in expected:
        for item in actual:
            task_ok = _matches(item.get("task", ""), [exp["task_keywords"]])
            assignee_ok = _norm(exp["assignee"]) in _norm(item.get("assignee") or "")
            deadline_ok = _matches(item.get("deadline") or "", [[kw] for kw in exp["deadline_keywords"]])
            if task_ok and assignee_ok and deadline_ok:
                matched += 1
                break
    total = len(expected)
    return {"matched": matched, "total_expected": total, "recall": matched / total if total else 1.0}


def score_open_questions(expected: list[dict], actual: list[str]) -> dict:
    matched = sum(1 for exp in expected if any(_matches(q, exp["keywords"]) for q in actual))
    total = len(expected)
    return {"matched": matched, "total_expected": total, "recall": matched / total if total else 1.0}


def build_report(run_id: str, rows: list[dict]) -> str:
    lines = [f"# Accuracy eval — {run_id}", ""]

    avg_wer = sum(r["wer"] for r in rows) / len(rows)
    total_decisions = sum(r["decision_score"]["total_expected"] for r in rows)
    matched_decisions = sum(r["decision_score"]["matched"] for r in rows)
    total_fp = sum(r["decision_score"]["false_positives"] for r in rows)
    total_actions = sum(r["action_score"]["total_expected"] for r in rows)
    matched_actions = sum(r["action_score"]["matched"] for r in rows)
    total_questions = sum(r["question_score"]["total_expected"] for r in rows)
    matched_questions = sum(r["question_score"]["matched"] for r in rows)

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Average WER: **{avg_wer:.1%}**")
    lines.append(f"- Decision recall: **{matched_decisions}/{total_decisions}**"
                 f" (false positives — discussed but not decided, wrongly recorded as a decision: {total_fp})")
    lines.append(f"- Action item recall (task + assignee + deadline all correct): **{matched_actions}/{total_actions}**")
    lines.append(f"- Open question recall: **{matched_questions}/{total_questions}**")
    lines.append("")

    for row in rows:
        lines.append(f"## {row['case']}")
        lines.append("")
        lines.append(f"- WER: {row['wer']:.1%}")
        lines.append(f"- Decisions: {row['decision_score']['matched']}/{row['decision_score']['total_expected']}"
                     f" matched, {row['decision_score']['false_positives']} false positive(s)")
        lines.append(f"- Action items: {row['action_score']['matched']}/{row['action_score']['total_expected']} matched")
        lines.append(f"- Open questions: {row['question_score']['matched']}/{row['question_score']['total_expected']} matched")
        lines.append("")
        lines.append("**Transcript:**")
        lines.append("")
        lines.append(f"> {row['transcript']}")
        lines.append("")
        lines.append("**Summary output:**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(row["summary"], indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / run_id
    audio_dir = run_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for case in CASES:
        print(f"[{case['name']}] synthesizing audio...")
        wav_path = audio_dir / f"{case['name']}.wav"
        synthesize_speech(case["script"], wav_path)

        print(f"[{case['name']}] transcribing...")
        transcript = transcribe(str(wav_path))
        wer = word_error_rate(case["script"], transcript)

        print(f"[{case['name']}] summarizing...")
        summary_result = summarize(transcript)

        rows.append(
            {
                "case": case["name"],
                "wer": wer,
                "transcript": transcript,
                "summary": summary_result,
                "decision_score": score_decisions(
                    case["expected_decisions"], case["must_not_decide"], summary_result["key_decisions"]
                ),
                "action_score": score_action_items(case["expected_action_items"], summary_result["action_items"]),
                "question_score": score_open_questions(
                    case["expected_open_questions"], summary_result["open_questions"]
                ),
            }
        )

    (run_dir / "raw_results.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=True), encoding="utf-8"
    )
    report_path = run_dir / "report.md"
    report_path.write_text(build_report(run_id, rows), encoding="utf-8")

    print()
    print("=== Results ===")
    for row in rows:
        print(
            f"{row['case']}: WER={row['wer']:.1%} "
            f"decisions={row['decision_score']['matched']}/{row['decision_score']['total_expected']}"
            f"(fp={row['decision_score']['false_positives']}) "
            f"actions={row['action_score']['matched']}/{row['action_score']['total_expected']} "
            f"open_q={row['question_score']['matched']}/{row['question_score']['total_expected']}"
        )
    print()
    print("Full report:", report_path)


if __name__ == "__main__":
    main()
