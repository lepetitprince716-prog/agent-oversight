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

`tools/check_integrity.py` 读提交，找出削弱检查而不是通过检查的改动：跳过、删除或改名出收集范围的测试、去掉断言、压掉
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
指定的提交不存在、git 缺失或太旧、git 调用失败、规则表读不了，或 `--overlay` 给的路径不存在），不会默认放行。`--json` 给程序看，
每条发现带级别。

扫描器读到的 diff 不受所在克隆的影响。每次 git 调用都固定会改变 diff 文本、文件列表或合并自动结果的配置（前缀、算法、
上下文、重命名、冲突标记样式、文件顺序、空行写法、replace ref、消息编码、renormalize 等），并去掉起同样作用的环境变量；补丁按文本读，所以 `-diff` 属性或 diff 驱动藏不住行。
读不懂的改动不是错误也不是放行，而是规则 `unreadable-change` 的一条 `block` 发现，和别的发现一样，一行 `Check-change:`
就能放行。包括：解析不了的补丁、子模块改动、未合并的路径、章鱼合并。文件类型变了（普通文件变符号链接，或反过来）按旧文件
被删算。扫描器自己的规则 `unreadable-change` 和 `overlay-changed` 不能在规则表里定义，也不能被叠加文件改。

运行检查的文件（钩子、CI 工作流、`tools/check_integrity.py` 和它的规则表，以及钩子和 CI 当作第二道检查调用的隐私扫描器
`tools/privacy_scan.py` 和它的规则 `tools/privacy_rules.json`；规则表里的类 `machinery`）按整个文件判，规则
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

另外四种便宜的关检查办法，由已有的规则和规则表里新加的一个列表盖住：

- `test-removed` 还会报一次：测试文件从测试运行器会收集的名字（`test_*.py`、`*_test.py`、JavaScript 和 TypeScript 的 `x.test.*`
  和 `x.spec.*`、`*_test.go`）改成它们不收集的名字，例如 `tests/x_cases.py`：测试还在磁盘上，却不再运行。旧名字对应的源文件
  同一提交里也被删时，和删除一样放行。
- `skip-added` 还会报现有测试文件里新增的恒假条件（`if False:`、`if 0:`、`if (false) {`、`if false {`）：它守着的代码永远不会跑。
  只是挪了位置的行不算新增。
- 让一步什么都没查也算通过的参数，出现在钩子、CI 文件或脚本的新增行里，报 `bypass-added`；出现在 lint 或测试配置的新增行里，
  报 `check-file-weakened`。这些参数是 `--co` 和 `--collect-only`（pytest 只列出测试，一个都不跑）、`--passWithNoTests`
  （运行器找不到测试文件也算通过）、`--exit-zero`（linter 不管查出什么都以 0 退出）。
- 规则表里的 `guard_lines` 列出设下限的行（`fail_under`、`fail-under`、`--cov-fail-under`、`coverageThreshold`）。这样的行被原地改写，
  新文本和旧文本只要有任何不同，`check-file-weakened` 就报，哪怕每个词都还在（`90` 改成 `.90`）。调高下限也报，也要理由行。

合并提交按这次合并在自动合并之外又改了什么来判（`git show --remerge-diff`）：干净的合并没有发现，被合并进来的提交
各自只判一次。解决冲突时加了跳过、删了检查行，会报在合并提交上；解决冲突时去掉的冲突标记不算。这要 git 2.36 或更新：
git 太旧，退出码为 2 并说明，不会换个办法硬判。章鱼合并（三个及以上父提交，git 不能对它做 remerge-diff）是一条
`unreadable-change` 发现。

重建自动合并时不读任何 `.gitattributes` 文件，工作区的和提交里的都不读（`git --attr-source` 指向空树，要 git 2.41 或更新，
否则退出码为 2）。CI 里的工作区就是被判改动自己的头提交，不这样做的话，改动带来的合并属性（`union`、`-merge`、`binary`）
会左右拿来对比的那个自动合并，解决冲突时丢掉一边的行就和它一样，判成没有改动。现在即使这类属性本来会保留一边，丢掉另一边的行
也照样被判。克隆自己的 `.git/info/attributes` 和 `core.attributesFile` 仍然起作用：它们属于跑扫描的人，不属于被判的改动。

