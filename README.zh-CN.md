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

## 模板

- [templates/handoff.zh-CN.md](templates/handoff.zh-CN.md)：交接文件（中文），写法规则见
  [templates/handoff.md](templates/handoff.md)。
- [templates/landing.md](templates/landing.md)：会话怎么收尾。
- [templates/escalation.md](templates/escalation.md)：送到人面前的三类事项。
- [templates/worker-packet.md](templates/worker-packet.md)：给 worker 的任务包和交回格式。
- [templates/instructions.md](templates/instructions.md)：可以贴进 `AGENTS.md` 或 `CLAUDE.md` 的一段。

## 许可

[MIT](LICENSE)
