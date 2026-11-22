"""Compare history rendering on deterministic inputs without calling a model."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pico.context_manager import ContextManager  # noqa: E402


BASELINE_REVISION = "ffd3de0ff41e4d9b435e440a940a43abb7c589f3"
HISTORY_BUDGET = 720


def message(index, size):
    return {"role": "user", "content": f"H{index:02} " + "x" * size}


def tool(index, name, args, content):
    return {"role": "tool", "name": name, "args": {**args, "replay_id": f"H{index:02}"}, "content": content}


def fixtures():
    return {
        "dense_messages": [message(index, 75) for index in range(12)],
        "long_tool": [
            *(message(index, 75) for index in range(8)),
            tool(8, "run_shell", {"command": "pytest -q " + "a" * 1000}, "FAIL smoke\n" + "b" * 2400),
            message(9, 20),
        ],
        "repeated_reads": [
            *(
                tool(index, "read_file", {"path": "sample.txt", "start": 1, "end": 3}, "alpha\nbeta\n" + "r" * 240)
                for index in range(3)
            ),
            *(message(index, 35) for index in range(3, 9)),
        ],
    }


def load_baseline():
    path = "pico/context_manager.py"
    source = subprocess.check_output(["git", "show", f"{BASELINE_REVISION}:{path}"], cwd=ROOT)
    module = ModuleType("_history_replay_baseline")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{BASELINE_REVISION}:{path}", "exec"), vars(module))
    return module.ContextManager


def replay(manager_type, history, **config):
    agent = SimpleNamespace(
        session={"history": history},
        memory_text=lambda: "Memory:\n- none",
        memory=SimpleNamespace(to_dict=lambda: {"file_summaries": {"sample.txt": {"summary": "alpha | beta"}}}),
    )
    manager = manager_type(agent, section_budgets={"history": HISTORY_BUDGET}, **config)
    prompt, metadata = manager.build("continue")
    history_meta = metadata["history"]
    transcript = prompt.split("Transcript:", 1)[1].split("\n\nCurrent user request:", 1)[0]
    transcript = "Transcript:" + transcript
    return transcript, history_meta


def legacy_decisions(transcript, history, baseline_type):
    # Synthetic markers survive the legacy clipping. The one exception is its
    # file summary, which replaces the first older read and loses its marker.
    manager = baseline_type(None)
    retained = [
        index for index in range(len(history))
        if f"H{index:02}" in transcript or (index == 0 and "sample.txt -> alpha | beta" in transcript)
    ]
    return {
        "strategy": "legacy_fixed",
        "budget_chars": HISTORY_BUDGET,
        "retained_count": len(retained),
        "recent_count": sum(index >= len(history) - 6 for index in retained),
        "shortened_entries": [
            {"index": index, "reason": "legacy_render"}
            for index in retained
            if manager._raw_history_text([history[index]]).split("\n", 1)[1] not in transcript
        ],
        "omitted_entries": [
            {"index": index, "reason": "legacy_not_rendered"}
            for index in range(len(history)) if index not in retained
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-baseline", action="store_true", help="Also replay the pre-Phase 1 Git revision.")
    args = parser.parse_args()
    baseline = load_baseline() if args.with_baseline else None
    results = {}
    for name, history in fixtures().items():
        results[name] = runs = {}
        if baseline:
            transcript, metadata = replay(baseline, history)
            runs["legacy_fixed"] = {**metadata, **legacy_decisions(transcript, history, baseline), "transcript": transcript}
        for strategy in ("fixed", "dynamic"):
            transcript, metadata = replay(ContextManager, history, history_strategy=strategy)
            runs[strategy] = {**metadata, "transcript": transcript}
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
