# Meeting Join Agent

A small Python agent that uses browser automation to join Zoom or Microsoft
Teams meetings, plus a lightweight scheduler UI and a Streamlit front end for
planning jobs.

## What it does

- Opens the meeting join flow in a browser
- Fills the display name when a name field is present
- Tries to click common "join in browser" and "join now" buttons
- Leaves the browser open so you can finish the meeting entry flow manually if
  the site asks for extra confirmation

## Install

Use the same Python interpreter that you will run the app with:

```bash
python -m pip install -r requirements.txt
python -m playwright install
```

If you are inside a virtual environment, make sure it is activated before
running those commands.

## Configure `.env`

This project ships with two local environment files:

- [`.env.example`](/Users/shubhamshandilya/Desktop/secondBrain/.env.example)
- [`.env`](/Users/shubhamshandilya/Desktop/secondBrain/.env)

`.env.example` shows the keys you can set. `.env` is the local file the app
loads automatically at startup.

Edit `.env` for your machine and add:

```bash
GITHUB_TOKEN=your_github_token
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your_jira_api_token
JIRA_PROJECT_KEY=ABC
SARVAM_API_KEY=your_sarvam_api_key
```

To enable Meera's voice with Sarvam AI, `SARVAM_API_KEY` must be set in
`.env` or exported in your shell.

## Run

Teams example:

```bash
python meeting_agent.py \
  --site teams \
  --meeting-id "https://teams.live.com/meet/937131588618?p=vf6q274cE0FhjdS1E0" \
  --display-name "Your Name"
```

If you only have a Teams meeting ID and token:

```bash
python meeting_agent.py \
  --site teams \
  --meeting-id 937131588618 \
  --passcode vf6q274cE0FhjdS1E0 \
  --display-name "Your Name"
```

Zoom example:

```bash
python meeting_agent.py \
  --site zoom \
  --meeting-id 123456789 \
  --passcode 123456 \
  --display-name "Your Name"
```

## Streamlit Front End

Start the Streamlit front end with:

```bash
streamlit run streamlit_app.py
```

In the Streamlit UI:

- Paste a meeting link
- Choose a join time in your local time zone
- Enter a display name
- Optionally enable `Meera conversation mode`
- Download the job JSON or queue it locally

The Streamlit app is the front end only. It prepares the job spec, but a local
worker must actually launch the browser and microphone session.

If you want Streamlit Cloud to talk to your local worker, expose the worker
with ngrok and set `MEERA_WORKER_URL` in `.env` or in Streamlit secrets.

## Local Worker

Run the worker on the machine that has browser and microphone access:

```bash
python worker.py --serve --port 8765
```

The worker watches `streamlit_jobs/inbox/` for JSON job files, waits until the
scheduled time, and launches `meeting_agent.py` locally.

To expose that worker over ngrok:

```bash
ngrok http 8765
```

Then copy the public ngrok URL into `MEERA_WORKER_URL`:

```bash
MEERA_WORKER_URL=https://your-ngrok-url.ngrok-free.app
```

When you submit a job from Streamlit, the app will POST it to
`$MEERA_WORKER_URL/jobs`. The worker writes it into its local queue and then
processes it on the machine that can actually join the call.

## Local Conversation

If you want to talk to Meera without joining a meeting, use the local
conversation panel in the Streamlit front end.

What it does:

- starts Meera immediately in the background
- uses the microphone device you select
- can use a local context brief file if you provide one
- still understands Jira tickets and can reason with Sarvam AI

This is the fastest way to test Meera before a meeting.

You can also run it from the CLI:

```bash
python meeting_agent.py \
  --local-conversation \
  --display-name "Meera" \
  --voice-device-index 0
```

## Work Context Briefing

If you want Meera to prepare before the call, enable the context option in the
UI. GitHub and Jira configuration live in `.env`, not in the form.

Set these values in `.env`:

- `GITHUB_REPOS`, like `owner/repo,owner2/repo2`
- `GITHUB_TOKEN`, if the repos need auth
- `JIRA_BASE_URL`, like `https://your-domain.atlassian.net`
- `JIRA_PROJECT_KEY`, like `ABC`
- `JIRA_JQL`, like `project = ABC ORDER BY updated DESC`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`

Meera will then:

- fetch open GitHub PRs, comments, and changed files
- fetch Jira issues and comments from your JQL query
- filter Jira issues by the project prefix when provided
- skip GitHub entirely when no repos are provided
- write a Markdown prep brief into `context_briefs/`
- launch the meeting agent after the prep window

Set these environment variables before running the worker:

```bash
GITHUB_TOKEN="your_github_token"
GITHUB_REPOS="owner/repo,owner2/repo2"
JIRA_BASE_URL="https://your-domain.atlassian.net"
JIRA_EMAIL="you@example.com"
JIRA_API_TOKEN="your_jira_api_token"
JIRA_PROJECT_KEY="ABC"
JIRA_JQL="project = ABC ORDER BY updated DESC"
```

If you use GitHub or Jira through a single account, these values can stay in
your shell environment or `.env`, and Meera will reuse them each time.

### Deployment note

Streamlit is a good front end for this project, but it cannot directly run the
browser automation or microphone loop on Streamlit Cloud. Keep the worker on a
local machine, VM, or server with GUI/audio access. The Streamlit app can
export the job JSON so the worker can pick it up.

## Voice Interaction

Use the same meeting agent with the `--voice-mode` flag:

```bash
python meeting_agent.py \
  --site teams \
  --meeting-id "https://teams.live.com/meet/937131588618?p=vf6q274cE0FhjdS1E0" \
  --display-name "Meera" \
  --voice-mode
```

After Meera joins, she will keep listening continuously. If you say:

- "Meera, what is your update?"
- "Meera, what's your update?"
- "What is your update?"

she will answer using the latest context brief when available, or with a
short acknowledgment if the question is general.

This uses Sarvam AI speech-to-text and text-to-speech, so `SARVAM_API_KEY`
must be set. If microphone access fails, install `PyAudio` in the same
environment.

## Notes

- Teams often works better with a full invite URL than with a plain numeric ID.
- Meeting pages change often, so selectors may need small tweaks over time.
- Meera's spoken announcement uses Sarvam AI TTS when `SARVAM_API_KEY` is set.
- If you want, I can make this into a more reliable bot that supports Teams
  meeting links directly from an `.ics` invite or calendar event.
