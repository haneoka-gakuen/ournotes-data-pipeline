"""Convert playable story commands into readable speaker-and-dialogue text."""

import html
import re

LANGUAGES = ["ja", "en", "zh-TW", "zh-CN", "ko"]
NARRATOR = ["ナレーション", "Narrator", "旁白", "旁白", "내레이션"]
ANONYMOUS = ["？？？", "???", "？？？", "？？？", "???"]


def localized(value, index):
    if isinstance(value, list):
        return str(value[index] or "") if index < len(value) else ""
    if isinstance(value, dict):
        return str(value.get(LANGUAGES[index]) or "")
    return str(value or "")


def plain(value):
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\[/?(?:b|i|u|s|color|size|font|ruby)(?:=[^\]]*)?\]", "", value, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def dialogue_commands(commands):
    for command in commands:
        if command.get("ignoreData"):
            continue
        group = command.get("commandGroup", {})
        if group.get("actions"):
            actions = group["actions"]
            if isinstance(actions, dict):
                actions = list(actions.values())
            for action in sorted(actions, key=lambda item: float(item.get("atSeconds") or 0)):
                yield from dialogue_commands([action["command"]])
        elif command.get("command") in {2, 28, 37}:
            yield command
        elif command.get("command") == 45:
            signals = command.get("timeline", {}).get("signals", [])
            if isinstance(signals, dict):
                signals = list(signals.values())
            for signal in signals:
                if signal.get("episode"):
                    yield from dialogue_commands([signal["episode"]])


def transcript(story, index):
    lines = []
    for command in dialogue_commands(story.get("commands", [])):
        body = plain(localized(command.get("text"), index))
        if not body:
            continue
        names = [plain(localized(name, index)) for name in command.get("targetTextNames", [])]
        names = [name for name in names if name]
        if not names:
            names = [plain(localized(target.get("name"), index)) for target in command.get("targets", [])]
            names = [name for name in names if name]
        status = command.get("targetStatus", 0)
        speaker = (
            ANONYMOUS[index]
            if status == 1
            else NARRATOR[index]
            if status == 2
            else ("、".join(names) or NARRATOR[index])
        )
        separator = ": " if index in {1, 4} else "："
        lines.append(f"{speaker}{separator}{body}")
    return "\n".join(lines) + "\n" if lines else ""
