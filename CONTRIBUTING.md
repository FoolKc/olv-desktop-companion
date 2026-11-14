# Contributing

Thanks for wanting to help! This is an experimental project, so small fixes, big ideas and
"this broke on my machine" reports are all welcome.

## Before you open a pull request

- **Keep the safety rules in code.** Tools that change anything must stay locked during bridge
  events, deletes and overwrites must keep the confirmation box, and file access must stay inside
  one folder. If your change touches `_system_turn`, `_confirm`, `_resolve` or the taint logic,
  explain why in the pull request.
- **Treat outside text as data.** Anything from a video, a file, a web page or the screen must
  never be able to act as an instruction. Clean it and wrap it (see `for_prompt` / `_safe`).
- **Tool descriptions say what a tool does, not how the character should sound.** Personality
  belongs in the user's prompt; shared behavior rules belong in `prompts/`.
- **No personal data** in code, prompts or examples (names, paths, accounts, keys).
- **Test what you can.** Say what you tried (OS, Open-LLM-VTuber version, model) and what you
  could not test.

## Good first issues

- Add an app to the allow-list in a way that works for other people too (paths, URI schemes).
- Add private-window words for your language in `PRIVATE_WORDS`.
- Improve error messages so the character can explain problems better.

## Reporting a security problem

If you find a way to make the character do something it should not (for example write outside
the folder, skip the confirmation box or act on text from a video), please open an issue with
the steps to reproduce it.
