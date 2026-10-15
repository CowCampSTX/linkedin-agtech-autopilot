"""LinkedIn AgTech autopilot: news in, drafts queued, posts out.

  python -m autopilot.cli fetch                     # pull AgTech news, show the best stories
  python -m autopilot.cli draft --count 3           # Claude writes drafts from the top stories (needs ANTHROPIC_API_KEY)
  python -m autopilot.cli add --text-file post.txt --source-url URL --source-title "..."
  python -m autopilot.cli list                      # the queue, with any rule problems
  python -m autopilot.cli review                    # write data/review.md for a human to read
  python -m autopilot.cli approve AG-0003 | reject AG-0003 --note "why" | edit AG-0003 --text-file post.txt
  python -m autopilot.cli post --id AG-0003 --confirm   # publish one approved draft now
  python -m autopilot.cli whoami                    # check the saved LinkedIn login (posts nothing)
  python -m autopilot.cli run --confirm             # scheduled job: fetch, top up drafts, post if due

Posting rules (a human stays in charge of what goes out under their name):
- Every post needs --confirm. Without it, post/run print what they would do and stop.
- With [linkedin].autopost = false, only drafts a person approved can post.
- With autopost = true, run also posts pending drafts that pass every check.
- A draft that fails a check in autopilot/core.check_post never posts.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import core as li
from .env import optional, require

ROOT = li.ROOT
DATA = ROOT / "data"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_local(cfg):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(cfg["linkedin"].get("timezone", "UTC")))
    except Exception:  # Windows without the tzdata package: fall back to the computer's clock
        return datetime.now().astimezone()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def used_urls(queue):
    return {li.norm_url(d["source"]["url"]) for d in queue if d.get("source", {}).get("url")}


def find(queue, draft_id):
    for d in queue:
        if d["id"].lower() == draft_id.lower():
            return d
    sys.exit(f"No draft {draft_id}. See: python -m autopilot.cli list")


def add_draft(queue, cfg, text, first_comment, source, meta=None):
    d = {"id": li.next_id(queue, cfg["profile"]["content_id_prefix"]), "status": "pending", "created_at": stamp(),
         "text": text.strip(), "first_comment": first_comment.strip(), "source": source,
         "issues": li.check_post(text, cfg), **(meta or {})}
    queue.append(d)
    return d


# ---------------------------------------------------------------- steps

def cmd_fetch(args, cfg, queue):
    items, errors = li.fetch_news(cfg)
    stories = li.pick_stories(items, cfg, used_urls(queue))
    out = DATA / "news.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([dict(s, published=s["published"].isoformat() if s["published"] else None) for s in stories],
                              indent=2, ensure_ascii=False))
    for e in errors:
        print(f"  feed skipped: {e}")
    print(f"{len(items)} stories from {len(cfg['sources']['feeds']) - len(errors)} feeds; {len(stories)} fresh and on topic -> {out.relative_to(ROOT)}")
    for s in stories[:args.show]:
        print(f"  [{s['score']:>2}] {s['source']}: {s['title']}\n       {s['url']}")
    return stories


def draft_models():  # imported here so fetch/list/post run without pydantic installed
    from pydantic import BaseModel, Field

    class Draft(BaseModel):
        story_url: str = Field(description="URL of the story this post is about, exactly as supplied.")
        pillar: int = Field(description="Pillar number (1-based) this post serves.")
        post: str = Field(description="The full LinkedIn post text, hook first, hashtags last, no URLs.")
        first_comment: str = Field(description="First comment: 'Source: <publication>, <headline>' then the URL on its own line.")
        planted_question: str = Field(description="The one question the hook plants, and the line that pays it off.")
        why: str = Field(description="One sentence: why this story will start a conversation with AgTech people.")

    class Batch(BaseModel):
        drafts: list[Draft]

    return Batch


def system_prompt(cfg):
    pos, rules = cfg["positioning"], cfg["post_rules"]
    facts = cfg.get("facts", {}).get("approved") or ["(none yet: do not state any personal background)"]
    pillars = "\n".join(f"{i}. {p}" for i, p in enumerate(pos["pillars"], 1))
    return f"""You write LinkedIn posts for {cfg['profile']['name']}, who works cattle and knows technology, and bridges the two in AgTech.
