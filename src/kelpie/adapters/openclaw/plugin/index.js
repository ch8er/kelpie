// Kelpie plugin for OpenClaw: forwards hook events to kelpie_hook.py, which appends them to
// Kelpie's spool, and enforces Kelpie's stops. Plain JavaScript (no build step, no
// dependencies); OpenClaw loads it in the Gateway process. See README.md in this directory's
// parent for install and privacy.
//
// Each forwarded hook starts `python3 kelpie_hook.py` with a reduced copy of the event on
// stdin: prompts, messages, tool results and assistant text are dropped here, before they
// leave the Gateway. Observation hooks are never awaited and never throw, so a missing or
// slow Python cannot hold up an agent run; at most MAX_RUNNING hook processes run at once and
// events beyond MAX_QUEUED waiting ones are dropped (counted in the log).
//
// Stop. Kelpie stops a session by writing <stopDir>/<sessionId>.json ({"at", "reason"}).
// While that marker holds (stopTtl seconds after its "at"), this plugin, in-process and
// without starting Python:
// - refuses every tool call of the session (before_tool_call: block, "Stopped by Kelpie: ...");
// - refuses every new run of the session that is not an owner's message (before_agent_run:
//   heartbeats, cron wakes, inter-session deliveries; needs conversation access);
// - lifts the stop when the owner sends the session a message (before_agent_run with trigger
//   "user" and senderIsOwner): the message goes through, the marker is deleted;
// - lifts it when the session is reset, started anew or deleted (session_end).
// Kelpie also aborts the run in flight through the Gateway (sessions.abort); see stop.py.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, readSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HOOK = join(dirname(fileURLToPath(import.meta.url)), "kelpie_hook.py");
const HOOK_TIMEOUT_MS = 10000;
const MAX_RUNNING = 8;
const MAX_QUEUED = 256;
const MAX_QUEUED_BYTES = 32 * 1024 * 1024; // what waiting events may hold in Gateway memory
// Tool arguments larger than this (as JSON) are not sent to the hook: the plugin hashes them
// itself (paramsDigest) and the hook records that hash.
const MAX_PARAMS = 1024 * 1024;
// Variables the hook process gets: nothing else of the Gateway's environment (which holds
// provider keys) reaches it.
const ENV_KEEP = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"];
const DEFAULT_STOP_DIR = "~/.openclaw/kelpie/stop";
const DEFAULT_STOP_TTL = 24 * 3600; // seconds a stop marker holds unless a human lifts it
const MAX_MARKER = 4096; // bytes; a larger marker file is ignored
const MAX_REASON = 500;
const FUTURE_SKEW = 300; // seconds; a marker dated further ahead is ignored
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;
// session_end reasons that lift a stop: a person reset or replaced the conversation. Idle,
// daily and compaction rollovers and Gateway shutdowns or restarts do not.
const LIFTING_ENDS = new Set(["new", "reset", "deleted"]);

