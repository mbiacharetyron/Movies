#!/usr/bin/env python3
"""
tg_resend.py - Resend Telegram media without the bot's inline buttons.

The media is reused by reference (Telegram's stored file id), so nothing is
downloaded or re-uploaded; a 1.5 GB file goes out instantly. The new message
is posted by YOUR account, with no "Forwarded from" header and no buttons.

Setup:
  pip install telethon
  Get API_ID / API_HASH at https://my.telegram.org -> "API development tools"
  export TG_API_ID=1234567
  export TG_API_HASH=abcdef0123456789...

Examples:
  # last 3 media messages from the bot, to Saved Messages
  python tg_resend.py --source @SomeMovieBot --target me --last 3

  # find by filename/caption text
  python tg_resend.py --source @SomeMovieBot --target @friend --search "Unexpected Christmas"

  # specific message ids, keep the original caption
  python tg_resend.py --source @SomeMovieBot --target -1001234567890 --ids 812 815 --keep-caption

  # list recent messages with their ids first
  python tg_resend.py --source @SomeMovieBot --list 15

  # interactive: search all USERNAME_BOT_* in .env, pick a hit, choose destination/topic
  python tg_resend.py --fetch "Money Heist"
  python tg_resend.py --fetch
"""
import argparse
import asyncio
import os
import re
import sys
from types import SimpleNamespace

from telethon import TelegramClient
from telethon.errors import FloodWaitError

CANCEL_WORDS = frozenset({"c", "cancel", "q", "quit"})
CANCEL_HINT = "c to cancel"


def check_cancel(raw):
    if raw.strip().lower() in CANCEL_WORDS:
        print("Cancelled.")
        sys.exit(0)


def load_dotenv(path=None):
    """Load KEY=VALUE lines from a .env file into os.environ (only if unset)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


def parse_args():
    p = argparse.ArgumentParser(description="Resend Telegram media without inline buttons.")
    p.add_argument("--source", help="@username, 'me', or numeric chat id")
    p.add_argument("--target", help="@username, 'me' (Saved Messages), or numeric chat id")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--ids", type=int, nargs="+", help="Message ids to resend")
    sel.add_argument("--last", type=int, help="Resend the last N media messages")
    sel.add_argument("--search", help="Resend media messages whose text matches this")
    sel.add_argument("--list", type=int, metavar="N", help="Just list the last N messages with ids")
    sel.add_argument("--dialogs", nargs="?", const="", metavar="FILTER",
                     help="List your chats with their @username and id (optional name filter)")
    sel.add_argument("--topics", action="store_true",
                     help="List the topics of the --target group with their ids")
    sel.add_argument("--fetch", nargs="?", const="", metavar="TITLE",
                     help="Search USERNAME_BOT_* bots for a title (prompt if omitted)")
    p.add_argument("--topic", type=int, default=None,
                   help="Topic id to post into, for groups with Topics (1 = General)")
    p.add_argument("--keep-caption", action="store_true", help="Copy the original caption text")
    p.add_argument("--caption", default=None, help="Use this caption instead")
    p.add_argument("--limit", type=int, default=200, help="How far back --last/--search look")
    p.add_argument("--session", default="tg_resend", help="Session file name")
    return p.parse_args()


async def collect(client, source, args):
    if args.ids:
        msgs = await client.get_messages(source, ids=args.ids)
        return [m for m in msgs if m and m.media]
    found = []
    async for m in client.iter_messages(source, limit=args.limit, search=args.search):
        if m.media:
            found.append(m)
            if args.last and len(found) >= args.last:
                break
    return list(reversed(found))  # oldest first, keeps original order


async def resend(client, target, msg, args):
    if args.caption is not None:
        caption, entities = args.caption, None
    elif args.keep_caption:
        caption, entities = msg.message or "", msg.entities
    else:
        caption, entities = "", None

    while True:
        try:
            await client.send_file(
                target,
                msg.media,                 # reuse by reference: no download/upload
                caption=caption,
                formatting_entities=entities,
                # posting into a forum topic = replying to the topic's id; General (1) needs none
                reply_to=args.topic if args.topic and args.topic != 1 else None,
                # no buttons= argument, so no inline keyboard is attached
            )
            return
        except FloodWaitError as e:
            print(f"  flood wait: sleeping {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)


async def get_forum_topics(client, group):
    """Return [(topic_id, title), ...] for a forum group."""
    from telethon.tl import functions
    peer = await client.get_input_entity(group)
    common = dict(offset_date=None, offset_id=0, offset_topic=0, limit=100)
    attempts = []
    if hasattr(functions.messages, "GetForumTopicsRequest"):
        attempts.append(lambda: functions.messages.GetForumTopicsRequest(peer=peer, **common))
    if hasattr(functions.channels, "GetForumTopicsRequest"):
        attempts.append(lambda: functions.channels.GetForumTopicsRequest(channel=peer, **common))
    last = None
    for make in attempts:
        try:
            res = await client(make())
        except Exception as e:  # wrong signature / not a forum
            last = e
            continue
        out = []
        for t in res.topics:
            out.append((t.id, getattr(t, "title", "<deleted>")))
        return out
    raise RuntimeError(
        f"Couldn't list topics ({last if attempts else 'unsupported Telethon version'}).\n"
        "Fallback: in Telegram, right-click the topic -> Copy Link; "
        "the last number in t.me/c/<group>/<N> is the topic id."
    )


async def list_topics(client, group):
    """Print a forum group's topics. Handles both old and new Telegram API layers."""
    for tid, title in await get_forum_topics(client, group):
        print(f"{tid:>8}  {title}")


