"""Applies the companion patch to Open-LLM-VTuber (run again after updating Open-LLM-VTuber).

Usage: python -I patch_olv.py C:\\path\\to\\Open-LLM-VTuber\\src\\open_llm_vtuber
- Copies companion_patch.py into agent\\agents\\.
- Adds a few lines to basic_memory_agent.py and tool_executor.py (keeps a .companion_backup first).
- Does nothing if already patched. If any piece is not found, changes NO file.
- Compiles the result; on an error it restores the backups.
Tested with Open-LLM-VTuber main 992309c (v1.2.1+).
"""
import py_compile
import shutil
import sys
from pathlib import Path

MARK = "companion_patch"

AGENT_EDITS = [
    ("import", "from loguru import logger\n",
     "from loguru import logger\nfrom . import companion_patch  # companion patch\n"),
    ("set_system", "        self._system = system\n",
     "        self._companion_base_system = system  # companion patch\n"
     "        self._system = companion_patch.compose_system(system)\n"),
    ("chat refresh", "            self.reset_interrupt()\n            self.prompt_mode_flag = False\n\n            messages = self._to_messages(input_data)\n",
     "            self.reset_interrupt()\n            self.prompt_mode_flag = False\n"
     "            self._system = companion_patch.compose_system(self._companion_base_system)  # companion patch: card on every request\n\n"
     "            messages = self._to_messages(input_data)\n"),
    ("window", "        messages = self._memory.copy()\n",
     "        messages = companion_patch.window(self)  # companion patch: sliding window\n"),
    ("images", "                if tool_results_for_llm:\n                    messages.extend(tool_results_for_llm)\n                continue\n",
     "                if tool_results_for_llm:\n"
     "                    companion_images = companion_patch.pop_images(tool_results_for_llm)  # companion patch\n"
     "                    messages.extend(tool_results_for_llm)\n"
     "                    if companion_images:\n"
     "                        messages.append(companion_patch.image_message(companion_images))\n"
     "                continue\n"),
]

EXECUTOR_EDITS = [
    ("import", "from loguru import logger\n",
     "from loguru import logger\nfrom ..agent.agents import companion_patch  # companion patch\n"),
    ("reset", "            status_content = text_content  # Default to text content\n",
     "            status_content = text_content  # Default to text content\n"
     "            companion_images = []  # companion patch\n"),
    ("collect", "                    elif caller_mode in [\"OpenAI\", \"Prompt\"]:\n                        llm_formatted_content = status_content\n",
     "                    elif caller_mode in [\"OpenAI\", \"Prompt\"]:\n"
     "                        llm_formatted_content = status_content\n"
     "                        if caller_mode == \"OpenAI\":  # companion patch: keep the image\n"
     "                            companion_images = companion_patch.collect_images(content_items)\n"),
    ("attach", "            formatted_result = self.format_tool_result(\n                caller_mode, tool_id, llm_formatted_content, is_error\n            )\n",
     "            formatted_result = self.format_tool_result(\n                caller_mode, tool_id, llm_formatted_content, is_error\n            )\n"
     "            if formatted_result and companion_images:  # companion patch\n"
     "                formatted_result[\"_companion_images\"] = companion_images\n"),
]


def plan(path: Path, edits):
    raw = path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    if MARK in text:
        return None, f"{path.name}: already patched, not touched"
    for name, old, new in edits:
        count = text.count(old)
        if count != 1:
            sys.exit(f"ERROR: {path.name} piece '{name}' found {count} times (expected 1). No file was changed.")
        text = text.replace(old, new)
    return text.replace("\n", nl), f"{path.name}: {len(edits)}/{len(edits)} pieces added"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -I patch_olv.py <...\\src\\open_llm_vtuber>")
    root = Path(sys.argv[1])
    agent = root / "agent" / "agents" / "basic_memory_agent.py"
    executor = root / "mcpp" / "tool_executor.py"
    source = Path(__file__).with_name("companion_patch.py")

    jobs = [(agent, *plan(agent, AGENT_EDITS)), (executor, *plan(executor, EXECUTOR_EDITS))]
    shutil.copyfile(source, agent.with_name("companion_patch.py"))
    print("companion_patch.py copied")
    for path, new_text, msg in jobs:
        if new_text is not None:
            backup = path.with_name(path.name + ".companion_backup")
            if not backup.exists():
                shutil.copyfile(path, backup)
            path.write_bytes(new_text.encode("utf-8"))
        print(msg)
    try:
        for path, _, _ in jobs:
            py_compile.compile(str(path), doraise=True)
        py_compile.compile(str(agent.with_name("companion_patch.py")), doraise=True)
    except py_compile.PyCompileError as e:
        for path, new_text, _ in jobs:
            if new_text is not None:
                shutil.copyfile(path.with_name(path.name + ".companion_backup"), path)
        sys.exit(f"ERROR: compile failed, backups restored:\n{e}")
    print("done: patch applied and compiled")


if __name__ == "__main__":
    main()