function expand(path) {
  return typeof path === "string" && path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

function text(value) {
  return typeof value === "string" ? value : undefined;
}

// JSON for the hook; BigInt (which JSON.stringify refuses) becomes its decimal text.
function toJson(value) {
  return JSON.stringify(value, (_key, v) => (typeof v === "bigint" ? v.toString() : v));
}

// The size of a tool result in characters: its text parts when it has the usual
// { content: [{ text }] } shape (no serialisation of large results on the event loop), else
// its JSON.
function outputChars(result) {
  if (result === undefined || result === null) return undefined;
  if (typeof result === "string") return result.length;
  if (Array.isArray(result.content)) {
    let total = 0;
    for (const part of result.content) {
      if (typeof part?.text === "string") total += part.text.length;
    }
    return total;
  }
  try {
    return toJson(result).length;
  } catch {
    return undefined;
  }
}

// Tool arguments for the hook: as they are, or, when larger than MAX_PARAMS (or not JSON),
// left out with a digest of their JSON in their place (loop detection compares digests).
function paramsFor(params) {
  let json;
  try {
    json = toJson(params ?? null);
  } catch {
    return { params: null };
  }
  if (json === undefined || json.length <= MAX_PARAMS) return { params };
  return { params: null, paramsDigest: createHash("sha256").update(json).digest("hex").slice(0, 16) };
}

// The fields kelpie_hook.py reads, per hook; nothing else is passed on.
const REDUCE = {
  session_start: (e) => ({ sessionId: e.sessionId, sessionKey: e.sessionKey, resumedFrom: e.resumedFrom }),
  session_end: (e) => ({
    sessionId: e.sessionId,
    sessionKey: e.sessionKey,
    reason: e.reason,
    nextSessionId: e.nextSessionId,
  }),
  after_tool_call: (e) => ({
    toolName: e.toolName,
    ...paramsFor(e.params),
    runId: e.runId,
    toolCallId: e.toolCallId,
    error: e.error ? "error" : undefined, // whether it failed, never the message
    durationMs: e.durationMs,
    outputChars: e.error ? undefined : outputChars(e.result),
  }),
  llm_output: (e) => ({
    runId: e.runId,
    sessionId: e.sessionId,
    provider: e.provider,
    model: e.model,
    usage: e.usage
      ? { input: e.usage.input, output: e.usage.output, cacheRead: e.usage.cacheRead, cacheWrite: e.usage.cacheWrite }
      : undefined,
  }),
  agent_end: (e) => ({ runId: e.runId, success: e.success, durationMs: e.durationMs }),
  // Not OpenClaw hooks: what this plugin did about a stop.
  kelpie_stopped: (e) => ({ event: e.event }),
  kelpie_resumed: (e) => ({ event: e.event }),
};

function reduceCtx(ctx) {
  const c = ctx || {};
  return {
    sessionId: text(c.sessionId),
    sessionKey: text(c.sessionKey),
    agentId: text(c.agentId),
    runId: text(c.runId),
    toolCallId: text(c.toolCallId),
    trigger: text(c.trigger),
  };
}

export function createForwarder(settings, logger, run = spawn) {
  const env = {};
  for (const key of ENV_KEEP) if (process.env[key] !== undefined) env[key] = process.env[key];
  if (settings.spool) env.KELPIE_OPENCLAW_SPOOL = expand(settings.spool);
  if (settings.commands === false) env.KELPIE_OPENCLAW_SPOOL_COMMANDS = "0";
  if (Number.isInteger(settings.maxBytes) && settings.maxBytes > 0) {
    env.KELPIE_OPENCLAW_SPOOL_MAX_BYTES = String(settings.maxBytes);
  }
  const python = settings.python || "python3";
  const queue = [];
  let queuedBytes = 0;
  let running = 0;
  let dropped = 0;
  let failed = 0;

  // Runs from hook handlers and from child-process callbacks: nothing may escape it (an
  // uncaught exception in a callback ends the Gateway).
  function next() {
    try {
      while (running < MAX_RUNNING && queue.length > 0) {
        const payload = queue.shift();
        queuedBytes -= payload.length;
        start(payload);
      }
    } catch (err) {
      report(err);
    }
  }

  function start(payload) {
    running += 1;
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      running -= 1;
      setImmediate(next); // never recurse from inside a callback
    };
    let child;
    try {
      // -I: the hook runs isolated (no user site, no current or script directory on sys.path).
      child = run(python, ["-I", HOOK], {
        env,
        stdio: ["pipe", "ignore", "ignore"],
        timeout: HOOK_TIMEOUT_MS,
        killSignal: "SIGKILL",
      });
    } catch (err) {
      report(err);
      finish();
      return;
    }
    try {
      child.on("error", (err) => {
        report(err);
        finish();
      });
      child.on("close", finish);
      // Without file descriptors spawn returns a child with no stdio; "error" follows.
      if (!child.stdin) return;
      child.stdin.on("error", () => {});
      child.stdin.end(payload);
    } catch (err) {
      report(err);
      finish();
    }
  }

  function warn(message) {
    try {
      logger?.warn?.(message);
    } catch {
      // a throwing logger must not take the Gateway down with it
    }
  }

  function report(err, what = `run ${python} ${HOOK}`) {
    failed += 1;
    if (failed === 1 || failed % 1000 === 0) {
      warn(`kelpie: cannot ${what} (${failed} failures): ${err?.message || err}`);
    }
  }

  return function forward(hook, event, ctx) {
    try {
      const reduce = REDUCE[hook];
      if (!reduce) return;
      const message = { hook, at: Date.now() / 1000, event: reduce(event || {}), ctx: reduceCtx(ctx) };
      let payload;
      try {
        payload = toJson(message);
      } catch {
        // e.g. circular params: the call is still recorded, without its arguments hash input
        payload = toJson({ ...message, event: { ...message.event, params: null } });
      }
      if (queue.length >= MAX_QUEUED || queuedBytes + payload.length > MAX_QUEUED_BYTES) {
        dropped += 1;
        if (dropped === 1 || dropped % 1000 === 0) warn(`kelpie: ${dropped} hook events dropped (hook too slow)`);
        return;
      }
      queue.push(payload);
      queuedBytes += payload.length;
      next();
    } catch (err) {
      report(err, `forward ${hook}`);
    }
  };
}