`--staged` 在合并等着提交时（存在 `MERGE_HEAD`，不论是冲突之后还是 `git merge --no-commit`）也这样判：拿暂存区的结果和
自动合并（`git merge-tree --write-tree`，要 git 2.38 或更新，否则退出码为 2；同上，不读 `.gitattributes`）比，不再和 `HEAD` 比，所以干净的合并不会让
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
拦住。`--overlay PATH` 改读那个文件（CI 那一步给它的就是基线里的），文件在但无效时同样处理，所以坏文件拦不住每一次运行；
路径不存在才退出码 2。`--no-overlay` 不读。

本仓库自己的文件让 `tests/test_check_integrity.py` 不受 `skip-added`、`suppression-added`、`bypass-added` 判断：
这个文件里有当作数据的标记字符串。改这些字符串不要理由行；在这个文件里删测试或删断言照样要，在别的测试文件里加跳过也照样要。

### 在别的仓库里用

复制 `tools/check_integrity.py` 和 `tools/check_integrity_rules.json`，在仓库自己的钩子里调用。本仓库
`.githooks/commit-msg` 和 `.githooks/pre-push` 展示了怎么和别的检查一起跑，任何一个失败都拒绝。

```sh
#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
python3 -I -B tools/check_integrity.py --staged --message-file "$1"
```

`-I`（隔离模式）让扫描器自己所在的目录和 `PYTHON*` 环境变量都不进模块搜索路径，放在扫描器旁边的文件顶替不了它导入的标准库模块。

pre-push 钩子把 git 给的标准输入接到 `--pre-push`，并传远端的名字（`--remote "$1"`），新分支靠它找基线；新分支扫的是
没有任何远端分支持有的提交。[.github/workflows/ci.yml](.github/workflows/ci.yml) 对每个拉取请求和每次推送跑同样的扫描。
工作流分两个 job：`gates` 先跑这道扫描、再跑隐私扫描，没有运行仓库测试或构建的步骤；`tests` 跑仓库自己的测试。
拉取请求上 `gates` 检出的是头提交，不是 GitHub 为这次运行造的试合并提交（那个提交用的是开拉取请求的账号的网页身份，隐私扫描会拒绝它的地址）；
隐私扫描读的是检出取回的每个分支（`rev-list --all`），不只是那个提交的历史。
两个扫描器都用隔离模式（`python3 -I -B`）启动。拉取请求的运行会被同一个拉取请求的新运行替换；推送的运行不会，因为每次推送的运行各用各的并发组：
推送扫的是从旧分支头到新分支头，推送的运行被替换或取消，它带来的提交就没被扫过（GitHub 还会把同一个组里还在排队的运行换成后排进来的那个，
和 `cancel-in-progress` 无关，所以同一分支的推送不能共用一个组）。这道扫描排在前面，是因为推送时隐私扫描跑的是被推提交自己的
`tools/privacy_scan.py`，它能把 `origin/<默认分支>` 移走，让这道扫描没有东西可判；这道扫描红了，隐私扫描那一步照样会跑。
这道扫描用的扫描器、规则表和 `.check-integrity.json` 一律取自基线，不取自被判的提交：拉取请求取 base 分支，推送到别的分支取默认分支，
推送到默认分支取旧的分支头（基线里还没有扫描器时，用改动自己带的那份判）。隐私扫描只在拉取请求上这样做；推送时它跑的是被推提交自己的扫描器。
两步都用完整的引用名（`refs/remotes/origin/<分支>`）找 base 分支或默认分支，并且只解析一次成提交号：git 把简写的
`origin/<分支>` 先当成同名的标签来读，而检出会取回所有标签，推上来一个叫 `origin/main` 的标签不能顶替那个分支。
克隆里没有那个分支时，步骤以退出码 2 结束。拉取请求扫它新增的提交；一个提交也没比 base 多的拉取请求让步骤失败（退出码 1），
因为空范围永远不能当成通过。新分支或强推扫从它和
默认分支的合并基点起的每个提交，没有合并基点时只扫最上面一个。推送到默认分支也扫它带来的提交，但那里的发现只是警告，步骤
照样通过（推送已经落地，而压缩合并留下的提交不带原来各个提交的 `Check-change:` 行）；扫描器自己出错（退出码 2）仍然让
步骤失败。第一次推送或强推到默认分支没有可扫的，会说明。

