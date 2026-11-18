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

退出码 0 表示没有需要理由的发现，或都有理由；1 表示有没理由的 `block` 发现；2 表示工具没能完成检查（用法错误、
指定的提交不存在、git 缺失或太旧、git 调用失败、规则表读不了，或 `--overlay` 给的文件有错），不会默认放行。`--json` 给程序看，
每条发现带级别。

扫描器读到的 diff 不受所在克隆的影响。每次 git 调用都固定会改变 diff 文本、文件列表或合并自动结果的配置（前缀、算法、
上下文、重命名、冲突标记样式等），并去掉起同样作用的环境变量；补丁按文本读，所以 `-diff` 属性或 diff 驱动藏不住行。
读不懂的改动不是错误也不是放行，而是规则 `unreadable-change` 的一条 `block` 发现，和别的发现一样，一行 `Check-change:`
就能放行。包括：解析不了的补丁、子模块改动、未合并的路径、章鱼合并。文件类型变了（普通文件变符号链接，或反过来）按旧文件
被删算。扫描器自己的规则 `unreadable-change` 和 `overlay-changed` 不能在规则表里定义，也不能被叠加文件改。

运行检查的文件（钩子、CI 工作流、`tools/check_integrity.py` 和它的规则表；规则表里的类 `machinery`）按整个文件判，规则
`check-machinery-changed`，每个文件一条发现：加行或删行、文件新建、删除或移动、权限位变了、文件类型变了，不管行写什么，
都是 `block`，和读这些文件各行的规则并列报。`check-file-weakened` 里对新增行的放宽判断，除了 `pyproject.toml`，现在也
覆盖 lint 和类型检查配置（类 `lint-config`：ruff、mypy、flake8、pylint、pyright、pytest.ini、setup.cfg、tox.ini、
coverage、eslint、biome、clippy、deny、golangci、shellcheck、stylelint、markdownlint）。

只在钩子或 CI 文件里挪版本号的改动是 `note`，不是 `block`：依赖机器人写不了理由行。哪些行的版本号机器人可以挪，列在规则表的
`version_lines` 里：带固定版本或标签的 `uses:`、`runs-on`、运行时或工具链的版本键（`python-version`、`node-version` 等）、
`image`、`container` 和 pre-commit 钩子的 `rev`。类 `bypass` 里的文件只是原地修改，而且每一处改动都能把一行被删的和一行
新增的配成对（同一个 hunk、同一个顺序），两行把版本号（数字和点）和完整的十六进制摘要换成占位符之后是同一行，这个文件就报
`note` 级的 `version-bumped`，不再报 `check-file-weakened` 和 `check-machinery-changed`。文件里有任何别的改动、版本行不在列表里、
别的数字（超时、覆盖率下限）、换了另一个 action、文件被移动或改了类型、权限位变了，都仍然是 `block`。规则表没有
`version_lines` 时什么都不算版本升级。叠加文件只能设 `version-bumped` 的级别；设成 `block` 就又要理由行了。

三条规则抓「不删测试、直接改测试把它关掉」：

- `test-short-circuited`：测试函数的第一句新增了光秃秃的 `return`（或 `return None`、`return;`），测试在断言之前就结束了，
  但仍然通过。测试指 Python 的 `def test...`（含 async）、Rust 的任何 `fn`、JS 或 TS 的 `it(`、`test(` 回调和 Go 的
  `func Test...`。规则看新文件里新增行的上一行，所以把 `return` 从一个测试挪到另一个会报，原本就在的不报。
- `test-class-detached`：改动在所有测试文件里一共丢了一个带 `TestCase` 基类的类（基类被去掉，或类被删）：unittest 不再收集它的测试。
- `tautology-added`：新增了不可能失败的断言：`assertTrue(True)`、`assertFalse(False)`、`assert True`、两边完全相同的
  `assertEqual` 或 `assertIs`、`assert x == x`、`expect(x).toBe(x)`、`assert_eq!(x, x)`（一边是一个标识符、一条属性路径或一个
  字面量）、`assertIsNone(None)`、对字面量的 `assertIsNotNone`，以及对非零数字或非空字符串的 `assertTrue` 和 `assert`。
  只是挪了位置的行不算新增。

这三条和别的发现一样，一行 `Check-change:` 就能放行。

合并提交按这次合并在自动合并之外又改了什么来判（`git show --remerge-diff`）：干净的合并没有发现，被合并进来的提交
各自只判一次。解决冲突时加了跳过、删了检查行，会报在合并提交上；解决冲突时去掉的冲突标记不算。这要 git 2.36 或更新：
git 太旧，退出码为 2 并说明，不会换个办法硬判。章鱼合并（三个及以上父提交，git 不能对它做 remerge-diff）是一条
`unreadable-change` 发现。