function cleanReason(reason) {
  const clean = reason.replace(/[\x00-\x1f\x7f]+/g, " ").trim() || "no reason given";
  return clean.length <= MAX_REASON ? clean : `${clean.slice(0, MAX_REASON - 3)}...`;
}

// Stop markers: read without following a symlink or blocking on a FIFO, bounded, and only
// while they hold. Any problem reading one means "no stop": a broken marker must not stall
// the session forever.
export function createStops(settings, logger) {
  const dir = expand(settings.stopDir || DEFAULT_STOP_DIR);
  const ttl = Number.isFinite(settings.stopTtl) && settings.stopTtl > 0 ? settings.stopTtl : DEFAULT_STOP_TTL;
  const warned = new Set();

  function path(sessionId) {
    return typeof sessionId === "string" && SESSION_ID.test(sessionId) ? join(dir, `${sessionId}.json`) : undefined;
  }

  function active(sessionId, now = Date.now() / 1000) {
    const file = path(sessionId);
    if (!file) return undefined;
    let fd;
    try {
      fd = openSync(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    } catch {
      return undefined; // no marker (the usual case), or a symlink
    }
    try {
      if (!fstatSync(fd).isFile()) return undefined;
      const buf = Buffer.alloc(MAX_MARKER + 1);
      const size = readSync(fd, buf, 0, buf.length, 0);
      if (size > MAX_MARKER) return undefined;
      const data = JSON.parse(buf.subarray(0, size).toString("utf8"));
      const at = data?.at;
      if (typeof at !== "number" || typeof data.reason !== "string") return undefined;
      if (!(now - ttl <= at && at <= now + FUTURE_SKEW)) return undefined;
      return cleanReason(data.reason);
    } catch (err) {
      if (!warned.has(file)) {
        warned.add(file);
        logger?.warn?.(`kelpie: unreadable stop marker ${file}: ${err?.message || err}`);
      }
      return undefined;
    } finally {
      closeSync(fd);
    }
  }

  function lift(sessionId) {
    const file = path(sessionId);
    if (!file) return false;
    try {
      unlinkSync(file);
      return true;
    } catch {
      return false;
    }
  }

  return { active, lift, path };
}

export default {
  id: "kelpie",
  name: "Kelpie",
  description: "Reports sessions, tool calls and token usage to Kelpie's spool, and enforces Kelpie's stops.",
  register(api) {
    const settings = api.pluginConfig || {};
    const forward = createForwarder(settings, api.logger);
    const stops = createStops(settings, api.logger);
    for (const hook of ["session_start", "after_tool_call", "llm_output", "agent_end"]) {
      api.on(hook, (event, ctx) => {
        forward(hook, event, ctx);
      });
    }
    api.on("session_end", (event, ctx) => {
      forward("session_end", event, ctx);
      if (LIFTING_ENDS.has(event?.reason) && stops.lift(event?.sessionId ?? ctx?.sessionId)) {
        forward("kelpie_resumed", { event: "session_end" }, { ...ctx, sessionId: event?.sessionId ?? ctx?.sessionId });
      }
    });
    // Never throws (OpenClaw fails a throwing before_tool_call closed: the tool would be refused).
    api.on("before_tool_call", (_event, ctx) => {
      try {
        const reason = stops.active(ctx?.sessionId);
        if (reason === undefined) return undefined;
        forward("kelpie_stopped", { event: "before_tool_call" }, ctx);
        return { block: true, blockReason: `Stopped by Kelpie: ${reason}` };
      } catch {
        return undefined;
      }
    });
    api.on("before_agent_run", (event, ctx) => {
      try {
        const reason = stops.active(ctx?.sessionId);
        if (reason === undefined) return { outcome: "pass" };
        if (ctx?.trigger === "user" && event?.senderIsOwner === true) {
          stops.lift(ctx.sessionId); // the owner resumes the session: the message goes through
          forward("kelpie_resumed", { event: "before_agent_run" }, ctx);
          return { outcome: "pass" };
        }
        forward("kelpie_stopped", { event: "before_agent_run" }, ctx);
        return {
          outcome: "block",
          reason: `Stopped by Kelpie: ${reason}`,
          message: `Stopped by Kelpie: ${reason}. The session resumes when its owner sends it a message.`,
        };
      } catch {
        return { outcome: "pass" };
      }
    });
  },
};