Goal: thoughtful comments and conversations with AgTech executives, founders and operators who see him as a peer.
He is not job hunting in public: never frame a post around wanting a job.

Audience: {pos['audience']}
Voice: {pos['voice']}

Pillars:
{pillars}

True facts you may use about him (nothing else):
{chr(10).join('- ' + f for f in facts)}

Post rules:
{chr(10).join('- ' + r for r in rules['rules'])}
- Length {rules['min_chars']}-{rules['max_chars']} characters (aim for 900-1500). First line under {rules['hook_max_chars']} characters.
- {rules['hashtags_min']}-{rules['hashtags_max']} hashtags, only at the end. No URLs in the post.
- Never use: {', '.join(rules['banned_phrases'])}.
- Plain text only: no markdown, no bold, no emoji bullets. At most one emoji in the whole post.
- Use only facts from the story summary you are given. If the summary is thin, ask a sharper question instead of filling gaps.
"""


def cmd_draft(args, cfg, queue):
    import anthropic

    room = cfg["linkedin"].get("max_drafts_in_queue", 9) - sum(d["status"] in ("pending", "approved") for d in queue)
    count = min(args.count, room)
    if count <= 0:
        print("Queue is full; review or post what's there first.")
        return []
    if args.stories:
        stories = json.loads(Path(args.stories).read_text())
    else:
        news = DATA / "news.json"
        stories = json.loads(news.read_text()) if news.exists() else []
    stories = [s for s in stories if li.norm_url(s["url"]) not in used_urls(queue)][:count * 3]
    if not stories:
        print("No fresh stories. Run fetch first (or pass --stories).")
        return []
    recent = [d["text"].splitlines()[0] for d in queue[-12:]]
    ask = (f"Write {count} LinkedIn posts, each about a DIFFERENT story below. Pick the stories most likely to get "
           f"practical comments from ag and AgTech people; spread them across pillars. Don't reuse these recent hooks: "
           f"{json.dumps(recent)}\n\nStories:\n{json.dumps(stories, ensure_ascii=False, indent=1)}")
    if args.brief:
        ask += f"\n\nDirection for this batch: {args.brief}"
    model = cfg.get("models", {}).get("posts", "claude-opus-5-5")
    client = anthropic.Anthropic(api_key=require("ANTHROPIC_API_KEY"))
    resp = client.beta.messages.parse(model=model, max_tokens=8000, system=system_prompt(cfg),
                                      messages=[{"role": "user", "content": ask}], output_format=draft_models(),
                                      output_config={"effort": "high"})
    if resp.parsed_output is None:
        sys.exit(f"No drafts returned (stop_reason={resp.stop_reason})")
    by_url = {li.norm_url(s["url"]): s for s in stories}
    made = []
    for d in resp.parsed_output.drafts[:count]:
        s = by_url.get(li.norm_url(d.story_url))
        if not s:
            print(f"  dropped a draft about an unknown story: {d.story_url}")
            continue
        made.append(add_draft(queue, cfg, d.post, d.first_comment,
                              {"title": s["title"], "url": s["url"], "feed": s["source"], "published": s.get("published")},
                              {"pillar": d.pillar, "planted_question": d.planted_question, "why": d.why, "model": model}))
    for d in made:
        print(f"  {d['id']} {'OK' if not d['issues'] else 'NEEDS FIX: ' + '; '.join(d['issues'])}  {d['text'].splitlines()[0]}")
    return made


def cmd_add(args, cfg, queue):
    text = Path(args.text_file).read_text()
    comment = args.first_comment or f"Source: {args.source_title}\n{args.source_url}"
    d = add_draft(queue, cfg, text, comment, {"title": args.source_title, "url": args.source_url, "feed": args.feed})
    print(f"{d['id']} queued{'' if not d['issues'] else ' with problems: ' + '; '.join(d['issues'])}")


def cmd_list(args, cfg, queue):
    for d in queue:
        if args.status and d["status"] != args.status:
            continue
        issues = li.check_post(d["text"], cfg) if d["status"] in ("pending", "approved") else []
        extra = f"  !! {'; '.join(issues)}" if issues else ""
        when = d.get("posted_at") or d["created_at"]
        print(f"{d['id']}  {d['status']:<8} {when[:10]}  {d['text'].splitlines()[0][:80]}{extra}")
        if d.get("post_url"):
            print(f"         {d['post_url']}")


def cmd_review(args, cfg, queue):
    out = DATA / "review.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"# LinkedIn drafts to review ({datetime.now():%Y-%m-%d})\n",
             "Approve: `python -m autopilot.cli approve AG-xxxx`. Edit: save the new text to a file, then "
             "`edit AG-xxxx --text-file it.txt`.\n"]
    for d in queue:
        if d["status"] not in ("pending", "approved"):
            continue
        issues = li.check_post(d["text"], cfg)
        parts.append(f"## {d['id']} ({d['status']}, {len(d['text'])} chars)\n\nStory: [{d['source']['title']}]({d['source']['url']})\n")
        if issues:
            parts.append("**Fix before approving:** " + "; ".join(issues) + "\n")
        parts.append("```text\n" + d["text"] + "\n```\n\nFirst comment:\n\n```text\n" + d["first_comment"] + "\n```\n")
    out.write_text("\n".join(parts))
    print(f"wrote {out.relative_to(ROOT)}")


def cmd_set(args, cfg, queue, status):
    d = find(queue, args.id)
    if d["status"] == "posted":
        sys.exit(f"{d['id']} is already posted.")
    if status == "approved" and (issues := li.check_post(d["text"], cfg)):
        sys.exit(f"{d['id']} can't be approved yet: {'; '.join(issues)}")
    d.update(status=status, updated_at=stamp(), note=getattr(args, "note", None) or d.get("note"))
    print(f"{d['id']} -> {status}")


def cmd_edit(args, cfg, queue):
    d = find(queue, args.id)
    if d["status"] == "posted":
        sys.exit(f"{d['id']} is already posted.")
    d.update(text=Path(args.text_file).read_text().strip(), status="pending", updated_at=stamp())
    d["issues"] = li.check_post(d["text"], cfg)
    print(f"{d['id']} updated (back to pending){'' if not d['issues'] else ': ' + '; '.join(d['issues'])}")


def postable(d, cfg):
    ok = d["status"] == "approved" or (d["status"] == "pending" and cfg["linkedin"].get("autopost"))
    return ok and not li.check_post(d["text"], cfg)


def due(cfg, queue):
    """Today is a posting day, it's past posting time, and nothing went out in the last 20 hours."""
    now = now_local(cfg)
    if DAYS[now.weekday()] not in cfg["linkedin"]["post_days"]:
        return False, f"{DAYS[now.weekday()]} is not a posting day"
    if now.strftime("%H:%M") < cfg["linkedin"]["post_time_local"]:
        return False, f"before {cfg['linkedin']['post_time_local']}"
    last = max((d["posted_at"] for d in queue if d.get("posted_at")), default=None)
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=20):
        return False, "already posted today"
    return True, "due"


