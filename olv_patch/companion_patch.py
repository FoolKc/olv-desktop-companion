"""Companion patch for Open-LLM-VTuber: user card, sliding window, tool images.

This file lives in src/open_llm_vtuber/agent/agents/. basic_memory_agent.py and tool_executor.py
are hooked up to it with a few lines (patch_olv.py adds them).

1) User card: the CARD_PATH file is put at the TOP of the system prompt on every request.
   Edit the file any time; no restart needed. Set CARD_PATH below, or the COMPANION_CARD
   environment variable for the Open-LLM-VTuber process. Empty = no card.
2) Sliding window: when the history passes WINDOW_MAX messages, only the last WINDOW_KEEP are
   sent. It cuts in chunks, so the start of the request stays the same most of the time
   (good for providers with prompt caching). The full history is still saved by
   Open-LLM-VTuber; it is just not sent to the LLM.
3) Tool images: with OpenAI-compatible models, Open-LLM-VTuber drops images returned by MCP
   tools. The image is now sent in a "user" message right after the tool result. That message
   exists only for that turn and never enters memory, so old screenshots are never re-sent.
"""
import os

from loguru import logger

CARD_PATH = os.environ.get("COMPANION_CARD", r"")   # e.g. r"C:\path\to\olv-desktop-companion\prompts\user_card.md"
CARD_MAX_CHARS = 4000
WINDOW_MAX = 30
WINDOW_KEEP = 15


# ---------------- 1) User card ----------------
def read_card() -> str:
    if not CARD_PATH:
        return ""
    try:
        with open(CARD_PATH, encoding="utf-8") as f:
            card = f.read().strip()
        if len(card) > CARD_MAX_CHARS:
            logger.warning(f"[companion] user card is {len(card)} characters; only the first {CARD_MAX_CHARS} are used")
        return card[:CARD_MAX_CHARS]
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.warning(f"[companion] could not read the user card: {e}")
        return ""


def compose_system(base: str) -> str:
    card = read_card()
    return f"{card}\n\n{base}" if card else base


# ---------------- 2) Sliding window ----------------
def on_dropped(agent, dropped: list) -> None:
    """Messages that left the window. A long-term memory could hook in here."""
    logger.info(f"[companion] window: {len(dropped)} old messages are no longer sent to the LLM")


def window(agent) -> list:
    mem = agent._memory
    if getattr(agent, "_companion_mem_id", None) != id(mem):   # new chat / history loaded
        agent._companion_mem_id, agent._companion_start = id(mem), 0
    start = min(agent._companion_start, len(mem))
    if len(mem) - start > WINDOW_MAX:
        old = start
        start = len(mem) - WINDOW_KEEP
        while start < len(mem) and mem[start].get("role") != "user":
            start += 1                                    # start the window with a user message
        if start >= len(mem):
            start = len(mem) - WINDOW_KEEP
        on_dropped(agent, mem[old:start])
    agent._companion_start = start
    return mem[start:]


# ---------------- 3) Tool images ----------------
def collect_images(content_items: list) -> list:
    """Data URLs from an MCP content list (used by tool_executor)."""
    return [f"data:{i['mimeType']};base64,{i['data']}"
            for i in content_items or []
            if i.get("type") == "image" and i.get("data") and i.get("mimeType")]


def pop_images(results: list) -> list:
    images = []
    for r in results:
        if isinstance(r, dict):
            images += r.pop("_companion_images", None) or []
    return images


def image_message(images: list) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": "[Image returned by the tool call above. This message comes from the system, "
                                 "not from the user. In your reply, say in one short sentence what you see.]"},
        *({"type": "image_url", "image_url": {"url": url}} for url in images),
    ]}
