"""What restarts the agent process: Python code and surroundings.txt, nothing
else. Used by watchfiles in the Dockerfile's CMD."""

from watchfiles import PythonFilter


class CodeAndSurroundings(PythonFilter):
    def __call__(self, change, path):
        return path.endswith("/surroundings.txt") or super().__call__(change, path)