### 提交检查的限制

- 本地钩子和钩子调用的扫描器都来自工作区，有写权限的人或代理可以在本地把它们关掉。CI 那一步更可靠，但它自己也有限制：
  - 在拉取请求上，`gates` 这个 job 不运行被判的改动里的任何代码：两个扫描器都是 base 分支的，仓库的测试在另一个 job `tests` 里跑，
    扫描器用隔离模式启动，改动放在它们旁边的文件顶替不了标准库模块。这一步用基线上的扫描器、规则表和叠加文件来判。
    推送时隐私扫描跑的是被推提交自己的 `tools/privacy_scan.py`，排在这一步判完推送之后；改这个文件是这一步的发现
    （`check-machinery-changed`），但被推的扫描器打印什么，是它自己说了算。
  - 改动碰了工作流文件本身时，GitHub 跑的是改过的那一版：拉取请求上如此，合并后推到默认分支的那次也如此。改写或删掉这一步的改动，
    CI 里没有任何一步会报，也没有谁说这一步被关了。带着钩子的克隆推送时，pre-push 会把它报成 `check-machinery-changed`，除非提交带了理由行
    （在临时仓库上试过）。合并前要有人读 `.github/workflows/` 里的每一处改动。
  - 默认分支上的规则表或扫描器自己坏了（加载不了）时，这一步对每个拉取请求都失败，修它的那个也一样，只能带着红的检查合并修复。
    `tests` 这个 job 的作用之一，就是不让坏的规则表合进去。
- 推到默认分支的提交会被扫，但只打警告，不让那一步失败。
- commit-msg 钩子可能放过一条随后被 git 删掉的理由行。`core.commentChar` 设成一个字母时，以那个字母开头的理由行能通过钩子（钩子读的是
  git 去掉注释行之前的信息），提交落地时没有这一行（试过）；pre-push 和 CI 随后会拦住。
- 用 GitHub 的合并按钮合并拉取请求时，按 GitHub 文档的说法，落到默认分支的那个提交是用合并者账号的网页身份写的；账号没有开
  「Keep my email addresses private」时，这个地址不在允许列表里，合并之后默认分支上的隐私扫描会失败。办法有两个：在 GitHub 的
  邮箱设置里打开这个开关，或者在本地合并后推送。这一点只在 GitHub 为拉取请求运行造的试合并提交上验证过，没有在这个仓库的按钮合并上验证过。
- 规则是按行看的绊线，不是证明：把一个断言换成更弱但不恒真的断言、把检查逻辑挪进一个不在 `machinery` 类里的脚本，都看不出来；
  仓库可以在自己的规则表里把这类脚本加进 `machinery` 类。
- 带理由行的提交会通过：理由行的作用是让这类改动在 `git log` 里查得到，不是拦住它。
- 抓「直接改测试」的三条规则各有看不到的写法。`test-short-circuited` 只认测试函数的第一句是光秃秃的 `return`：测试中途的条件
  返回、定义行和 `return` 之间隔着 docstring、定义行跨多行的，都看不出来。`test-class-detached` 只看一行里有没有 `TestCase`，
  基类换成间接的 `TestCase` 子类会被报出来，是误报。`tautology-added` 只认规则表里列的写法：「同一个调用写两遍再比较」不算，
  因为它和确定性测试分不开，别的恒真断言也看不出来。
