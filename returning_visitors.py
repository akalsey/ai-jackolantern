"""Remembers recent groups, so the pumpkin can tell when one comes back.

The stage camera gives each visitor an appearance signature (the average color
of the top and bottom halves of their body) and a feet-to-head height. A new
group whose people mostly match the people of a recent group, in both, is taken
to be that group again. What the pumpkin said they were wearing is kept with
them, so it can say so.

Kept in memory only, for one evening's recent visitors. Only numbers and the
pumpkin's own costume descriptions, never images.
"""

import threading
import time

NOT_SEEN_BEFORE = "You haven't seen these people before tonight."
REMEMBER_SECONDS = 20 * 60
MAX_GROUPS = 30
# Mean color difference (0..1) between two signatures that still counts as the
# same clothes. Tighter than the stage's frame-to-frame tracking, since a
# return is judged without the person's position to help.
SAME_LOOK = 0.06
# Heights within this fraction of each other count as the same person.
SAME_HEIGHT = 0.2


def look_change(a, b):
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def same_person(a, b):
    change = look_change(a.get("look"), b.get("look"))
    if change is None or change > SAME_LOOK:
        return False
    if a.get("whole") and b.get("whole"):
        ha, hb = a["height"], b["height"]
        return abs(ha - hb) <= SAME_HEIGHT * max(ha, hb)
    return True


def ago(seconds):
    minutes = round(seconds / 60)
    if minutes < 1:
        return "moments ago"
    return "a minute ago" if minutes == 1 else f"{minutes} minutes ago"


class RecentGroups:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.groups = []  # newest last: {"people", "at", "costumes", "gentle"}
        self.lock = threading.Lock()

    def arrived(self, people):
        """Record a group stepping up. Returns the recent group it matches, if any."""
        now = self.clock()
        people = [p for p in people or [] if p.get("look")]
        with self.lock:
            self.groups = [g for g in self.groups if now - g["at"] < REMEMBER_SECONDS][-MAX_GROUPS:]
            match = next((g for g in reversed(self.groups) if self._matches(people, g["people"])), None)
            seen = {"people": people, "at": now, "costumes": match["costumes"] if match else "", "gentle": False}
            self.groups.append(seen)
            if not match:
                return None
            return {"seconds_ago": now - match["at"], "costumes": match["costumes"], "gentle": match["gentle"]}

    def mark_gentle(self):
        """The latest group got gentle mode, so if they come back soon, they stay gentle."""
        with self.lock:
            if self.groups:
                self.groups[-1]["gentle"] = True

    @staticmethod
    def _matches(people, earlier):
        """Most of the new group, and at least one person, were in the earlier one."""
        if not people or not earlier:
            return False
        found = sum(any(same_person(p, e) for e in earlier) for p in people)
        return found >= 1 and found * 2 >= len(people)

    def describe(self, costumes):
        """What the pumpkin saw the latest group wearing."""
        with self.lock:
            if self.groups and costumes:
                self.groups[-1]["costumes"] = costumes

    def hint(self, match):
        """The sentence for global_data.seen_before."""
        if not match:
            return NOT_SEEN_BEFORE
        wearing = f", wearing {match['costumes']}" if match["costumes"] else ""
        return (
            f"You have seen these people before: they were here {ago(match['seconds_ago'])}"
            f"{wearing}, and now they're back."
        )
