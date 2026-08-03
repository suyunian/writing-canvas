# 提交与推送规则

本仓库采用轻量的 Conventional Commits 约定，保持提交可读、可检索、可回溯。

## 提交格式

```text
<type>(<scope>): <summary>
```

- `type` 使用 `feat`、`fix`、`chore`、`docs`、`refactor`、`test`、`build`、`ci`、`perf`、`style`、`revert` 或 `release`。
- `scope` 可选，当前功能代码建议使用 `writing-canvas`。
- `summary` 使用动词开头，简洁说明本次完整变更，主题行不超过 72 个字符。

示例：

```text
feat(writing-canvas): add native checklist support
fix(writing-canvas): align completed task item styles
```

## 提交前

先只暂存本次变更涉及的文件，再运行统一检查：

```bash
git add <明确的文件路径>
./scripts/check.sh
git diff --cached --check
```

不要使用 `git add .` 混入无关文件。涉及界面或交互的改动，还需用 Codex 内置浏览器通过临时文档做一次人工回归；脚本不会自动打开浏览器。

## 首次启用

每个新克隆或新工作副本执行一次：

```bash
git config --local core.hooksPath .githooks
```

## 推送

确认提交内容和目标分支后再推送：

```bash
git push origin main
```

不使用强制推送，不改写已有提交历史。提交说明校验和推送前检查由 `.githooks/` 自动执行；也可以手动运行 `./scripts/check.sh`。