- 规则看不到的、关掉测试或检查的其他办法：
  - 在测试函数里加一行 `yield`，函数变成生成器，运行器不再执行函数体。按行读的规则分不出它和普通 `yield` 夹具，所以不报。
  - 基类写成表达式（`unittest.TestCase.__base__`，实际是 `object`）不报：类定义行里还有 `TestCase` 这个词，不算丢了一个类。
  - 新增的文件运行起来做什么，没有规则去判。新的 `conftest.py` 或 `tests/__init__.py` 在导入时把断言方法换掉，整套测试会变绿，而不被报。
    看新增行的规则会读新文件，有两处例外：`skip-added` 不看新的测试文件；`check-file-weakened` 看新增行的那一条不看新的 lint 或测试配置，
    所以新建一个 `addopts` 里带 `--collect-only` 的 `pytest.ini` 不报。
  - 恒假条件只认四种写法：`if False:`、`if 0:`、`if (false) {`、`if false {`。`else if (false)`、`if (0)`、`if (False):` 不认。
  - `package.json` 和 `jest.config.*` 里的 `coverageThreshold` 不在看守范围里：`package.json` 属于 `script` 类，那一类没有看改写的规则；
    `jest.config.*` 不属于任何类。
  - 上面四个参数，出现在钩子、CI 文件、脚本或 lint 配置的任何新增行里就报，不管那一行跑的是什么；不属于这些类的文件里的行不报，
    例如拼出 `pytest --collect-only` 命令的 Python 源文件。它们是一张单子，不是所有同类参数：pytest 的 `--setup-plan`、`--setup-only`
    同样让测试一条不跑，却不在单子上；`--passWithNoTests` 只认这一种写法（`--pass-with-no-tests` 不认）。
  - 「会被收集的名字」是几种测试运行器的规则的并集，不是这个仓库自己的运行器的规则。`test_x.py` 改名成 `x_test.py`，扫描器认为前后都被收集，
    而 `unittest discover` 的默认模式 `test*.py` 不再收集新名字：里面的测试不再跑，也没有东西失败（它是唯一的测试文件时，Python 3.11 打印
    `Ran 0 tests` 和 OK，3.13 以退出码 5 结束并打印 `NO TESTS RAN`）。对 Python，扫描器认的名字是 `test_*.py` 和 `*_test.py`，不是 unittest 的
    `test*.py`：`testx.py` 在原目录改名成 `helper.py` 不报，尽管 unittest 不再收集它；`test_x.py` 改名成 `testx.py` 会报，尽管 unittest 照样收集它。
  - 把一条测试挪进测试目录下另一个名字不被收集的文件（`tests/support.py`，新建的或原来就有的），不报：丢掉的测试和断言是在 `test` 类的
    所有文件上合起来数的，而 `tests/` 下的每个文件都属于这一类。挪到所有测试目录之外的文件就会报。改名出收集范围或删掉一个测试文件，只要同一次提交里
    删掉了一个和它的源文件同名的文件（`test_util.py` 对 `util.py`）就放行，不管它在哪个目录，也不管它是不是这个测试测的那个文件。
- 没有丢东西、但要理由行的改动：
  - 测试文件里，两边改了同一条断言，解决冲突时取一边：不论取哪一边（`--ours`、`--theirs` 或手改），被舍弃的那条都由 `assertion-loss`
    报在合并提交上。`-X ours`、`-X theirs` 这类自动取一边的合并同理。
  - 调高覆盖率下限；下限那一行只改了行尾注释、缩进，或在行尾加了一个别的参数也一样：`guard_lines` 报这一行的任何改写。
- 真实历史里读到的误报，理由行可以放行：
  - `skip-added` 对每一行新增的 `skipTest(...)` 或 `skipUnless(...)` 都报，只是按环境跳过（以 root 运行、没有信号量的沙箱）的也报，
    只改了提示文字的行也报，因为改写后的那一行和原来的文本不同。
  - `test-removed` 对「名字属于收集名、里面却没有测试」的文件改名也报（`tests/test_helpers.py` 只有辅助函数，改名成 `tests/helpers.py`），
    因为它只看名字，不看文件里有什么。
  - 恒假条件和别的标记行是按行认的，作为数据出现时也报：测试文件里三引号字符串中的样例代码、`tests/` 下的样例项目文件。
    叠加文件可以把这样的文件整个豁免（`exempt_paths`；本仓库对自己的测试文件就是这么做的）。
  - 读整个钩子和 CI 文件的规则，对重构过的工作流（job 改名、一个步骤挪到另一个 job、矩阵变了）每个被删的行报一次；读过的几例里，
    没有哪个检查被关掉。

## 模板

- [templates/handoff.zh-CN.md](templates/handoff.zh-CN.md)：交接文件（中文），写法规则见
  [templates/handoff.md](templates/handoff.md)。
- [templates/landing.md](templates/landing.md)：会话怎么收尾。
- [templates/escalation.md](templates/escalation.md)：送到人面前的三类事项。
- [templates/worker-packet.md](templates/worker-packet.md)：给 worker 的任务包和交回格式。
- [templates/instructions.md](templates/instructions.md)：可以贴进 `AGENTS.md` 或 `CLAUDE.md` 的一段。

## 许可

[MIT](LICENSE)
