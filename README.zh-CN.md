# agent-oversight

不读 agent 的对话记录，也能同时跑很多个编码 agent。[English](README.md)

一个人同时跑很多 agent（交互会话、后台 worker、定时任务）时，没法再逐条跟着读。跟着读原本是最后一道
检查，没有它会出四种问题：

- agent 把没做完的事报成做完了；
- 坏消息只在一个没人打开的会话里说过一次；
- 「谁在等什么、谁手上有什么」的记录和实际对不上；
- 会话一结束，手上的活就丢了，没人发现。

这个仓库放的是应对这种情况的一套小方法、配套模板，和一个只读工具。

## 方法

共六部分，全文见 [docs/method.md](docs/method.md)（英文），每一部分借鉴了谁的做法见
[docs/sources.md](docs/sources.md)。

- 记录：任务库才是记录，会话随时可弃。
- 证据：「做完」是一个要带证明的说法；风险高于低档时，做的人不能自己验。
- 对账：由程序拿记录和实际情况对比，列出对不上的地方。
- 上报：只有决定、验收、事故三类事送到人面前，每件是同一个收件箱里的一条。
- 改进：每个晚到的问题先变成一条检查，最后才变成一条规则。
- 分工：只在划算的地方拆给多个 agent。一组文件只有一个写入者，读和查可以铺开。

## 工具

