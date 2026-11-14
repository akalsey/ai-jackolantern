"""Relay between the phone remote and the pumpkin page.

The pumpkin page holds the call and the settings, so the server only passes
messages: the remote adds numbered commands (start, stop, set a value), the
pumpkin page fetches the ones it hasn't applied yet, and posts its status
back for the remote to show. Numbering means a remote change is applied once,
and never overwrites a slider someone moved on the pumpkin's computer afterwards.

In memory only: a restart forgets pending commands, and the pumpkin page
posts a fresh status within a second.
"""

import threading
import time

KEEP_COMMANDS = 200
KEEP_ALARMS = 20


class Remote:
    def __init__(self):
        self._lock = threading.Lock()
        self._commands = []  # [{"seq", "kind", "name", "value"}]
        self._seq = 0
        self._status = None
        self._status_at = 0.0
        self._alarms = []  # [{"t", "text"}], newest last

    def command(self, kind, name=None, value=None):
        """Queue a command: "run" (value True/False), "set" (name, value),
        "reset" (name is a slider group: effects, gate, stage), or "restart"
        (from the server, value is why: hang up and redial between shows)."""
        with self._lock:
            self._seq += 1
            self._commands.append({"seq": self._seq, "kind": kind, "name": name, "value": value})
            del self._commands[:-KEEP_COMMANDS]
            return self._seq

    def commands_after(self, seq):
        """Commands newer than seq, plus the latest seq. A page that has never
        fetched (seq -1) gets none of the backlog, only the current position, so
        a reload doesn't replay old changes over its saved settings."""
        with self._lock:
            if seq < 0:
                return {"seq": self._seq, "commands": []}
            return {"seq": self._seq, "commands": [c for c in self._commands if c["seq"] > seq]}

    def report(self, status):
        with self._lock:
            self._status = status
            self._status_at = time.time()

    def status(self):
        """The pumpkin page's last status and how old it is, or None if it never reported."""
        with self._lock:
            if self._status is None:
                return {"status": None, "age": None}
            return {"status": self._status, "age": round(time.time() - self._status_at, 1)}

    def alarm(self, text):
        """Something the operator should know (a restart, a pumpkin that can't
        see); the remote shows the recent ones."""
        with self._lock:
            self._alarms.append({"t": round(time.time(), 1), "text": text})
            del self._alarms[:-KEEP_ALARMS]

    def alarms(self):
        with self._lock:
            return list(self._alarms)
