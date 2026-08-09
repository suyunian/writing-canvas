# Writing Canvas

Writing Canvas 是一个面向 Codex 的本地 Markdown 写作画布 Skill：把 Markdown
渲染成可编辑文档，支持文档切换、局部编辑、审阅提案、Markdown/PDF 导出和
本地运行数据持久化。

它只服务于本机写作场景，不连接云端，也不是面向多用户的在线文档服务。

## 功能

- 在 Codex 中创建、编辑和预览 Markdown 文档；
- 用 revision-aware proposal 流程审阅局部修改；
- 管理活动文档和归档文档；
- 导出 Markdown，或在安装可选依赖和中文字体后导出 PDF；
- 运行数据与源码分离，避免用户文档进入 Git 仓库。

## 环境要求

- Python 3.10 或更高版本；
- macOS 和 Linux 已验证；Windows 尚未验证，当前文件锁实现依赖 POSIX 的
  `fcntl`；
- Node.js：只用于运行前端 JavaScript 语法检查。

必需依赖写在 `requirements.txt` 中：

```bash
python3 -m pip install -r requirements.txt
```

PDF 导出是可选能力，额外安装：

```bash
python3 -m pip install -r requirements-pdf.txt
```

PDF 导出还需要系统中存在可用的中文字体；没有中文字体时，画布和
Markdown 导出仍可用。

## 安装到 Codex

将本仓库克隆到任意位置后，把 Codex 的 Skill 安装目录指向它。下面的命令
不会覆盖已有目录：

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
test ! -e "$CODEX_HOME/skills/writing-canvas" || {
  echo "已有安装目录，请先人工确认：$CODEX_HOME/skills/writing-canvas" >&2
  exit 1
}
ln -s "$(pwd)" "$CODEX_HOME/skills/writing-canvas"
```

重新启动 Codex 或新建会话后，Codex 会从 `SKILL.md` 发现这个 Skill。

## 启动

在仓库根目录运行：

```bash
python3 scripts/canvas.py ensure
```

然后在 Codex 内置 Browser 中打开：

```text
http://127.0.0.1:39173/
```

也可以使用 `--data-dir` 和 `--port` 指定一次性运行参数。

## 数据位置

默认运行数据目录是：

```text
$CODEX_HOME/writing-canvas-data
```

当 `CODEX_HOME` 未设置时，默认使用 `~/.codex/writing-canvas-data`。文档、
审阅提案、索引和锁文件都保存在这里，不保存在仓库中；请按个人数据备份
策略保护这个目录。

## 安全边界

- 服务只绑定 `127.0.0.1`；
- 服务没有远程多用户认证；
- 不要把监听地址改成 `0.0.0.0`，也不要通过公网、反向代理或端口转发暴露；
- 运行数据可能包含用户私密文档，不应提交到 Git 或上传到公开仓库；
- 这是本地单用户工具，不提供账号系统、云端同步或跨设备访问控制。

## 示例和第三方内容

`examples/tesla-2025-annual-report.md` 是写作画布测试稿，文件末尾列出了
SEC 和 Tesla Investor Relations 的公开来源链接；它不是 Tesla 官方文件，也
不应被误认为投资建议。该示例不复制原始报告正文，但再分发前仍应确认示例
内容和来源链接符合你的版权与合规要求。仓库 MIT 许可不自动覆盖第三方内容。

## 检查

安装必需依赖后运行：

```bash
./scripts/check.sh
```

检查包括 Skill 自检、`markdown-it-py` 依赖检查、Python 编译、前端 JavaScript
语法、Git 空白和暂存区空白检查。`reportlab` 未安装时只会提示 PDF 导出不可用，
不会阻塞其他检查。

## 开源许可

除示例文件和其他可能受第三方权利约束的内容外，仓库代码按 MIT License 发布。
详见 `LICENSE`。

## 贡献

提交格式和发布检查见 `CONTRIBUTING.md`。建议后续开发使用功能分支和 Pull
Request，再合并到 `main`。
