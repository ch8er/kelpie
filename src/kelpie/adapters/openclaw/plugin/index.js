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
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HOOK = join(dirname(fileURLToPath(import.meta.url)), "kelpie_hook.py");
const HOOK_TIMEOUT_MS = 10000;
const MAX_RUNNING = 8;
const MAX_QUEUED = 256;
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

function outputChars(result) {
  if (result === undefined || result === null) return undefined;
  if (typeof result === "string") return result.length;
  try {
    return toJson(result).length;
  } catch {
    return undefined;
  }
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
    params: e.params,
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
  const python = settings.python || "python3";
  const queue = [];
  let running = 0;
  let dropped = 0;
  let failed = 0;

  function next() {
    while (running < MAX_RUNNING && queue.length > 0) {
      const payload = queue.shift();
      running += 1;
      let child;
      const done = () => {
        running -= 1;
        next();
      };
      try {
        child = run(python, [HOOK], { env, stdio: ["pipe", "ignore", "ignore"], timeout: HOOK_TIMEOUT_MS });
      } catch (err) {
        report(err);
        done();
        continue;
      }
      let settled = false;
      const finish = () => {
        if (!settled) {
          settled = true;
          done();
        }
      };
      child.on("error", (err) => {
        report(err);
        finish();
      });
      child.on("close", finish);
      child.stdin.on("error", () => {});
      child.stdin.end(payload);
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
