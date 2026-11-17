#!/usr/local/bin/python
"""Keep explicit config overrides on the resume subcommand in CLI 0.144.1."""

import os
import sys


def normalize(arguments):
    if arguments and arguments[0] == "exec" and "resume" in arguments:
        resume = arguments.index("resume")
        before = arguments[1:resume]
        ordinary, config = [], []
        index = 0
        while index < len(before):
            if before[index] in {"-c", "--config"} and index + 1 < len(before):
                config.extend(before[index:index + 2])
                index += 2
            else:
                ordinary.append(before[index])
                index += 1
        return ["exec", *ordinary, "resume", *config, *arguments[resume + 1:]]
    return arguments


if __name__ == "__main__":
    os.execvp("codex", ["codex", *normalize(sys.argv[1:])])
