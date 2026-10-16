// Kelpie plugin for OpenClaw: forwards hook events to kelpie_hook.py, which appends them to
// Kelpie's spool. Plain JavaScript (no build step, no dependencies); OpenClaw loads it in the
// Gateway process. See README.md in this directory's parent for install and privacy.
//
// Each forwarded hook starts `python3 kelpie_hook.py` with a reduced copy of the event on
// stdin: prompts, messages, tool results and assistant text are dropped here, before they
// leave the Gateway. Observation hooks are never awaited and never throw, so a missing or
// slow Python cannot hold up an agent run; at most MAX_RUNNING hook processes run at once and
// events beyond MAX_QUEUED waiting ones are dropped (counted in the log).

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HOOK = join(dirname(fileURLToPath(import.meta.url)), "kelpie_hook.py");
const HOOK_TIMEOUT_MS = 10000;
const MAX_RUNNING = 8;
const MAX_QUEUED = 256;
// Tool arguments larger than this (as JSON) are not sent to the hook: the plugin hashes them
// itself (paramsDigest) and the hook records that hash.
const MAX_PARAMS = 1024 * 1024;
// Variables the hook process gets: nothing else of the Gateway's environment (which holds
// provider keys) reaches it.
const ENV_KEEP = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"];

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
  let running = 0;
  let dropped = 0;
  let failed = 0;

  // Runs from hook handlers and from child-process callbacks: nothing may escape it (an
  // uncaught exception in a callback ends the Gateway).
  function next() {
    try {
      while (running < MAX_RUNNING && queue.length > 0) {
        start(queue.shift());
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

  function report(err, what = `run ${python} ${HOOK}`) {
    failed += 1;
    if (failed === 1 || failed % 1000 === 0) {
      logger?.warn?.(`kelpie: cannot ${what} (${failed} failures): ${err?.message || err}`);
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
      if (queue.length >= MAX_QUEUED) {
        dropped += 1;
        if (dropped === 1 || dropped % 1000 === 0) logger?.warn?.(`kelpie: ${dropped} hook events dropped (hook too slow)`);
        return;
      }
      queue.push(payload);
      next();
    } catch (err) {
      report(err, `forward ${hook}`);
    }
  };
}

export default {
  id: "kelpie",
  name: "Kelpie",
  description: "Reports sessions, tool calls and token usage to Kelpie's spool.",
  register(api) {
    const forward = createForwarder(api.pluginConfig || {}, api.logger);
    for (const hook of Object.keys(REDUCE)) {
      api.on(hook, (event, ctx) => {
        forward(hook, event, ctx);
      });
    }
  },
};
