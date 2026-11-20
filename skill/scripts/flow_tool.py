"""Local workflow for Flow jobs. Browser work stays in the supported Chrome UI."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4


MAX_ATTEMPTS = 3
ASR_MODEL = "gpt-transcribe"
IMAGE_ASPECT_TOLERANCE = 0.04
VIDEO_ASPECT_TOLERANCE = 0.01
FIELDS = (
    "project", "account", "profile", "model", "mode", "resolution",
    "aspect", "seconds", "outputs", "refs", "prompt", "source_asset",
)
FLOW_HOSTS = ("flow.google.com", "labs.google")
PROJECT_PATH = re.compile(r"/project/([\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12})(?:/|$)", re.IGNORECASE)
RESOLUTION_ALIASES = {"4k": "2160p", "uhd": "2160p", "fullhd": "1080p", "fhd": "1080p", "hd": "720p"}
# Hook guard: direct access to the state database, or redirecting it via LOCALAPPDATA.
STATE_GUARD = re.compile(
    r"flow-video-state|LOCALAPPDATA[^\n;|&]*flow-video-generator|LOCALAPPDATA\s*=|LOCALAPPDATA\s*['\"]?\s*,",
    re.IGNORECASE,
)
STATES = {
    "prepared", "armed", "pending", "succeeded", "failed",
    "unusual_activity", "policy_refusal", "verified",
}
STEPS = {
    "arm": ("prepared", "failed"),
    # "succeeded" may only move to "failed": the downloaded result was rejected on review.
    # "prepared"/"failed" only accept a --manual result the owner submitted in Flow.
    "record": ("armed", "pending", "unusual_activity", "succeeded", "prepared", "failed"),
    "finish": ("succeeded",),
    "clear": ("unusual_activity",),
    "handoff": tuple(STATES - {"verified"}),
}
HOOK_STEPS = "arm|record|finish|handoff|clear"
HOOK_COMMAND = re.compile(
    rf"flow_tool\.py[\\\"'`]*\s+({HOOK_STEPS})\b(?:\s+[\\\"'`]*([a-f0-9]{{32}})\b)?",
    re.IGNORECASE,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(data, dict), "Expected a JSON object")
    return data


def project_id(url):
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    match = PROJECT_PATH.search(parts.path)
    require(parts.scheme == "https" and host in FLOW_HOSTS and match,
            "Expected an HTTPS Google Flow project URL (flow.google.com or labs.google) containing /project/<uuid>")
    return match[1].lower()


def normalize_resolution(value):
    value = re.sub(r"\s+", "", value).casefold()
    return RESOLUTION_ALIASES.get(value, value)


def normalize_input(data):
    fields = {name: data[name] for name in FIELDS if name in data}
    fields.setdefault("prompt", "")
    fields.setdefault("refs", [])
    fields.setdefault("source_asset", "")
    for name in ("project", "account", "profile", "model", "mode", "resolution", "aspect"):
        require(isinstance(fields.get(name), str) and fields[name].strip(), f"Missing {name}")
    for name in ("prompt", "source_asset"):
        require(isinstance(fields[name], str), f"{name} must be text")
    fields["project"] = project_id(fields["project"])
    fields["account"] = hashlib.sha256(fields["account"].strip().lower().encode()).hexdigest()
    fields["model"] = re.sub(r"[\s_-]+", "", fields["model"]).casefold()
    fields["mode"] = fields["mode"].strip().casefold()
    fields["profile"] = fields["profile"].strip()
    fields["aspect"] = re.sub(r"\s+", "", fields["aspect"]).replace("：", ":")
    fields["resolution"] = normalize_resolution(fields["resolution"])
    fields["prompt"] = re.sub(r"\s+", " ", fields["prompt"]).strip()
    require(type(fields.get("outputs")) is int and fields["outputs"] == 1, "Exactly one output is required")
    require(fields["aspect"] in ("16:9", "9:16", "1:1", "4:3", "3:4"), "Invalid aspect")
    require(isinstance(fields.get("refs"), list), "refs must be an ordered list")
    for ref in fields["refs"]:
        require(isinstance(ref, dict) and set(ref) == {"kind", "name"}, "Each reference needs kind and name")
        require(ref["kind"] in ("image", "character", "voice", "asset") and isinstance(ref["name"], str)
                and ref["name"].strip(), "Invalid reference")
    seconds = fields.get("seconds")
    require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0, "Invalid duration")
    return fields


def make_plan(data):
    data = {
        "model": "Veo 3.1 Lite", "mode": "ingredients", "resolution": "720p",
        "aspect": "16:9", "seconds": 8, "outputs": 1,
        "needs_audio": True, "characters": [], **data,
    }
    action, job = data["action"], data["job"]
    require(action in ("video", "image", "edit", "upscale", "download"), "Unknown action")
    require(isinstance(job, str) and job.strip(), "job must be the stable shot or asset ID")
    require(type(data.get("points_limit")) is int and data["points_limit"] >= 0,
            "Provide the user-authorized points limit explicitly")
    require(type(data["needs_audio"]) is bool, "needs_audio must be Boolean")
    ui = normalize_input(data)
    mode = ui["mode"]
    require(mode in ("text", "ingredients", "frames", "image", "edit", "upscale", "download"), "Invalid mode")
    if action == "video":
        require(mode in ("text", "ingredients", "frames"), "Video requires text, ingredients or frames mode")
        require(ui["seconds"] > 0 and ui["prompt"], "Video requires duration and prompt")
        if mode == "frames":
            kinds = [ref["kind"] for ref in ui["refs"]]
            images = 2 if kinds[:2] == ["image", "image"] else 1
            require(kinds[:1] == ["image"] and all(k in ("character", "voice") for k in kinds[images:]),
                    "Frames requires the start image first, an optional end image second, "
                    "then only character or voice elements")
        elif mode == "ingredients":
            require(ui["refs"], "Ingredients requires ordered references")
        else:
            require(not ui["refs"], "Text mode has no reference assets")
    if action == "image":
        require(mode == "image" and ui["seconds"] == 0 and ui["prompt"], "Image requires image mode, zero seconds and prompt")
        require(not data["needs_audio"], "Image does not require an audio stream")
    else:
        require(re.fullmatch(r"[1-9]\d*p", ui["resolution"]) and ui["seconds"] > 0,
                "Video requires a resolution such as 720p and a positive duration")
    if action in ("edit", "upscale", "download"):
        require(mode == action and ui["source_asset"].strip(), "Specify the matching mode and source asset")
        require(action != "edit" or ui["prompt"], "Edit requires a prompt")
    require(action != "download" or data["points_limit"] == 0, "Download does not spend points")
    chars = data["characters"]
    require(isinstance(chars, list) and all(isinstance(c, str) and c.strip() for c in chars),
            "characters must list the names appearing on screen")
    if action == "video":
        selected = {r["name"] for r in ui["refs"] if r["kind"] == "character"}
        note = data.get("character_exception", "")
        require(set(chars) <= selected or (isinstance(note, str) and note.strip()),
                "Attach the Flow character elements, or document the user-approved exception")
    return {"job": job.strip(), "action": action, "ui": ui, "points_limit": data["points_limit"],
            "needs_audio": data["needs_audio"], "characters": chars}


def state_path():
    root = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state"
    return Path(root) / "flow-video-generator" / "flow-video-state.sqlite3"


def open_state(create=True):
    path = state_path()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level="IMMEDIATE")
    db.row_factory = sqlite3.Row
    if create:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, job TEXT NOT NULL,
            plan TEXT NOT NULL, state TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
            attempt TEXT, evidence TEXT, asset TEXT, artifact TEXT, handoff TEXT,
            UNIQUE(project, job)
          );
          CREATE TABLE IF NOT EXISTS hook_jobs (
            session TEXT NOT NULL, turn TEXT NOT NULL, job TEXT NOT NULL,
            PRIMARY KEY(session, turn, job)
          );
        """)
    return db