def publish(args, cfg, d):
    lic = cfg["linkedin"]
    if not args.confirm:
        print(f"DRY RUN: would post {d['id']} to LinkedIn as {cfg['profile']['name']}:\n\n{d['text']}\n\n"
              f"first comment: {d['first_comment']}\n\nAdd --confirm to post.")
        return
    expires = optional("LINKEDIN_TOKEN_EXPIRES")
    if expires and date.fromisoformat(expires) - date.today() < timedelta(days=7):
        print(f"WARNING: the LinkedIn token expires {expires}. Run python -m autopilot.connect again soon.")
    api = li.LinkedIn(require("LINKEDIN_ACCESS_TOKEN"), lic.get("api_version", "202605"))
    author = require("LINKEDIN_PERSON_URN").strip().strip('"').strip("'").strip()
    try:
        urn = api.create_post(author, d["text"])
    except li.LinkedInError as e:
        d.update(status="failed", error=str(e), updated_at=stamp())
        print(f"{d['id']} failed: {e}")
        return
    d.update(status="posted", post_urn=urn, post_url=li.LinkedIn.post_url(urn), posted_at=stamp())
    print(f"{d['id']} posted: {d['post_url']}")
    if lic.get("link_in_first_comment") and d.get("first_comment"):
        try:
            api.comment(author, urn, d["first_comment"])
        except li.LinkedInError as e:  # the post is live either way; say so and move on
            d["comment_error"] = str(e)
            print(f"  first comment failed (add the source link by hand): {e}")


