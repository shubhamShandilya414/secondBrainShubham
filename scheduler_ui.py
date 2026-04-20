"""Simple scheduler UI for launching meeting joins in the background.

This app lets you paste a meeting link, choose a join time, and optionally set
your display name. When the scheduled time arrives, it starts the existing
meeting agent as a background process so the UI stays open.
"""

from __future__ import annotations

import datetime as dt
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path


ROOT = Path(__file__).resolve().parent
AGENT_SCRIPT = ROOT / "meeting_agent.py"


def _parse_local_datetime(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Join time is required")

    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(
        "Use a time like 2026-04-20 18:30 or 2026-04-20T18:30"
    )


def _format_now_hint() -> str:
    now = dt.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M")


class MeetingSchedulerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Meeting Scheduler")
        self.root.geometry("760x520")
        self.root.minsize(720, 500)

        self.status_queue: queue.Queue[str] = queue.Queue()
        self.current_job: threading.Thread | None = None
        self.cancel_requested = threading.Event()

        self.meeting_link_var = tk.StringVar()
        self.join_time_var = tk.StringVar(value=_format_now_hint())
        self.display_name_var = tk.StringVar(value="Your Name")
        self.site_var = tk.StringVar(value="teams")
        self.passcode_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._poll_status_queue()

    def _build_ui(self) -> None:
        self.root.configure(bg="#0f172a")

        container = ttk.Frame(self.root, padding=20)
        container.pack(fill="both", expand=True)

        title = ttk.Label(container, text="Meeting Scheduler", font=("Helvetica", 20, "bold"))
        title.pack(anchor="w", pady=(0, 6))

        subtitle = ttk.Label(
            container,
            text="Paste a Teams or Zoom meeting link, choose a time, and the agent will join in the background.",
            wraplength=700,
        )
        subtitle.pack(anchor="w", pady=(0, 16))

        form = ttk.Frame(container)
        form.pack(fill="x", pady=(0, 12))

        self._row(form, 0, "Meeting link", self.meeting_link_var)
        self._row(form, 1, "Join time", self.join_time_var, hint="Local time, e.g. 2026-04-20 18:30")
        self._row(form, 2, "Display name", self.display_name_var)
        self._row(form, 3, "Passcode / token", self.passcode_var)

        site_frame = ttk.Frame(form)
        site_frame.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 8))
        ttk.Label(site_frame, text="Site").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(site_frame, text="Teams", value="teams", variable=self.site_var).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(site_frame, text="Zoom", value="zoom", variable=self.site_var).pack(side="left")

        button_row = ttk.Frame(container)
        button_row.pack(fill="x", pady=(8, 8))

        self.schedule_button = ttk.Button(button_row, text="Schedule join", command=self.schedule_join)
        self.schedule_button.pack(side="left")

        ttk.Button(button_row, text="Join now", command=self.join_now).pack(side="left", padx=(10, 0))
        ttk.Button(button_row, text="Cancel scheduled job", command=self.cancel_job).pack(side="left", padx=(10, 0))

        status_box = ttk.LabelFrame(container, text="Status", padding=12)
        status_box.pack(fill="both", expand=True, pady=(10, 0))

        self.status_label = ttk.Label(status_box, textvariable=self.status_var, wraplength=680)
        self.status_label.pack(anchor="w")

        self.log = tk.Text(status_box, height=12, wrap="word")
        self.log.pack(fill="both", expand=True, pady=(10, 0))
        self.log.configure(state="disabled")

    def _row(self, parent: ttk.Frame, index: int, label: str, var: tk.StringVar, hint: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=index, column=0, sticky="w", pady=6, padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=var, width=72)
        entry.grid(row=index, column=1, sticky="ew", pady=6)
        parent.grid_columnconfigure(1, weight=1)
        if hint:
            ttk.Label(parent, text=hint, foreground="#64748b").grid(row=index, column=2, sticky="w", padx=(10, 0))

    def schedule_join(self) -> None:
        if self.current_job and self.current_job.is_alive():
            messagebox.showinfo("Job already running", "A scheduled job is already waiting.")
            return

        try:
            scheduled_time = _parse_local_datetime(self.join_time_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid time", str(exc))
            return

        meeting_link = self.meeting_link_var.get().strip()
        if not meeting_link:
            messagebox.showerror("Missing link", "Please enter a meeting link.")
            return

        delay = (scheduled_time - dt.datetime.now()).total_seconds()
        if delay < 0:
            messagebox.showerror("Time passed", "The join time must be in the future.")
            return

        self.cancel_requested.clear()
        self.current_job = threading.Thread(
            target=self._run_scheduled_job,
            args=(delay,),
            daemon=True,
        )
        self.current_job.start()

        self.status_var.set(f"Scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(f"Scheduled meeting for {scheduled_time.isoformat(sep=' ', timespec='seconds')}")

    def join_now(self) -> None:
        self.cancel_requested.clear()
        self._launch_agent_in_background()

    def cancel_job(self) -> None:
        self.cancel_requested.set()
        self.status_var.set("Scheduled job canceled.")
        self._append_log("Canceled the scheduled job.")

    def _run_scheduled_job(self, delay_seconds: float) -> None:
        self._append_log(f"Waiting {int(delay_seconds)} seconds until join time.")
        remaining = int(delay_seconds)
        while remaining > 0 and not self.cancel_requested.is_set():
            self.status_queue.put(f"Joining in {remaining} seconds...")
            self.root.after(0, lambda r=remaining: self._append_log(f"Join in {r} seconds"))
            time.sleep(1)
            remaining -= 1

        if self.cancel_requested.is_set():
            return

        self.root.after(0, self._launch_agent_in_background)

    def _launch_agent_in_background(self) -> None:
        meeting_link = self.meeting_link_var.get().strip()
        display_name = self.display_name_var.get().strip() or "Your Name"
        site = self.site_var.get().strip() or "teams"
        passcode = self.passcode_var.get().strip()

        if not meeting_link:
            messagebox.showerror("Missing link", "Please enter a meeting link.")
            return

        if not AGENT_SCRIPT.exists():
            messagebox.showerror("Missing agent", f"Could not find {AGENT_SCRIPT}")
            return

        cmd = [
            sys.executable,
            str(AGENT_SCRIPT),
            "--site",
            site,
            "--meeting-id",
            meeting_link,
            "--display-name",
            display_name,
        ]
        if passcode:
            cmd.extend(["--passcode", passcode])

        try:
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.status_var.set("Agent launched in the background.")
            self._append_log(f"Launched background agent: {meeting_link}")
        except Exception as exc:
            messagebox.showerror("Launch failed", str(exc))
            self._append_log(f"Launch failed: {exc}")

    def _append_log(self, message: str) -> None:
        def write() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", f"{message}\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        self.root.after(0, write)

    def _poll_status_queue(self) -> None:
        try:
            while True:
                message = self.status_queue.get_nowait()
                self.status_var.set(message)
        except queue.Empty:
            pass
        self.root.after(250, self._poll_status_queue)

    def run(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TLabel", font=("Helvetica", 11))
        style.configure("TButton", font=("Helvetica", 11))
        style.configure("TRadiobutton", font=("Helvetica", 11))
        style.configure("TLabelframe.Label", font=("Helvetica", 11, "bold"))

        self.root.mainloop()


def main() -> int:
    app = MeetingSchedulerApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