def bot_usernames_from_env():
    """Unique @usernames from USERNAME_BOT_1 .. USERNAME_BOT_N in env order."""
    seen = set()
    bots = []
    for key, val in sorted(os.environ.items(), key=lambda kv: kv[0]):
        if not re.fullmatch(r"USERNAME_BOT_\d+", key):
            continue
        u = val.strip()
        if not u:
            continue
        if not u.startswith("@"):
            u = "@" + u
        if u.lower() in seen:
            continue
        seen.add(u.lower())
        bots.append(u)
    return bots


def search_relevance(query, msg):
    """Higher = better match for ordering results."""
    text = label(msg).lower()
    q = query.lower().strip()
    if not text or not q:
        return 0
    score = 0
    if q in text:
        score += 100 + len(q)
    for word in q.split():
        if word in text:
            score += 10
    return score


async def search_bots(client, query, bots, limit):
    """Search each bot chat; return [(bot_username, message), ...]."""
    hits = []
    seen_ids = set()
    for bot in bots:
        print(f"  searching {bot} ...", flush=True)
        try:
            async for m in client.iter_messages(bot, limit=limit, search=query):
                if not m.media:
                    continue
                key = (bot.lower(), m.id)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                hits.append((bot, m))
        except Exception as e:
            print(f"    skip ({e})", flush=True)
    hits.sort(key=lambda hm: search_relevance(query, hm[1]), reverse=True)
    return hits


async def find_dialog(client, name_substring):
    """Find a dialog whose title contains name_substring (case-insensitive)."""
    needle = name_substring.lower()
    matches = []
    async for d in client.iter_dialogs():
        if needle in (d.name or "").lower():
            matches.append(d)
    return matches


def parse_choice_indices(raw, count):
    """Parse 1-based selections: 2, 1-3, 1,3,5-7 -> sorted unique 0-based indices."""
    indices = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not a.isdigit() or not b.isdigit():
                return None
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            for n in range(lo, hi + 1):
                if n < 1 or n > count:
                    return None
                indices.append(n - 1)
        elif part.isdigit():
            n = int(part)
            if n < 1 or n > count:
                return None
            indices.append(n - 1)
        else:
            return None
    if not indices:
        return None
    return sorted(set(indices))


def prompt_choice(prompt, count, allow_all=False):
    while True:
        multi = ", 1-3" if count > 1 else ""
        extra = " or 'all'" if allow_all and count > 1 else ""
        raw = input(f"{prompt} [1-{count}{multi}{extra}, {CANCEL_HINT}]: ").strip()
        check_cancel(raw)
        if allow_all and raw.lower() == "all":
            return list(range(count))
        picked = parse_choice_indices(raw, count)
        if picked is not None:
            return picked
        print("  Invalid choice, try again.")


def prompt_line(prompt, default=None):
    suffix = f" [{CANCEL_HINT}]"
    if default is not None:
        suffix = f" [{default}, {CANCEL_HINT}]"
    raw = input(f"{prompt}{suffix}: ").strip()
    check_cancel(raw)
    if not raw and default is not None:
        return default
    return raw


