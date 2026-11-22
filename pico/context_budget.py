"""Estimate input before sending and keep provider measurements separate."""

import math


def estimate_input_tokens(prompt):
    ascii_chars = len(prompt) if prompt.isascii() else sum(char.isascii() for char in prompt)
    non_ascii = len(prompt.encode("utf-8", errors="replace")) - ascii_chars
    # A rough estimate for mixed prose, code and logs, including message framing.
    return 16 + math.ceil(ascii_chars / 3 + non_ascii / 2)


def input_budget(window, output_reserve):
    return None if window is None else max(0, window - output_reserve)


def completion_usage(metadata):
    usage = {
        key: metadata[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if type(metadata.get(key)) is int and metadata[key] >= 0
    }
    if "input_tokens" in usage:
        usage["input_tokens_source"] = "provider"
    return usage