def cmd_post(args, cfg, queue):
    if args.id:
        d = find(queue, args.id)
        if d["status"] != "approved":
            sys.exit(f"{d['id']} is {d['status']}; approve it first.")
        if issues := li.check_post(d["text"], cfg):
            sys.exit(f"{d['id']} fails checks: {'; '.join(issues)}")
    else:
        d = next((d for d in queue if postable(d, cfg)), None)
        if not d:
            print("Nothing ready to post (approve a draft, or turn on autopost).")
            return
    publish(args, cfg, d)


def cmd_whoami(args, cfg, queue):
    """Checks the saved LinkedIn login without posting anything."""
    api = li.LinkedIn(require("LINKEDIN_ACCESS_TOKEN"), cfg["linkedin"].get("api_version", "202605"))
    me = api.me()
    match = f"urn:li:person:{me.get('sub')}" == optional("LINKEDIN_PERSON_URN").strip().strip('"').strip("'").strip()
    print(f"LinkedIn login OK: {me.get('name', 'unknown name')}; person id {'matches' if match else 'DOES NOT match'} the saved one.")
    if not match:
        sys.exit(1)


def cmd_run(args, cfg, queue):
    """The scheduled job. Safe to run every day: it only posts on posting days, once a day."""
    cmd_fetch(argparse.Namespace(show=5), cfg, queue)
    waiting = sum(d["status"] in ("pending", "approved") for d in queue)
    if waiting < cfg["linkedin"]["posts_per_week"] and not args.no_draft:
        if not optional("ANTHROPIC_API_KEY"):
            print("Queue is low, but no Anthropic key is set; skipping drafts (add drafts by hand with the add command).")
        else:
            cmd_draft(argparse.Namespace(count=cfg["linkedin"]["posts_per_week"], stories=None, brief=None),
                      cfg, queue)
    ok, why = due(cfg, queue)
    if not ok:
        print(f"Not posting: {why}.")
        return
    d = next((d for d in queue if postable(d, cfg)), None)
    if not d:
        print("Due, but nothing is approved" + ("" if cfg["linkedin"].get("autopost") else " (autopost is off)") + ".")
        return
    publish(args, cfg, d)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.toml"), help="settings file (default config.toml)")
    ap.add_argument("--queue", help="queue file (default data/queue.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("--show", type=int, default=10)
    p = sub.add_parser("draft"); p.add_argument("--count", type=int, default=3)
    p.add_argument("--stories", help="JSON list of {title,url,summary,source} to draft from instead of the fetched news")
    p.add_argument("--brief")
    p = sub.add_parser("add"); p.add_argument("--text-file", required=True); p.add_argument("--source-url", required=True)
    p.add_argument("--source-title", required=True); p.add_argument("--feed", default=""); p.add_argument("--first-comment")
    p = sub.add_parser("list"); p.add_argument("--status")
    sub.add_parser("review")
    sub.add_parser("whoami")
    p = sub.add_parser("approve"); p.add_argument("id")
    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("--note")
    p = sub.add_parser("edit"); p.add_argument("id"); p.add_argument("--text-file", required=True)
    p = sub.add_parser("post"); p.add_argument("--id"); p.add_argument("--confirm", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--confirm", action="store_true"); p.add_argument("--no-draft", action="store_true")
    args = ap.parse_args()

    cfg = li.load_cfg(args.config)
    path = Path(args.queue) if args.queue else li.queue_path()
    queue = li.load_queue(path)
    handlers = {"fetch": cmd_fetch, "draft": cmd_draft, "add": cmd_add, "list": cmd_list, "review": cmd_review,
                "approve": lambda a, c, q: cmd_set(a, c, q, "approved"), "reject": lambda a, c, q: cmd_set(a, c, q, "rejected"),
                "edit": cmd_edit, "post": cmd_post, "run": cmd_run, "whoami": cmd_whoami}
    try:
        handlers[args.cmd](args, cfg, queue)
    finally:
        if args.cmd not in ("list", "review", "fetch", "whoami"):
            li.save_queue(path, queue)


if __name__ == "__main__":
    main()