def get_job(db, receipt):
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (receipt,)).fetchone()
    require(row, "Receipt missing. Run prepare first.")
    return row


def evidence(path):
    path = Path(path).resolve(strict=True)
    require(path.is_file(), "Evidence must be a screenshot file")
    with path.open("rb") as stream:
        head = stream.read(12)
    require(head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
            or head.startswith(b"RIFF") and head[8:] == b"WEBP", "Evidence is not a PNG, JPEG or WebP screenshot")
    return str(path)


def check_state(row, step):
    require(row["state"] in STEPS[step], f"{step} blocked at state {row['state']}")


def media_tools(name):
    path = os.environ.get(name.upper()) or shutil.which(name)
    if not path and os.name == "nt":
        path = Path("C:/tools/ffmpeg/bin") / f"{name}.exe"
    require(path and (Path(path).is_file() or shutil.which(str(path))), f"Set {name.upper()} to the installed executable")
    return str(path)


def run_tool(command, timeout):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise ValueError(f"{Path(command[0]).name} failed ({result.returncode}): " + " | ".join(detail))
    return result.stdout


def display_size(video):
    """Width and height as displayed: apply sample aspect ratio and ±90° rotation."""
    width, height = video["width"], video["height"]
    sar = video.get("sample_aspect_ratio", "1:1")
    if re.fullmatch(r"[1-9]\d*:[1-9]\d*", sar or "") and sar != "1:1":
        num, den = map(int, sar.split(":"))
        width = round(width * num / den)
    rotation = video.get("tags", {}).get("rotate")
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    try:
        if round(float(rotation or 0)) % 180 == 90:
            width, height = height, width
    except (TypeError, ValueError):
        pass
    return width, height


