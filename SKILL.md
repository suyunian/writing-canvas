---
name: writing-canvas
description: "在本地写作画布中创建、编辑和预览 Markdown 文档。用户明确要求‘用画布输出’、‘用写作块输出’、‘在写作画布中生成’、‘打开写作画布’或同义表达时使用；如果本会话已创建或打开画布，后续修改当前文档、调整内容、继续写作或新建文档也继续使用本 Skill；不要因与画布无关的普通写作、保存或润色请求自动触发。画布选区来自 Codex 内置浏览器，修改建议在本地画布中预览并确认。"
---

# Writing Canvas

Use this skill only for an explicit writing-canvas request. Keep the service and
runtime data in the user-level paths below; never write the current project or
use a database, cloud service, account system, or system browser.

## Paths and runtime

- Skill files: `/Users/suyunian/.codex/skills/writing-canvas`
- Runtime data: `/Users/suyunian/.codex/writing-canvas-data`
- Local URL: `http://127.0.0.1:39173/`
- Service command: `python3 /Users/suyunian/.codex/skills/writing-canvas/scripts/canvas.py ensure`

The service must bind only to `127.0.0.1`. `ensure` initializes the runtime
document, reuses a healthy existing service, or starts one in the background.
Documents are stored independently under the runtime `documents/` directory.
The runtime uses the indexed `main` document directly; legacy root-level files
are not created or migrated.

## Generate or open a canvas

1. Generate the requested Markdown in the conversation.
2. For a new document, pipe the Markdown to `canvas.py create --title <title>`;
   for an existing document, use the proposal flow below for modifications.
   Use `canvas.py write --document-id <id>` only when the user explicitly asks
   to apply or replace content immediately.
3. Run `canvas.py ensure` and use its URL.
4. When `codex_app__open_in_codex` is available, call it with
   `{target:{type:"browser",url:<url>},placement:"right"}`. This is the
   Codex in-app Browser path; never call `open`, `start`, `xdg-open`, or another
   system-browser command.
5. If the app opener is unavailable or reports that it cannot open the page,
   return the local URL and state that it must be opened in Codex's in-app
   Browser. Do not substitute another browser.
6. After opening or falling back to the URL, always include the returned URL as
   `[重新打开写作画布](<url>)` in the final response.

## Canvas behavior

The page uses rendered Markdown as the primary canvas. Clicking a top-level
block makes that rendered block directly editable in place; it does not open a
separate editor. Changes are converted back to Markdown and replace only that
block. It also contains Markdown/PDF export, full-document copy, debounced
autosave, and revision-aware synchronization.
The header contains a document switcher menu. Codex creates documents; the page
displays, switches, and can delete them after confirmation. The service may have
no documents; the document menu and canvas show an empty state until the user
creates one. Each document has independent content, revision, review, and
in-memory undo/redo state.
Menu names follow the current document's first Markdown level-one heading; when
that heading is missing or empty, the menu shows “未命名文档”.
When Codex proposes a marked edit, the local service keeps one pending review
proposal instead of changing the document immediately. The page shows the
original block with a deletion line and the proposed block in blue, with
撤销/接受 actions; only 接受 writes the document. The page polls the server
revision; it applies external changes when there are no unsaved local edits and
otherwise keeps the local text and asks the user to reload.

## Use Codex's built-in marking

When the user asks to follow a mark or modification created by Codex's built-in
Browser configuration:

1. Read the exact selected source text and modification instruction exposed by
   the current Codex context.
2. Read the target canvas with `canvas.py read --document-id <id>` and verify
   that the selected source occurs exactly once at the expected location.
3. Construct the replacement text in memory, changing only that selected range.
4. Pipe `{"source": "...", "replacement": "..."}` to `canvas.py propose
   --document-id <id> --expected-revision <revision>`. A source may span
   multiple contiguous Markdown blocks. If the source is missing, ambiguous,
   does not map to a contiguous block range, or the revision changed, create no
   proposal and ask the user to re-mark the selection or reload.
5. Refresh or let the open page poll; it must show the affected old and proposed
   content with 撤销/接受 controls. Do not write the document directly; the
   page's 接受 action performs the revision-checked replacement.

Annotation-driven edits must use this local CLI flow; do not click canvas blocks,
move the mouse, simulate keyboard input, or manipulate `contenteditable`. The
browser only needs to remain open for the page to receive its normal poll update.

Do not create a second local mark format or infer a range from surrounding text.

## Use the proposal flow for direct conversation edits

When a conversation has already created or opened a Writing Canvas, treat
follow-up requests to modify, rewrite, reorganize, or continue the current
document as direct conversation edits even if the user does not repeat
“Writing Canvas”. Read the current document first, construct an exact
`source`/`replacement` pair, and call `canvas.py propose`; do not return only
the changed content in chat. For a request to create another document, call
`canvas.py create`. Keep `write` for explicit immediate-apply requests and
direct in-canvas editing.

Export uses the local service endpoints `/api/export/markdown` and
`/api/export/pdf`; the latter uses the local `reportlab` runtime and a local
Chinese font, and never sends document content to a remote service.

## Validation

Do not start UI automation or browser validation by default. For copy, color,
and static HTML/CSS changes, run only the checks below plus `git diff --check`;
use browser validation only when the user explicitly requests it or the task
specifically targets interaction, responsive layout, or visual regression.

Run these checks after implementation or repair:

```bash
python3 /Users/suyunian/.codex/skills/writing-canvas/scripts/canvas.py self-check
python3 /Users/suyunian/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  /Users/suyunian/.codex/skills/writing-canvas
```

When UI verification is explicitly requested, the end-to-end check is: generate
Markdown → open the in-app Browser → edit or preview → apply one built-in Browser
modification to an exact source range → verify the page updates while all other
text stays unchanged.