async def interactive_fetch(client, args):
    title = (args.fetch or "").strip()
    if not title:
        title = prompt_line("Movie / series name to search")
    if not title:
        sys.exit("No search title given.")

    bots = bot_usernames_from_env()
    if not bots:
        sys.exit("No USERNAME_BOT_* entries in .env")

    print(f"\nSearching {len(bots)} bot(s) for {title!r} ...\n")
    hits = await search_bots(client, title, bots, args.limit)

    if not hits:
        print("No media found in any configured bot.")
        return

    print("\nFound:")
    for i, (bot, m) in enumerate(hits, 1):
        print(f"  {i:>2}.  {bot}  id={m.id}  {label(m)}")

    pick = prompt_choice("\nWhich file to send?", len(hits), allow_all=len(hits) > 1)
    selected = [hits[i] for i in pick]

    pirate_dialogs = await find_dialog(client, "PIRATE MOVIES PRO")
    pirate = None
    for d in pirate_dialogs:
        if getattr(d.entity, "username", None) == "pirate_movies_pro":
            pirate = d
            break
    if pirate is None and pirate_dialogs:
        pirate = pirate_dialogs[0]

    print("\nSend to:")
    print("  1.  Saved Messages (me)")
    if pirate:
        uname = getattr(pirate.entity, "username", None)
        print(f"  2.  {pirate.name}  (id {pirate.id}"
              f"{', @' + uname if uname else ''})")
    else:
        print("  (PIRATE MOVIES PRO not found in your dialogs — use --dialogs Pirate)")

    dest_raw = prompt_line("\nDestination [1=Saved Messages, 2=PIRATE MOVIES PRO]", default="1")
    target, topic = None, None

    if dest_raw == "2" and pirate:
        target = pirate.id
        print(f"\nTopics in {pirate.name} (id {pirate.id}):\n")
        try:
            topics = await get_forum_topics(client, target)
        except RuntimeError as e:
            sys.exit(str(e))
        if not topics:
            sys.exit("No topics returned for this group.")
        for i, (tid, ttitle) in enumerate(topics, 1):
            print(f"  {i:>2}.  {tid:>8}  {ttitle}")
        tpick = prompt_choice("\nWhich topic?", len(topics))
        topic = topics[tpick[0]][0]
    elif dest_raw == "2":
        sys.exit("PIRATE MOVIES PRO not available.")
    else:
        target = "me"

    send_args = SimpleNamespace(
        caption=args.caption,
        keep_caption=args.keep_caption,
        topic=topic,
    )

    for bot, m in selected:
        print(f"-> {bot} #{m.id}: {label(m)} -> {target}"
              + (f" (topic {topic})" if topic else ""))
        await resend(client, target, m, send_args)
        await asyncio.sleep(1)
    print(f"Done: {len(selected)} message(s) sent.")


def label(msg):
    if msg.file and msg.file.name:
        return msg.file.name
    return (msg.message or "").split("\n")[0][:60] or f"<{type(msg.media).__name__}>"


async def main():
    load_dotenv()
    args = parse_args()
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Set TG_API_ID and TG_API_HASH environment variables first.")

    def chat(x):
        return int(x) if x.lstrip("-").isdigit() else x

    async with TelegramClient(args.session, int(api_id), api_hash) as client:
        if args.fetch is not None:
            await interactive_fetch(client, args)
            return

        if args.dialogs is not None:
            needle = args.dialogs.lower()
            async for d in client.iter_dialogs():
                if needle and needle not in (d.name or "").lower():
                    continue
                uname = getattr(d.entity, "username", None)
                print(f"{d.id:>16}  {'@' + uname if uname else '-':<28}  {d.name}")
            return

        if args.topics:
            if not args.target:
                sys.exit("--topics needs --target <group>")
            await list_topics(client, chat(args.target))
            return

        if not args.source:
            sys.exit("--source is required (use --dialogs to find it)")
        source = chat(args.source)

        if args.list:
            async for m in client.iter_messages(source, limit=args.list):
                kind = "media" if m.media else "text "
                print(f"{m.id:>8}  {kind}  {label(m)}")
            return

        if not args.target:
            sys.exit("--target is required unless you use --list")
        target = chat(args.target)

        msgs = await collect(client, source, args)
        if not msgs:
            sys.exit("No matching media messages found.")

        for m in msgs:
            print(f"-> {m.id}: {label(m)}")
            await resend(client, target, m, args)
            await asyncio.sleep(1)  # be gentle with rate limits
        print(f"Done: {len(msgs)} message(s) sent.")


if __name__ == "__main__":
    asyncio.run(main())