def validate_media(path, plan):
    path = Path(path).resolve(strict=True)
    picture = plan["action"] == "image"
    extensions = (".jpg", ".jpeg", ".png", ".webp") if picture else (".mp4", ".mov", ".webm")
    require(path.is_file() and path.suffix.lower() in extensions and path.stat().st_size > 0,
            "Unexpected or empty media file")
    data = json.loads(run_tool(
        [media_tools("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], 30,
    ))
    video = next((s for s in data["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    require(video and video.get("width", 0) > 0 and video.get("height", 0) > 0, "No usable picture stream")
    width, height = display_size(video)
    exact = picture and re.fullmatch(r"\d+x\d+", plan["ui"]["resolution"])
    if exact:
        require(f"{width}x{height}" == plan["ui"]["resolution"], "Image dimensions mismatch")
    else:
        aw, ah = map(int, plan["ui"]["aspect"].split(":"))
        tolerance = IMAGE_ASPECT_TOLERANCE if picture else VIDEO_ASPECT_TOLERANCE
        require(abs(width * ah / (height * aw) - 1) <= tolerance,
                f"Aspect ratio mismatch: {width}x{height} is not {plan['ui']['aspect']}")
    audio = any(s["codec_type"] == "audio" for s in data["streams"])
    duration = None
    if not picture:
        short_edge = int(plan["ui"]["resolution"][:-1])
        require(min(width, height) == short_edge, f"Resolution mismatch: {width}x{height}")
        duration = float(video.get("duration") or data["format"].get("duration") or 0)
        require(math.isfinite(duration) and abs(duration - plan["ui"]["seconds"]) <= 0.25,
                f"Duration mismatch: {duration:.3f}s")
        require(not plan["needs_audio"] or audio, "Missing required audio stream")
    run_tool(
        [media_tools("ffmpeg"), "-v", "error", "-xerror", "-i", str(path),
         "-map", f"0:{video['index']}", "-map", "0:a?", "-f", "null", "-"], 180,
    )
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"file": str(path), "width": width, "height": height, "seconds": duration,
            "has_audio": audio, "sha256": digest}


def execute(db, args):
    step = args.command
    if step == "prepare":
        plan = make_plan(read_json(args.plan))
        serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        project = plan["ui"]["project"]
        db.execute("BEGIN IMMEDIATE")
        found = db.execute("SELECT * FROM jobs WHERE project = ? AND job = ?", (project, plan["job"])).fetchone()
        if found:
            if found["plan"] == serialized:
                return {"receipt": found["id"], "state": found["state"]}
            require(found["state"] in ("prepared", "failed"),
                    f"Existing receipt is {found['state']}; its plan can only change while prepared or failed. "
                    "Resolve the current attempt first.")
            db.execute("UPDATE jobs SET plan=?, attempt=NULL, handoff=NULL WHERE id=?", (serialized, found["id"]))
            return {"receipt": found["id"], "state": found["state"], "plan_updated": True, "tries": found["tries"]}
        receipt = uuid4().hex
        db.execute("INSERT INTO jobs(id, project, job, plan, state) VALUES (?, ?, ?, ?, 'prepared')",
                   (receipt, project, plan["job"], serialized))
        return {"receipt": receipt, "state": "prepared"}

    if step == "finish":
        before = get_job(db, args.receipt)
        check_state(before, step)
        plan = json.loads(before["plan"])
        review = read_json(args.review)
        require(review.get("visual") == "PASS" and isinstance(review.get("notes"), str)
                and review["notes"].strip(), "Record the visual review and its observations")
        require(review.get("audio") in ("PASS", "NOT_RUN"), "Set review.audio to PASS or NOT_RUN")
        require(not plan["needs_audio"] or review["audio"] == "PASS",
                "Check the required audio with the gpt-transcribe MCP (transcribe_audio) before finishing. "
                "Never attach audio files or base64 audio to the model input.")
        if review["audio"] == "PASS":
            asr = review.get("asr")
            require(isinstance(asr, dict) and asr.get("model") == ASR_MODEL
                    and all(isinstance(asr.get(k), str) and asr[k].strip() for k in ("text", "request_id")),
                    f'audio PASS needs review.asr from the gpt-transcribe MCP: '
                    f'{{"model": "{ASR_MODEL}", "text": ..., "request_id": ...}}')
        artifact = validate_media(args.media, plan)
        artifact["review"] = review

    if step == "status":
        row = get_job(db, args.receipt)
        return {"receipt": row["id"], "job": row["job"], "state": row["state"],
                "attempt": row["attempt"], "tries": row["tries"], "evidence": row["evidence"],
                "asset": row["asset"], "handoff": row["handoff"], "artifact": json.loads(row["artifact"] or "null")}

    db.execute("BEGIN IMMEDIATE")
    row = get_job(db, args.receipt)
    check_state(row, step)

    if step == "arm":
        plan = json.loads(row["plan"])
        ui = read_json(args.observed)
        seen = normalize_input(ui)
        differences = [name for name in FIELDS if seen.get(name) != plan["ui"].get(name)]
        require(not differences, "Visible Flow settings mismatch: " + ", ".join(differences))
        cost, credits = ui.get("cost"), ui.get("credits")
        require(type(cost) is int and 0 <= cost <= plan["points_limit"], "Cost absent or over authorized limit")
        require(type(credits) is int and credits >= cost, "Insufficient or unverified credits")
        snapshot = evidence(ui["evidence"])
        total = db.execute("SELECT COALESCE(SUM(tries), 0) FROM jobs WHERE job = ?", (row["job"],)).fetchone()[0]
        require(total < MAX_ATTEMPTS,
                f"{total} attempts already used for job {row['job']} across all projects. Split the shot (split_required).")
        active = db.execute(
            "SELECT state FROM jobs WHERE project = ? AND state IN ('armed','pending','succeeded','unusual_activity')",
            (row["project"],),
        ).fetchone()
        require(not active, "Project has unresolved work or an unusual-activity stop")
        same_shot = db.execute(
            "SELECT state FROM jobs WHERE job=? AND id<>? "
            "AND state IN ('armed','pending','succeeded','unusual_activity','verified')",
            (row["job"], row["id"]),
        ).fetchone()
        require(not same_shot, "The same job already has an unresolved or completed result in another project")
        attempt = uuid4().hex
        state = "succeeded" if plan["action"] == "download" else "armed"
        db.execute("UPDATE jobs SET state=?, attempt=?, tries=tries+1, evidence=?, asset=?, handoff=NULL WHERE id=?",
                   (state, attempt, snapshot, plan["ui"]["source_asset"] if state == "succeeded" else None, row["id"]))
        return {"receipt": row["id"], "state": state, "attempt": attempt,
                "next": "download_existing" if state == "succeeded" else "one_flow_ui_submission"}
    if step == "record":
        if row["state"] in ("prepared", "failed"):
            require(args.manual and args.state == "succeeded",
                    f"No attempt is armed at state {row['state']}. Only an owner-submitted result already "
                    "visible in Flow can be recorded: record RECEIPT manual succeeded EVIDENCE --asset NAME --manual")
        else:
            require(row["attempt"] == args.attempt, "Attempt receipt mismatch")
        if row["state"] == "unusual_activity":
            require(args.manual and args.state == "succeeded",
                    "An unusual-activity stop can only accept a user-submitted result already visible in Flow. "
                    "Use clear RECEIPT REASON once the owner has resolved the stop.")
        if row["state"] == "succeeded":
            require(args.state == "failed",
                    "A succeeded result can only be recorded as failed (rejected after download review)")
        require(args.state != "succeeded" or args.asset.strip(), "Provide the exact visible result card name or URL")
        snapshot = evidence(args.evidence)
        db.execute("UPDATE jobs SET state=?, evidence=?, asset=?, handoff=NULL WHERE id=?",
                   (args.state, snapshot, args.asset.strip() or row["asset"], row["id"]))
        return {"receipt": row["id"], "state": args.state}
    if step == "clear":
        require(args.reason.strip(), "Explain how the owner resolved the unusual-activity stop")
        db.execute("UPDATE jobs SET state='failed', attempt=NULL, handoff=? WHERE id=?",
                   ("unusual_activity cleared: " + args.reason.strip(), row["id"]))
        return {"receipt": row["id"], "state": "failed", "tries": row["tries"]}
    if step == "handoff":
        require(args.reason.strip(), "Explain the blocker or pending work")
        db.execute("UPDATE jobs SET handoff=? WHERE id=?", (args.reason, row["id"]))
        return {"receipt": row["id"], "state": row["state"], "handoff": args.reason}

    reused = db.execute(
        "SELECT id, job FROM jobs WHERE id <> ? AND artifact IS NOT NULL AND json_extract(artifact, '$.sha256') = ?",
        (row["id"], artifact["sha256"]),
    ).fetchone()
    require(not reused, f"This media file already verified job {reused['job']} ({reused['id']}). "
                        "Download the result for this shot." if reused else "")
    db.execute("UPDATE jobs SET state='verified', artifact=?, handoff=NULL WHERE id=?",
               (json.dumps(artifact, ensure_ascii=False), row["id"]))
    return {"receipt": row["id"], "state": "verified", **artifact}


def deny(event, reason):
    return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def hook_command(tool_input):
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if isinstance(command, list):
        command = " ".join(map(str, command))
    return command if isinstance(command, str) else ""


def safe_hook(raw):
    """Hooks must exit 0 with valid output. Fail closed only for flow_tool commands in PreToolUse."""
    data = {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        return run_hook(data)
    except Exception as exc:  # noqa: BLE001 - a hook must never crash the host turn
        event = data.get("hook_event_name") if isinstance(data, dict) else None
        command = hook_command(data.get("tool_input") or {}) if isinstance(data, dict) else ""
        if event == "PreToolUse" and "flow_tool" in command:
            return deny(event, f"Flow workflow hook error: {exc}")
        return None


def run_hook(data):
    event = data["hook_event_name"]
    scope = (data["session_id"], data.get("turn_id", ""))
    if event == "Stop":
        if not state_path().is_file():
            return None
        with open_state(create=False) as db:
            unfinished = db.execute(
                "SELECT jobs.id, jobs.state FROM hook_jobs JOIN jobs ON jobs.id = hook_jobs.job "
                "WHERE session=? AND turn=? AND state!='verified' AND handoff IS NULL", scope,
            ).fetchall()
        if unfinished and not data.get("stop_hook_active"):
            jobs = ", ".join(f"{row['id']} ({row['state']})" for row in unfinished)
            return {"decision": "block", "reason": f"Flow work remains: {jobs}. Verify the result or record handoff; do not resubmit."}
        if unfinished:
            return {"systemMessage": "Flow still has unfinished work. Report its actual state."}
        return None

    tool_input = data.get("tool_input") or {}
    command = hook_command(tool_input)
    if data.get("tool_name") == "apply_patch":
        targets = "\n".join(re.findall(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$",
                                       command, re.MULTILINE))
        if event == "PreToolUse" and (state_path().name in targets or "flow-video-state" in targets):
            return deny(event, "Use flow_tool.py to access the Flow state database")
        return None
    if event == "PreToolUse" and STATE_GUARD.search(command):
        return deny(event, "Use flow_tool.py to access the Flow state database; "
                           "do not touch or redirect it (LOCALAPPDATA)")
    matches = list(HOOK_COMMAND.finditer(command))
    if not matches:
        return None
    if event == "PreToolUse":
        try:
            for match in matches:
                require(match[2], f"Run flow_tool.py {match[1].lower()} as its own command with the literal "
                                  "32-character receipt (no variables) so the workflow gate can track it")
            require(state_path().is_file(), "Run prepare before using a receipt")
            with open_state(create=False) as db:
                for match in matches:
                    step, receipt = match[1].lower(), match[2].lower()
                    check_state(get_job(db, receipt), step)
                    db.execute("INSERT OR IGNORE INTO hook_jobs VALUES (?, ?, ?)", (*scope, receipt))
        except (ValueError, sqlite3.Error) as exc:
            return deny(event, str(exc))
        return None
    if event == "PostToolUse" and state_path().is_file():
        notes = []
        with open_state(create=False) as db:
            for receipt in dict.fromkeys(m[2].lower() for m in matches if m[2]):
                row = db.execute("SELECT state FROM jobs WHERE id = ?", (receipt,)).fetchone()
                if row and row["state"] in ("failed", "unusual_activity", "policy_refusal"):
                    notes.append(f"Flow receipt {receipt}: {row['state']}. Read status and record handoff as needed.")
        if notes:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": " ".join(notes)}}
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").add_argument("plan")
    sub.add_parser("hook")
    for step in ("status", "arm", "record", "finish", "handoff", "clear"):
        sub.add_parser(step).add_argument("receipt")
    sub.choices["arm"].add_argument("observed")
    sub.choices["record"].add_argument("attempt")
    sub.choices["record"].add_argument("state", choices=(
        "pending", "succeeded", "failed", "unusual_activity", "policy_refusal",
    ))
    sub.choices["record"].add_argument("evidence")
    sub.choices["record"].add_argument("--asset", default="")
    sub.choices["record"].add_argument("--manual", action="store_true", help="Observe an existing result submitted by the user")
    sub.choices["finish"].add_argument("media")
    sub.choices["finish"].add_argument("review")
    sub.choices["handoff"].add_argument("reason")
    sub.choices["clear"].add_argument("reason")
    args = parser.parse_args()
    if args.command == "hook":
        result = safe_hook(sys.stdin.buffer.read())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        require(args.command != "status" or state_path().is_file(), "No Flow state yet. Run prepare first.")
        with open_state() as db:
            result = execute(db, args)
        print(json.dumps(result, ensure_ascii=False))
    except KeyError as exc:
        print(json.dumps({"error": f"Missing required field {exc}"}, ensure_ascii=False))
        return 1
    except (ValueError, TypeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
