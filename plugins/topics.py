import re

LINK_REGEX = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:c/)?([a-zA-Z_0-9]+)/(?:(\d+)/)?(\d+)"
)


def normalize_chat(chat_ref):
    """Convert a numeric chat id string coming from a t.me/c/ link to the
    real (-100 prefixed) chat id used by Pyrogram. Usernames are kept as-is."""
    if isinstance(chat_ref, str) and chat_ref.isnumeric():
        return int("-100" + chat_ref)
    return chat_ref


def parse_link(link: str):
    """Parse a single telegram message link.

    Returns (chat_ref, topic_id_or_None, msg_id) or None if it doesn't match.
    """
    if not link:
        return None
    match = LINK_REGEX.search(link.strip().replace("?single", ""))
    if not match:
        return None
    chat_ref = normalize_chat(match.group(1))
    topic_id = int(match.group(2)) if match.group(2) else None
    msg_id = int(match.group(3))
    return chat_ref, topic_id, msg_id


def parse_range(text: str):
    """Parse a `START_LINK-END_LINK` string used to bound a scan.

    Returns (chat_ref, start_msg_id, end_msg_id) or None.
    """
    if not text:
        return None
    text = text.strip()
    if "-" not in text:
        return None
    idx = text.index("-", text.index("t.me"))
    start_link, end_link = text[:idx], text[idx + 1:]
    start = parse_link(start_link)
    end = parse_link(end_link)
    if not start or not end:
        return None
    chat_ref = start[0]
    return chat_ref, start[2], end[2]


def parse_fbatch_report(text: str):
    """Parse either a full /fbatch generated report, or plain
    `START_LINK-END_LINK` lines (one per topic), into a list of jobs.

    Returns a list of dicts: {chat_ref, topic_id, first, last, name}
    """
    jobs = []
    if not text:
        return jobs

    firsts = re.findall(r"First\s*:\s*(\S+)", text)
    lasts = re.findall(r"Last\s*:\s*(\S+)", text)
    names = re.findall(r"Topic\s*:\s*(.+)", text)

    if firsts and lasts:
        for idx, (f_link, l_link) in enumerate(zip(firsts, lasts)):
            f = parse_link(f_link)
            l = parse_link(l_link)
            if not f or not l:
                continue
            jobs.append({
                "chat_ref": f[0],
                "topic_id": f[1],
                "first": f[2],
                "last": l[2],
                "name": names[idx].strip() if idx < len(names) else None,
            })
        return jobs

    for line in text.splitlines():
        line = line.strip()
        if not line or "t.me" not in line:
            continue
        parsed = parse_range(line)
        if not parsed:
            continue
        chat_ref, start_id, end_id = parsed
        topic_id = parse_link(line.split("-", 1)[0])[1]
        jobs.append({
            "chat_ref": chat_ref,
            "topic_id": topic_id,
            "first": start_id,
            "last": end_id,
            "name": None,
        })
    return jobs


def get_topic_id(msg):
    """Extract the forum-topic id from a Pyrogram Message across builds."""
    if getattr(msg, "forum_topic_created", None) is not None:
        return msg.id

    tid = getattr(msg, "message_thread_id", None)
    if tid:
        return int(tid)

    tid = getattr(msg, "reply_to_top_message_id", None)
    if tid:
        return int(tid)

    reply_to = getattr(msg, "reply_to_message", None)
    if reply_to is not None:
        tid = getattr(reply_to, "message_thread_id", None) or getattr(reply_to, "id", None)
        if tid and getattr(msg, "reply_to_message_id", None) != tid:
            return int(tid)

    return None


def raw_chat(chat_ref):
    if isinstance(chat_ref, int):
        return str(abs(chat_ref))[3:]
    return str(chat_ref)


def msg_url(chat_ref, topic_id, msg_id):
    rc = raw_chat(chat_ref)
    base = f"https://t.me/c/{rc}" if isinstance(chat_ref, int) else f"https://t.me/{rc}"
    if topic_id:
        return f"{base}/{topic_id}/{msg_id}"
    return f"{base}/{msg_id}"


def topic_url(chat_ref, topic_id):
    rc = raw_chat(chat_ref)
    base = f"https://t.me/c/{rc}" if isinstance(chat_ref, int) else f"https://t.me/{rc}"
    return f"{base}/{topic_id}"

