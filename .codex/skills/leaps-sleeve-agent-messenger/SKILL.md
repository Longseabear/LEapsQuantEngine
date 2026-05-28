---
name: leaps-sleeve-agent-messenger
description: Send operator messages to mapped LEapsQuantEngine sleeve or manager Codex agents through Codex multi-agent tools. Use when the user asks to message, ping, greet, ask status from, or forward instructions to an operational sleeve agent or manager role by sleeve id, sleeve name, manager role, sectionID/sessionID, or agent id; resolve targets through the section map and leave unmapped targets as `unkown`.
---

# LEaps Sleeve Agent Messenger

## Purpose

Route a short operator message from the current Codex thread to an existing
LEapsQuantEngine sleeve or manager Codex agent. This skill is
communication-only: do not mutate engine runtime state, write trading artifacts,
or submit broker orders as part of sending the message.

## Target Map

Before resolving a sleeve name or manager role, read
`references/sleeve-session-map.md`. The operator may say `sectionID`,
`sessionID`, `session id`, or `agent id`; for this skill they all mean the
`target` value used with `multi_agent_v1`.

The map uses `unkown` as the sentinel for unmapped sleeves. Also treat
`unknown`, blank, and `null` as unmapped if they appear later.

## Workflow

1. Resolve the target.
   - If the user gives a UUID-like agent id directly, use it directly for this
     message. Do not write it into the map unless the user explicitly says which
     sleeve it belongs to.
   - If the user gives a sleeve name, normalize common forms such as
     `Sleeve-LEaps`, `LEaps`, `us_etf_rotation`, `us etf rotation`,
     `semiconduct-kor`, `kr-domestic-4401`, `kr-core-compass`, `leaps-lowvol`, and
     `leaps lowvol`. Treat `leaps-lowvol` / `leaps lowvol` as the
     `kr-lowvol-defensive` sleeve.
   - If the user gives a manager role, resolve it through the `[Manager]`
     section. Normalize `news daily collector`, `news/daily collector`,
     `news collector`, `daily bar collector`, and the Korean role text shown in
     the manager map to the `news-daily-collector` manager row.
   - If the sleeve row has `section_id: unkown`, tell the user the sleeve is not
     mapped yet and ask for the section/session id instead of guessing.

2. Ensure the Codex multi-agent tools are available.
   - If `multi_agent_v1` tools are not already exposed, use `tool_search` with a
     query like `multi agent send_input resume_agent`.
   - If no multi-agent send tool is available, report that this Codex session
     cannot message sleeve agents. Never append to `.codex/sessions/*.jsonl`
     manually.

3. Resume the target before sending.
   - Call `multi_agent_v1.resume_agent` with the resolved id.
   - Use the result only as a reachability/status check. Do not infer a sleeve
     mapping from a response unless the user asks to record that mapping.

4. Send the message.
   - Use `multi_agent_v1.send_input`.
   - Set `interrupt=false` by default.
   - Set `interrupt=true` only when the user explicitly asks to interrupt,
     redirect, stop current work, or handle the message immediately.
   - Use the `message` field for plain text. Use `items` only when forwarding
     structured mentions, images, or skill references.
   - Preserve the user's message text and language. Do not transform a chat
     message into an order, contract, or trading decision.

5. Report the result.
   - Return the target sleeve or direct id and the `submission_id`.
   - If the user asks for the reply, call `resume_agent` again after sending and
     relay the latest completed/status text. If there is no new reply yet, say
     that the message is queued or still running.
   - Do not close operational sleeve agents after messaging them.

## Updating The Map

When the user gives a verified sleeve-to-sectionID pair or manager role target,
update only `references/sleeve-session-map.md`. Keep one row per sleeve or
manager role. Preserve `unkown` for targets that are still unmapped.