`--staged` 在合并等着提交时（存在 `MERGE_HEAD`，不论是冲突之后还是 `git merge --no-commit`）也这样判：拿暂存区的结果和
自动合并（`git merge-tree --write-tree`，要 git 2.38 或更新，否则退出码为 2）比，不再和 `HEAD` 比，所以干净的合并不会让
commit-msg 钩子去要被合并进来的那些提交的理由，只判解决冲突时改的部分。解决冲突时去掉的冲突标记不算发现。正在进行的合并
有两个以上被合并的提交时，是一条 `unreadable-change` 发现。

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
发现，要 `Check-change:` 行，所以一次提交不能不说理由就放宽判它的规则。叠加文件不能提到 `overlay-changed` 和
`unreadable-change`；对整个文件才判的规则（如 `check-machinery-changed`）不能加 `allow`：它没有可放行的行。

判一次改动的叠加文件，取自这次改动的基线提交，从来不是工作区里的，也不是被判提交自己带的：没暂存的修改放宽不了判它的
规则。基线是：`--staged` 用 `HEAD`（合并进行中也一样），`--commit` 用第一个父提交，`--range` 用左端（`A...B` 用合并
基点），`--pre-push` 用远端那一侧。远端上新建的分支，用它和 `refs/remotes/<remote>/HEAD` 的合并基点上的叠加文件，找不到
就用最早的新提交的第一个父提交；根提交或没有共同祖先的范围没有基线，也没有叠加文件。基线里的叠加文件无效（不是 UTF-8、
不是普通文件、有不认识的键）时，当作没有叠加：stderr 一行警告，所有规则都按 `block` 判，这样修好它的那次提交不会被坏文件
拦住。`--overlay PATH` 改读那个文件，里面有错就退出码 2；`--no-overlay` 不读。

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

pre-push 钩子把 git 给的标准输入接到 `--pre-push`，并传远端的名字（`--remote "$1"`），新分支靠它找基线；新分支扫的是
没有任何远端分支持有的提交。[.github/workflows/ci.yml](.github/workflows/ci.yml) 对每个拉取请求和每次推送跑同样的扫描。
扫描器、规则表和 `.check-integrity.json` 一律取自基线，不取自被判的提交：拉取请求取 base 分支，推送到别的分支取默认分支，
推送到默认分支取旧的分支头（基线里还没有扫描器时，用改动自己带的那份判）。拉取请求扫它新增的提交；新分支或强推扫从它和
默认分支的合并基点起的每个提交，没有合并基点时只扫最上面一个。推送到默认分支也扫它带来的提交，但那里的发现只是警告，步骤
照样通过（推送已经落地，而压缩合并留下的提交不带原来各个提交的 `Check-change:` 行）；扫描器自己出错（退出码 2）仍然让
步骤失败。第一次推送或强推到默认分支没有可扫的，会说明。

### 提交检查的限制

- 本地钩子和钩子调用的扫描器都来自工作区，有写权限的人或代理可以在本地把它们关掉。真正守得住的是 CI 那一步，它用基线上的
  扫描器、规则表和叠加文件来判。
- 推到默认分支的提交会被扫，但只打警告，不让那一步失败。
- 规则是按行看的绊线，不是证明：把一个断言换成更弱但不恒真的断言、把检查逻辑挪进一个不在 `machinery` 类里的脚本，都看不出来；
  仓库可以在自己的规则表里把这类脚本加进 `machinery` 类。
- 带理由行的提交会通过：理由行的作用是让这类改动在 `git log` 里查得到，不是拦住它。
- 抓「直接改测试」的三条规则各有看不到的写法。`test-short-circuited` 只认测试函数的第一句是光秃秃的 `return`：测试中途的条件
  返回、定义行和 `return` 之间隔着 docstring、定义行跨多行的，都看不出来。`test-class-detached` 只看一行里有没有 `TestCase`，
  基类换成间接的 `TestCase` 子类会被报出来，是误报。`tautology-added` 只认规则表里列的写法：「同一个调用写两遍再比较」不算，
  因为它和确定性测试分不开，别的恒真断言也看不出来。

## 模板

- [templates/handoff.zh-CN.md](templates/handoff.zh-CN.md)：交接文件（中文），写法规则见
  [templates/handoff.md](templates/handoff.md)。
- [templates/landing.md](templates/landing.md)：会话怎么收尾。
- [templates/escalation.md](templates/escalation.md)：送到人面前的三类事项。
- [templates/worker-packet.md](templates/worker-packet.md)：给 worker 的任务包和交回格式。
- [templates/instructions.md](templates/instructions.md)：可以贴进 `AGENTS.md` 或 `CLAUDE.md` 的一段。

## 许可

[MIT](LICENSE)