`oversight` 读 [beads](https://github.com/steveyegge/beads) 任务库、GitHub 和本机的克隆，打印记录
和实际情况对不上的地方。它不改任何东西，由 agent 照着清单去处理。

需要 Python 3.11 以上、`bd` 命令、GitHub CLI（`gh`）和 `git`，不用安装别的。

```bash
mkdir -p ~/.config/agent-oversight
cp config.example.toml ~/.config/agent-oversight/config.toml   # 再填上任务库目录和你的仓库
bin/oversight reconcile
bin/oversight digest
```

`digest` 是给人看的：等你的事按类型和天数排列，后面是对不上的地方，一共十行。配置里写 `lang = "zh"`，
计数行和类型标签用中文，对不上的条目保持英文。

| 检查 | 对不上的情况 |
|---|---|
| `blocker_gone` | 在等的任务点名的 PR 已经合并或关闭。 |
| `blocker_stuck` | 在等的任务点名的 PR 还开着，但有冲突或检查失败。 |
| `silent_wait` | 在等的任务没写在等什么。 |
| `quiet_holder` | 进行中的任务连续多天没有更新。 |
| `schedule_red` | 定时 CI 最近几次连续失败。 |
| `pr_idle` | 开着的 PR 多天没人动，又没有任务在等它：有冲突、检查失败、还是草稿，或者能合并却一直没合。 |
| `local_only` | 本机分支多天没动，上面有提交不在任何远端。每个提交只算一次，记在被别的分支当作起点的那条分支名下。 |
| `guard_off` | 标了 `public` 的仓库，本机克隆里没有可执行的 pre-push 钩子。 |

任务用 `repo:<name>` 标签说明属于哪个仓库，在备注里 `blocker:` 或 `卡点：` 之后写在等什么。那一行里的
PR 写成 `PR #12`、`owner/name#12` 或 PR 链接时会被查到。最后两项检查看的是本机磁盘，要在配置里给仓库
写上克隆目录：`path` 开启 `local_only`，再加 `public = true` 开启 `guard_off`（见 [PRIVACY.md](PRIVACY.md)）。

查不到的项目会列为「没查到」，不会当成没问题。`schedule_red` 看每个启用中的 workflow 最近 20 次定时
运行；停用的 workflow 不看，包括仓库 60 天没有活动后被 GitHub 自动停用的。开着的 PR 或 workflow 达到
1000 个的仓库，列表可能不全，也列为「没查到」。配置里的 `path` 要写克隆的顶层目录，用绝对路径或以 `~`
开头；不是顶层目录会列为「没查到」，相对路径在读配置时就被拒绝。退出码 0 表示没有要处理的，
1 表示有，2 表示工具没能完成检查：配置或任务库读不了，或者工具自己出错（traceback 在标准错误里）。输出固定
是 UTF-8。

## 提交检查

`tools/check_integrity.py` 读提交，找出削弱检查而不是通过检查的改动：跳过或删除测试、去掉断言、压掉
linter 或类型检查、绕过钩子或 CI 步骤、从检查配置里删行。它是一个文件加一份规则表，只用标准库。

被报的提交，提交说明里有一行顶格的 `Check-change: <理由>`，理由不少于 20 个字符，就能通过；一行
管该提交的全部发现。只认提交说明：写在代码旁边的理由不算。

每条规则有一个级别。`block` 是默认级别，要有这一行，否则退出码为 1。`note` 照常打印并计数，不影响退出码，
也不需要这一行。自带规则里只有 `suppression-added`（新增 `# noqa`、`# type: ignore`、`eslint-disable`
这类压制标记）是 `note`：这类标记多半是局部判断，每个都要理由，理由行就成了例行公事。

```bash
python3 tools/check_integrity.py --staged --message-file .git/COMMIT_EDITMSG   # commit-msg 钩子跑的
python3 tools/check_integrity.py --commit HEAD                                 # 一个提交（合并提交见下）
python3 tools/check_integrity.py --range origin/main..HEAD                     # 一个范围里的每个提交
python3 tools/check_integrity.py --pre-push < refs                             # git 给 pre-push 钩子的那几行
```

退出码 0 表示没有需要理由的发现，或都有理由；1 表示有没理由的 `block` 发现；2 表示工具没能完成检查（规则表读
不了、git 调用失败、diff 解析不了），不会默认放行。`--json` 给程序看，每条发现带级别。

合并提交按这次合并在自动合并之外又改了什么来判（`git show --remerge-diff`）：干净的合并没有发现，被合并进来的提交
各自只判一次。解决冲突时加了跳过、删了检查行，会报在合并提交上；解决冲突时去掉的冲突标记不算。这要 git 2.36 或更新：
git 太旧，或者是章鱼合并（三个及以上父提交，git 不能对它做 remerge-diff），退出码为 2 并说明，不会换个办法硬判。
`--staged` 在收尾一次合并的提交上，仍然和第一个父提交比。

### 单个仓库调整规则

仓库顶层的 `.check-integrity.json` 叠加在规则表上。它只能按规则 id 改 `severity`、追加 `allow` 正则和列 `exempt_paths`，别的
键或规则表里没有的 id 都是错误（退出码 2）。`allow` 正则从每处匹配的起点试起，对规则覆盖的所有文件生效。
`exempt_paths` 是对仓库内相对路径的 glob（`fnmatch` 规则：`*` 也匹配 `/`），路径匹配的文件完全不受这条规则判断；
重命名的文件要新旧两个路径都匹配才算豁免。

```json
{
  "version": 1,
  "rules": {
    "skip-added": { "severity": "note" },
    "bypass-added": { "allow": ["\\|\\|\\s*true\\b.*# best effort"] }
  }
}
```

这个文件本身受管：对它的任何改动（加行、删行、新建、删除）都是扫描器自己的 `overlay-changed` 规则的一条 `block`
发现，要 `Check-change:` 行，所以一次提交不能不说理由就放宽判它的规则。叠加文件不能改动、降级或豁免这条规则（退出码
2）。`--overlay PATH` 读另一个文件，`--no-overlay` 不读。

本仓库自己的文件让 `tests/test_check_integrity.py` 不受 `skip-added`、`suppression-added`、`bypass-added` 判断：
这个文件里有当作数据的标记字符串。改这些字符串不要理由行；在这个文件里删测试或删断言照样要，在别的测试文件里加跳过也照样要。

### 在别的仓库里用

复制 `tools/check_integrity.py` 和 `tools/check_integrity_rules.json`，在仓库自己的钩子里调用。本仓库
`.githooks/commit-msg` 和 `.githooks/pre-push` 展示了怎么和别的检查一起跑，任何一个失败都拒绝。

```sh
#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
python3 tools/check_integrity.py --staged --message-file "$1"
```

pre-push 钩子把 git 给的标准输入接到 `--pre-push`；新分支扫的是没有任何远端分支持有的提交。
[.github/workflows/ci.yml](.github/workflows/ci.yml) 对每个拉取请求、以及每次推送到默认分支以外的分支，跑同样的扫描，
拉取请求上扫描器、规则表和 `.check-integrity.json` 取自 base 分支。推送到默认分支时不扫：合并拉取请求产生的提交
不带原来各个提交的 `Check-change:` 行，而拉取请求已经逐个提交判过。本地的 pre-push 钩子仍然会扫直接推送。

## 模板

- [templates/handoff.zh-CN.md](templates/handoff.zh-CN.md)：交接文件（中文），写法规则见
  [templates/handoff.md](templates/handoff.md)。
- [templates/landing.md](templates/landing.md)：会话怎么收尾。
- [templates/escalation.md](templates/escalation.md)：送到人面前的三类事项。
- [templates/worker-packet.md](templates/worker-packet.md)：给 worker 的任务包和交回格式。
- [templates/instructions.md](templates/instructions.md)：可以贴进 `AGENTS.md` 或 `CLAUDE.md` 的一段。

## 许可

[MIT](LICENSE)
