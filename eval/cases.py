"""Golden test cases for the accuracy/quality eval.

Each case is a short, plainly-worded meeting script — narrated as a single
voice (TTS here doesn't diarize, and neither does the app itself, so this
matches what the pipeline actually has to work with in production). Every
case is hand-written specifically so the "correct" decisions, action items,
and open questions are known in advance — that known answer is what
transcription WER and summary precision/recall get measured against.

Each script also deliberately includes something that is *discussed but
not decided* — the failure mode the summarization prompt explicitly
guards against (SUMMARY_SCHEMA rule 1) is inventing a decision out of
something that was only floated. `must_not_decide` checks that directly.
"""

CASES = [
    {
        "name": "sprint_planning",
        "script": (
            "All right, let's start sprint planning for next week. First, the "
            "login bug. Priya said she'll pick that up and have a fix ready "
            "by Wednesday. Good. Next, we discussed whether to migrate the "
            "database to Postgres this sprint, but that's a bigger "
            "conversation for another day, so we're not deciding on it now. "
            "We did decide to upgrade the CI pipeline to the new runner, "
            "since it cuts build time significantly. Marcus, can you own "
            "that? Marcus agreed to handle the CI upgrade and said he'd "
            "have it done by Friday. There was also a discussion about "
            "whether to hire a contractor for the mobile app, but no "
            "decision was made. We're still waiting on budget approval "
            "from finance before that can move forward. One more thing, "
            "everyone agreed the daily standup time should move to 9:30 AM "
            "starting Monday. That's it for today, thanks everyone."
        ),
        "expected_decisions": [
            {"keywords": [["ci"]]},
            {"keywords": [["standup"]]},
        ],
        "must_not_decide": [
            {"keywords": [["contractor"]]},
        ],
        "expected_action_items": [
            {"task_keywords": ["login"], "assignee": "priya", "deadline_keywords": ["wednesday"]},
            {"task_keywords": ["ci"], "assignee": "marcus", "deadline_keywords": ["friday"]},
        ],
        "expected_open_questions": [
            # The model may frame this as the blocker ("budget approval")
            # or the decision it's blocking ("hire a contractor") — both
            # are a correct capture of the same unresolved item.
            {"keywords": [["budget"], ["contractor"]]},
        ],
    },
    {
        "name": "marketing_budget",
        "script": (
            "Thanks for joining. Let's go over the Q4 budget. First up, the "
            "trade show booth. We agreed to cut that from the budget this "
            "quarter since attendance was low last year. Instead, the team "
            "decided to put that money into paid social ads. Jamie, you'll "
            "own the social ads campaign. Can you have a plan ready by the "
            "15th? Jamie said yes, she'll have the campaign plan by "
            "the 15th. We also talked about redesigning the website, "
            "but we couldn't agree on a budget for it, so that's still "
            "open. Someone needs to get a quote from the design agency "
            "before we can decide. Also, congratulations to the team, the "
            "holiday campaign numbers came in above target. Before we wrap "
            "up, does anyone know if the printed catalog budget was "
            "approved by finance yet? Nobody was sure, so we'll have to "
            "follow up on that separately. Okay, that's everything for "
            "today."
        ),
        "expected_decisions": [
            {"keywords": [["trade show"]]},
            {"keywords": [["social"]]},
        ],
        "must_not_decide": [
            {"keywords": [["website"]]},
        ],
        "expected_action_items": [
            {"task_keywords": ["social"], "assignee": "jamie", "deadline_keywords": ["fifteenth", "15th"]},
        ],
        "expected_open_questions": [
            {"keywords": [["website"]]},
            {"keywords": [["catalog"]]},
        ],
    },
    {
        "name": "support_standup",
        "script": (
            "Morning everyone, quick standup. Ticket volume was high "
            "yesterday because of the outage, so we're still catching up. "
            "Alex, can you take the backlog of billing tickets? Alex "
            "agreed to clear the billing ticket backlog by end of day "
            "today. We also decided to send a status page update to "
            "customers about yesterday's outage. Priya will draft that "
            "update and get it out this morning. Someone raised whether we "
            "should add a live chat option, but we didn't get into it "
            "today, we'll pick that up next week. Anything else? No? "
            "Okay, let's get to it."
        ),
        "expected_decisions": [
            {"keywords": [["status"]]},
        ],
        "must_not_decide": [
            {"keywords": [["live chat"]]},
        ],
        "expected_action_items": [
            {"task_keywords": ["billing"], "assignee": "alex", "deadline_keywords": ["today", "end of day"]},
            {"task_keywords": ["status", "update"], "assignee": "priya", "deadline_keywords": ["morning", "today"]},
        ],
        "expected_open_questions": [
            {"keywords": [["live chat"]]},
        ],
    },
    {
        # None of the first three cases ever left an owner or deadline
        # unstated — every action item had both. That means they never
        # actually tested SUMMARY_SCHEMA rule 3 (null, never guessed) in
        # the one situation where a wrong answer is possible: forcing a
        # guess. This case exists specifically to check that.
        "name": "incident_review",
        "script": (
            "Quick incident review for yesterday's outage. Root cause was "
            "the database connection pool running out during the traffic "
            "spike. We decided to increase the connection pool size in "
            "production. Also, we're going to add an alert for when the "
            "pool goes above 80 percent, but nobody signed up for it yet, "
            "so someone still needs to grab that. The runbook needs to be "
            "updated with these steps too, but we didn't figure out who's "
            "doing that today, we'll assign it at next week's sync. We "
            "also talked about moving to a managed database service "
            "instead, but that's a much bigger discussion for later, "
            "we're not deciding on it now. Good work everyone on the fast "
            "response yesterday."
        ),
        "expected_decisions": [
            {"keywords": [["connection pool"], ["pool size"]]},
        ],
        "must_not_decide": [
            {"keywords": [["managed database"]]},
        ],
        "expected_action_items": [
            {"task_keywords": ["alert"], "assignee": None, "deadline_keywords": None},
            {"task_keywords": ["runbook"], "assignee": None, "deadline_keywords": None},
        ],
        "expected_open_questions": [
            {"keywords": [["managed database"]]},
        ],
    },
]
